from flask import Flask, request, jsonify, send_from_directory, Response
import sqlite3, os, datetime, logging, threading, queue, json, time
from contextlib import contextmanager
from functools import wraps

app = Flask(__name__, static_folder='static', template_folder='templates')

DB_FILE = os.environ.get('DB_PATH', 'trip_database.db')

# ─── LOGGING ────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.FileHandler('app.log'), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# ─── SSE BROADCAST HUB ──────────────────────────────────────
# Maps  trip_id -> set of queue.Queue  (one per connected SSE client)
_sse_clients: dict[int, set] = {}
_sse_lock = threading.Lock()

def _sse_subscribe(trip_id: int) -> queue.Queue:
    q = queue.Queue(maxsize=50)
    with _sse_lock:
        _sse_clients.setdefault(trip_id, set()).add(q)
    return q

def _sse_unsubscribe(trip_id: int, q: queue.Queue):
    with _sse_lock:
        bucket = _sse_clients.get(trip_id, set())
        bucket.discard(q)

def broadcast(trip_id: int, event: str, data: dict):
    """Push an SSE event to every client watching this trip."""
    payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
    with _sse_lock:
        dead = set()
        for q in _sse_clients.get(trip_id, set()):
            try:
                q.put_nowait(payload)
            except queue.Full:
                dead.add(q)
        _sse_clients.get(trip_id, set()).difference_update(dead)

# ─── DB CONNECTION POOL ─────────────────────────────────────
_local = threading.local()

def get_conn():
    if not hasattr(_local, 'conn') or _local.conn is None:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-32000")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA mmap_size=268435456")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return _local.conn

@contextmanager
def db_transaction():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise

# ─── DB INIT ────────────────────────────────────────────────
def init_db():
    with db_transaction() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS all_users (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT UNIQUE NOT NULL,
                promptpay    TEXT DEFAULT '',
                bank_name    TEXT DEFAULT '',
                bank_account TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS trips (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                name      TEXT UNIQUE NOT NULL,
                trip_date TEXT DEFAULT '',
                deleted   INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS members (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                trip_id INTEGER NOT NULL,
                name    TEXT NOT NULL,
                FOREIGN KEY(trip_id) REFERENCES trips(id) ON DELETE CASCADE,
                UNIQUE(trip_id, name)
            );
            CREATE TABLE IF NOT EXISTS expenses (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                trip_id       INTEGER NOT NULL,
                description   TEXT NOT NULL,
                amount        REAL NOT NULL CHECK(amount >= 0),
                payer_name    TEXT NOT NULL,
                split_members TEXT DEFAULT '',
                image_blob    TEXT,
                created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(trip_id) REFERENCES trips(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS notifications (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                trip_id   INTEGER NOT NULL,
                to_user   TEXT NOT NULL,
                from_user TEXT NOT NULL,
                message   TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                is_auto   INTEGER DEFAULT 0,
                is_read   INTEGER DEFAULT 0,
                FOREIGN KEY(trip_id) REFERENCES trips(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS online_status (
                name      TEXT PRIMARY KEY,
                last_seen DATETIME
            );

            CREATE INDEX IF NOT EXISTS idx_members_trip    ON members(trip_id);
            CREATE INDEX IF NOT EXISTS idx_expenses_trip   ON expenses(trip_id);
            CREATE INDEX IF NOT EXISTS idx_notif_trip_to   ON notifications(trip_id, to_user);
            CREATE INDEX IF NOT EXISTS idx_notif_trip_from ON notifications(trip_id, from_user);
            CREATE INDEX IF NOT EXISTS idx_online_seen     ON online_status(last_seen);
        ''')
    logger.info("Database initialised.")

init_db()

def now_local():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

# ─── ERROR HANDLER ──────────────────────────────────────────
def handle_errors(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except sqlite3.IntegrityError as e:
            logger.warning("IntegrityError: %s", e)
            return jsonify({'error': str(e)}), 409
        except sqlite3.OperationalError as e:
            logger.error("OperationalError: %s", e)
            return jsonify({'error': 'Database error, please retry'}), 503
        except Exception as e:
            logger.exception("Unexpected error: %s", e)
            return jsonify({'error': 'Internal server error'}), 500
    return wrapper

# ─── SERVE FRONTEND ─────────────────────────────────────────
@app.route('/')
def index():
    return send_from_directory('templates', 'index.html')

# ══════════════════════════════════════════════════════════════
#  SSE STREAM  –  GET /api/trips/<id>/stream?user=<name>
# ══════════════════════════════════════════════════════════════
@app.route('/api/trips/<int:trip_id>/stream')
def trip_stream(trip_id):
    """
    Server-Sent Events endpoint.
    Clients connect once and receive push events whenever data changes.
    Events: expenses_changed | members_changed | notifications_new | online_changed | ping
    """
    q = _sse_subscribe(trip_id)

    def generate():
        # Send initial snapshot so client can sync immediately
        yield _snapshot_event(trip_id)
        # Then stream live events
        while True:
            try:
                msg = q.get(timeout=20)   # 20-s keepalive cycle
                yield msg
            except queue.Empty:
                yield "event: ping\ndata: {}\n\n"   # keepalive

    resp = Response(generate(), mimetype='text/event-stream')
    resp.headers['Cache-Control'] = 'no-cache'
    resp.headers['X-Accel-Buffering'] = 'no'   # disable nginx buffering

    # Clean up when client disconnects
    @resp.call_on_close
    def _cleanup():
        _sse_unsubscribe(trip_id, q)

    return resp

def _snapshot_event(trip_id: int) -> str:
    """Return a single SSE 'snapshot' event with full trip state."""
    conn = get_conn()
    expenses = [dict(r) for r in conn.execute(
        'SELECT * FROM expenses WHERE trip_id=? ORDER BY id', (trip_id,)
    ).fetchall()]
    for e in expenses:
        e['split_members'] = e['split_members'].split(',') if e['split_members'] else []

    members = [r['name'] for r in conn.execute(
        'SELECT name FROM members WHERE trip_id=? ORDER BY name', (trip_id,)
    ).fetchall()]

    online = [r['name'] for r in conn.execute(
        "SELECT name FROM online_status WHERE last_seen >= datetime('now','localtime','-15 seconds')"
    ).fetchall()]

    data = json.dumps({'expenses': expenses, 'members': members, 'online': online},
                      ensure_ascii=False)
    return f"event: snapshot\ndata: {data}\n\n"

# ─── USERS ──────────────────────────────────────────────────
@app.route('/api/users', methods=['GET'])
@handle_errors
def get_users():
    rows = [dict(r) for r in get_conn().execute(
        'SELECT id, name, promptpay, bank_name, bank_account FROM all_users ORDER BY name'
    ).fetchall()]
    return jsonify(rows)

@app.route('/api/users', methods=['POST'])
@handle_errors
def create_user():
    data = request.json or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'ชื่อห้ามว่าง'}), 400
    with db_transaction() as conn:
        conn.execute('INSERT INTO all_users (name) VALUES (?)', (name,))
        row = dict(conn.execute('SELECT * FROM all_users WHERE name=?', (name,)).fetchone())
    return jsonify(row), 201

@app.route('/api/users/<name>', methods=['PUT'])
@handle_errors
def update_user(name):
    data = request.json or {}
    with db_transaction() as conn:
        conn.execute(
            'UPDATE all_users SET promptpay=?, bank_name=?, bank_account=? WHERE name=?',
            (data.get('promptpay',''), data.get('bank_name',''), data.get('bank_account',''), name)
        )
    return jsonify({'ok': True})

# ─── ONLINE STATUS ───────────────────────────────────────────
@app.route('/api/heartbeat', methods=['POST'])
@handle_errors
def heartbeat():
    data = request.json or {}
    name = data.get('name','').strip()
    trip_id = data.get('trip_id')          # optional – for online broadcast
    if not name:
        return jsonify({'error': 'no name'}), 400
    ts = now_local()
    with db_transaction() as conn:
        conn.execute(
            'INSERT INTO online_status(name, last_seen) VALUES(?,?) '
            'ON CONFLICT(name) DO UPDATE SET last_seen=excluded.last_seen',
            (name, ts)
        )
    if trip_id:
        online = [r['name'] for r in get_conn().execute(
            "SELECT name FROM online_status WHERE last_seen >= datetime('now','localtime','-15 seconds')"
        ).fetchall()]
        broadcast(int(trip_id), 'online_changed', {'online': online})
    return jsonify({'ok': True})

@app.route('/api/online', methods=['GET'])
@handle_errors
def online_users():
    rows = get_conn().execute(
        "SELECT name FROM online_status "
        "WHERE last_seen >= datetime('now','localtime','-15 seconds')"
    ).fetchall()
    return jsonify([r['name'] for r in rows])

# ─── TRIPS ───────────────────────────────────────────────────
@app.route('/api/trips', methods=['GET'])
@handle_errors
def get_trips():
    rows = [dict(r) for r in get_conn().execute(
        'SELECT * FROM trips WHERE deleted=0 ORDER BY id DESC'
    ).fetchall()]
    return jsonify(rows)

@app.route('/api/trips', methods=['POST'])
@handle_errors
def create_trip():
    data = request.json or {}
    name = (data.get('name') or '').strip()
    date = (data.get('trip_date') or '').strip()
    if not name:
        return jsonify({'error': 'ชื่อ Event ห้ามว่าง'}), 400
    with db_transaction() as conn:
        conn.execute('INSERT INTO trips(name, trip_date) VALUES(?,?)', (name, date))
        row = dict(conn.execute('SELECT * FROM trips WHERE name=?', (name,)).fetchone())
    return jsonify(row), 201

@app.route('/api/trips/<int:trip_id>', methods=['PUT'])
@handle_errors
def update_trip(trip_id):
    data = request.json or {}
    with db_transaction() as conn:
        conn.execute('UPDATE trips SET name=?, trip_date=? WHERE id=?',
                     (data.get('name'), data.get('trip_date',''), trip_id))
    return jsonify({'ok': True})

@app.route('/api/trips/<int:trip_id>', methods=['DELETE'])
@handle_errors
def delete_trip(trip_id):
    with db_transaction() as conn:
        conn.execute('UPDATE trips SET deleted=1 WHERE id=?', (trip_id,))
    return jsonify({'ok': True})

# ─── MEMBERS ─────────────────────────────────────────────────
@app.route('/api/trips/<int:trip_id>/members', methods=['GET'])
@handle_errors
def get_members(trip_id):
    rows = get_conn().execute(
        'SELECT name FROM members WHERE trip_id=? ORDER BY name', (trip_id,)
    ).fetchall()
    return jsonify([r['name'] for r in rows])

@app.route('/api/trips/<int:trip_id>/members', methods=['POST'])
@handle_errors
def add_member(trip_id):
    name = ((request.json or {}).get('name') or '').strip()
    if not name:
        return jsonify({'error': 'ชื่อห้ามว่าง'}), 400
    with db_transaction() as conn:
        conn.execute('INSERT OR IGNORE INTO members(trip_id, name) VALUES(?,?)', (trip_id, name))
    # broadcast
    members = [r['name'] for r in get_conn().execute(
        'SELECT name FROM members WHERE trip_id=? ORDER BY name', (trip_id,)
    ).fetchall()]
    broadcast(trip_id, 'members_changed', {'members': members})
    return jsonify({'ok': True}), 201

@app.route('/api/trips/<int:trip_id>/members/<name>', methods=['DELETE'])
@handle_errors
def remove_member(trip_id, name):
    with db_transaction() as conn:
        conn.execute('DELETE FROM members WHERE trip_id=? AND name=?', (trip_id, name))
    members = [r['name'] for r in get_conn().execute(
        'SELECT name FROM members WHERE trip_id=? ORDER BY name', (trip_id,)
    ).fetchall()]
    broadcast(trip_id, 'members_changed', {'members': members})
    return jsonify({'ok': True})

# ─── EXPENSES ────────────────────────────────────────────────
@app.route('/api/trips/<int:trip_id>/expenses', methods=['GET'])
@handle_errors
def get_expenses(trip_id):
    rows = [dict(r) for r in get_conn().execute(
        'SELECT * FROM expenses WHERE trip_id=? ORDER BY id', (trip_id,)
    ).fetchall()]
    for r in rows:
        r['split_members'] = r['split_members'].split(',') if r['split_members'] else []
    return jsonify(rows)

def _fetch_expenses(trip_id: int, conn) -> list:
    rows = [dict(r) for r in conn.execute(
        'SELECT * FROM expenses WHERE trip_id=? ORDER BY id', (trip_id,)
    ).fetchall()]
    for r in rows:
        r['split_members'] = r['split_members'].split(',') if r['split_members'] else []
    return rows

@app.route('/api/trips/<int:trip_id>/expenses', methods=['POST'])
@handle_errors
def add_expense(trip_id):
    data = request.json or {}
    description = (data.get('description') or '').strip()
    amount = float(data.get('amount', 0))
    payer = (data.get('payer_name') or '').strip()
    split = [s.strip() for s in data.get('split_members', []) if s.strip()]

    if not description or not payer or amount <= 0:
        return jsonify({'error': 'ข้อมูลไม่ครบ'}), 400

    share = amount / len(split) if split else 0

    with db_transaction() as conn:
        cur = conn.cursor()
        cur.execute(
            'INSERT INTO expenses(trip_id,description,amount,payer_name,split_members,image_blob) '
            'VALUES(?,?,?,?,?,?)',
            (trip_id, description, amount, payer, ','.join(split), data.get('image_blob'))
        )
        exp_id = cur.lastrowid

        notifs = [
            (trip_id, member, 'ระบบสรุปยอด',
             f"📌 บิลใหม่: '{description}'\n"
             f"💰 ยอดรวม {amount:,.2f} บาท\n"
             f"👤 คนจ่าย: {payer}\n"
             f"💸 ส่วนของคุณ: {share:,.2f} บาท",
             1, 0, now_local())
            for member in split if member != payer
        ]
        if notifs:
            conn.executemany(
                'INSERT INTO notifications(trip_id,to_user,from_user,message,is_auto,is_read,timestamp) '
                'VALUES(?,?,?,?,?,?,?)', notifs
            )

        expenses = _fetch_expenses(trip_id, conn)

    broadcast(trip_id, 'expenses_changed', {'expenses': expenses})
    if notifs:
        broadcast(trip_id, 'notifications_new', {'trip_id': trip_id})
    return jsonify({'id': exp_id}), 201

@app.route('/api/expenses/<int:exp_id>', methods=['PUT'])
@handle_errors
def update_expense(exp_id):
    data = request.json or {}
    split = [s.strip() for s in data.get('split_members', []) if s.strip()]
    trip_id = data.get('trip_id')
    with db_transaction() as conn:
        conn.execute(
            'UPDATE expenses SET description=?,amount=?,payer_name=?,split_members=?,image_blob=? WHERE id=?',
            (data.get('description'), data.get('amount'), data.get('payer_name'),
             ','.join(split), data.get('image_blob'), exp_id)
        )
        if trip_id:
            expenses = _fetch_expenses(int(trip_id), conn)
    if trip_id:
        broadcast(int(trip_id), 'expenses_changed', {'expenses': expenses})
    return jsonify({'ok': True})

@app.route('/api/expenses/<int:exp_id>', methods=['DELETE'])
@handle_errors
def delete_expense(exp_id):
    data = request.json or {}
    trip_id = data.get('trip_id')
    with db_transaction() as conn:
        conn.execute('DELETE FROM expenses WHERE id=?', (exp_id,))
        if trip_id:
            expenses = _fetch_expenses(int(trip_id), conn)
    if trip_id:
        broadcast(int(trip_id), 'expenses_changed', {'expenses': expenses})
    return jsonify({'ok': True})

# ─── NOTIFICATIONS ───────────────────────────────────────────
@app.route('/api/trips/<int:trip_id>/notifications/<username>', methods=['GET'])
@handle_errors
def get_notifications(trip_id, username):
    limit  = min(int(request.args.get('limit', 100)), 500)
    offset = int(request.args.get('offset', 0))
    rows = [dict(r) for r in get_conn().execute(
        '''SELECT * FROM notifications
           WHERE trip_id=? AND (to_user=? OR from_user=?)
           ORDER BY timestamp ASC, id ASC LIMIT ? OFFSET ?''',
        (trip_id, username, username, limit, offset)
    ).fetchall()]
    return jsonify(rows)

@app.route('/api/notifications/send', methods=['POST'])
@handle_errors
def send_notification():
    data = request.json or {}
    trip_id = int(data['trip_id'])
    with db_transaction() as conn:
        conn.execute(
            'INSERT INTO notifications(trip_id,to_user,from_user,message,is_auto,is_read,timestamp) '
            'VALUES(?,?,?,?,0,0,?)',
            (trip_id, data['to_user'], data['from_user'], data['message'], now_local())
        )
    broadcast(trip_id, 'notifications_new', {'trip_id': trip_id, 'to_user': data['to_user']})
    return jsonify({'ok': True}), 201

@app.route('/api/notifications/read', methods=['POST'])
@handle_errors
def mark_read():
    data = request.json or {}
    trip_id   = data['trip_id']
    to_user   = data['to_user']
    from_user = data.get('from_user')
    with db_transaction() as conn:
        if from_user == 'ระบบสรุปยอด':
            conn.execute('UPDATE notifications SET is_read=1 WHERE trip_id=? AND to_user=? AND is_auto=1',
                         (trip_id, to_user))
        else:
            conn.execute('UPDATE notifications SET is_read=1 WHERE trip_id=? AND to_user=? AND from_user=?',
                         (trip_id, to_user, from_user))
    return jsonify({'ok': True})

@app.route('/api/notifications/<int:notif_id>', methods=['DELETE'])
@handle_errors
def delete_notification(notif_id):
    with db_transaction() as conn:
        conn.execute('DELETE FROM notifications WHERE id=?', (notif_id,))
    return jsonify({'ok': True})

# ─── DB MAINTENANCE ─────────────────────────────────────────
@app.route('/api/admin/vacuum', methods=['POST'])
@handle_errors
def vacuum_db():
    conn = get_conn()
    conn.execute('VACUUM')
    conn.execute('ANALYZE')
    logger.info("VACUUM + ANALYZE completed.")
    return jsonify({'ok': True})

# ─── RUN ─────────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    # threaded=True is required for SSE (each client holds a streaming response)
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
