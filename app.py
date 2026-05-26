from flask import Flask, request, jsonify, send_from_directory
import sqlite3, os, datetime

app = Flask(__name__, static_folder='static', template_folder='templates')

DB_FILE = os.environ.get('DB_PATH', 'trip_database.db')

# ─── DB INIT ────────────────────────────────────────────────
def get_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.executescript('''
        CREATE TABLE IF NOT EXISTS all_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            promptpay TEXT, bank_name TEXT, bank_account TEXT
        );
        CREATE TABLE IF NOT EXISTS trips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            trip_date TEXT,
            deleted INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trip_id INTEGER, name TEXT,
            FOREIGN KEY(trip_id) REFERENCES trips(id)
        );
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trip_id INTEGER, description TEXT, amount REAL,
            payer_name TEXT, split_members TEXT, image_blob TEXT,
            FOREIGN KEY(trip_id) REFERENCES trips(id)
        );
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trip_id INTEGER, to_user TEXT, from_user TEXT,
            message TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            is_auto INTEGER DEFAULT 0, is_read INTEGER DEFAULT 0,
            FOREIGN KEY(trip_id) REFERENCES trips(id)
        );
        CREATE TABLE IF NOT EXISTS online_status (
            name TEXT PRIMARY KEY, last_seen DATETIME
        );
    ''')
    conn.commit()
    conn.close()

init_db()

def now_local():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

# ─── SERVE FRONTEND ─────────────────────────────────────────
@app.route('/')
def index():
    return send_from_directory('templates', 'index.html')

# ─── USERS ──────────────────────────────────────────────────
@app.route('/api/users', methods=['GET'])
def get_users():
    conn = get_conn()
    rows = [dict(r) for r in conn.execute('SELECT id,name,promptpay,bank_name,bank_account FROM all_users').fetchall()]
    conn.close()
    return jsonify(rows)

@app.route('/api/users', methods=['POST'])
def create_user():
    data = request.json
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'ชื่อห้ามว่าง'}), 400
    try:
        conn = get_conn()
        conn.execute('INSERT INTO all_users (name) VALUES (?)', (name,))
        conn.commit()
        row = dict(conn.execute('SELECT * FROM all_users WHERE name=?', (name,)).fetchone())
        conn.close()
        return jsonify(row), 201
    except sqlite3.IntegrityError:
        return jsonify({'error': 'ชื่อนี้มีอยู่แล้ว'}), 409

@app.route('/api/users/<name>', methods=['PUT'])
def update_user(name):
    data = request.json
    conn = get_conn()
    conn.execute('UPDATE all_users SET promptpay=?, bank_name=?, bank_account=? WHERE name=?',
                 (data.get('promptpay',''), data.get('bank_name',''), data.get('bank_account',''), name))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

# ─── ONLINE STATUS ───────────────────────────────────────────
@app.route('/api/heartbeat', methods=['POST'])
def heartbeat():
    name = (request.json or {}).get('name','')
    if not name:
        return jsonify({'error': 'no name'}), 400
    conn = get_conn()
    conn.execute('''INSERT INTO online_status(name, last_seen) VALUES(?,?)
                    ON CONFLICT(name) DO UPDATE SET last_seen=?''',
                 (name, now_local(), now_local()))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/online', methods=['GET'])
def online_users():
    conn = get_conn()
    rows = conn.execute(
        "SELECT name FROM online_status WHERE last_seen >= datetime('now','localtime','-15 seconds')"
    ).fetchall()
    conn.close()
    return jsonify([r['name'] for r in rows])

# ─── TRIPS ───────────────────────────────────────────────────
@app.route('/api/trips', methods=['GET'])
def get_trips():
    conn = get_conn()
    rows = [dict(r) for r in conn.execute('SELECT * FROM trips WHERE deleted=0').fetchall()]
    conn.close()
    return jsonify(rows)

@app.route('/api/trips', methods=['POST'])
def create_trip():
    data = request.json
    name = (data.get('name') or '').strip()
    date = data.get('trip_date','')
    if not name:
        return jsonify({'error': 'ชื่อ Event ห้ามว่าง'}), 400
    try:
        conn = get_conn()
        conn.execute('INSERT INTO trips(name, trip_date) VALUES(?,?)', (name, date))
        conn.commit()
        row = dict(conn.execute('SELECT * FROM trips WHERE name=?', (name,)).fetchone())
        conn.close()
        return jsonify(row), 201
    except sqlite3.IntegrityError:
        return jsonify({'error': 'ชื่อ Event ซ้ำ'}), 409

@app.route('/api/trips/<int:trip_id>', methods=['PUT'])
def update_trip(trip_id):
    data = request.json
    conn = get_conn()
    conn.execute('UPDATE trips SET name=?, trip_date=? WHERE id=?',
                 (data.get('name'), data.get('trip_date',''), trip_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/trips/<int:trip_id>', methods=['DELETE'])
def delete_trip(trip_id):
    conn = get_conn()
    conn.execute('UPDATE trips SET deleted=1 WHERE id=?', (trip_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

# ─── MEMBERS ─────────────────────────────────────────────────
@app.route('/api/trips/<int:trip_id>/members', methods=['GET'])
def get_members(trip_id):
    conn = get_conn()
    rows = [r['name'] for r in conn.execute('SELECT name FROM members WHERE trip_id=?', (trip_id,)).fetchall()]
    conn.close()
    return jsonify(rows)

@app.route('/api/trips/<int:trip_id>/members', methods=['POST'])
def add_member(trip_id):
    name = (request.json or {}).get('name','').strip()
    conn = get_conn()
    existing = conn.execute('SELECT id FROM members WHERE trip_id=? AND name=?', (trip_id, name)).fetchone()
    if existing:
        conn.close()
        return jsonify({'error': 'มีแล้ว'}), 409
    conn.execute('INSERT INTO members(trip_id, name) VALUES(?,?)', (trip_id, name))
    conn.commit()
    conn.close()
    return jsonify({'ok': True}), 201

@app.route('/api/trips/<int:trip_id>/members/<name>', methods=['DELETE'])
def remove_member(trip_id, name):
    conn = get_conn()
    conn.execute('DELETE FROM members WHERE trip_id=? AND name=?', (trip_id, name))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

# ─── EXPENSES ────────────────────────────────────────────────
@app.route('/api/trips/<int:trip_id>/expenses', methods=['GET'])
def get_expenses(trip_id):
    conn = get_conn()
    rows = [dict(r) for r in conn.execute('SELECT * FROM expenses WHERE trip_id=?', (trip_id,)).fetchall()]
    conn.close()
    for r in rows:
        r['split_members'] = r['split_members'].split(',') if r['split_members'] else []
    return jsonify(rows)

@app.route('/api/trips/<int:trip_id>/expenses', methods=['POST'])
def add_expense(trip_id):
    data = request.json
    split = data.get('split_members', [])
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        'INSERT INTO expenses(trip_id,description,amount,payer_name,split_members,image_blob) VALUES(?,?,?,?,?,?)',
        (trip_id, data['description'], data['amount'], data['payer_name'], ','.join(split), data.get('image_blob'))
    )
    exp_id = cur.lastrowid

    # auto notifications
    share = data['amount'] / len(split) if split else 0
    for member in split:
        if member != data['payer_name']:
            msg = (f"📌 บิลใหม่: '{data['description']}'\n"
                   f"💰 ยอดรวม {data['amount']:,.2f} บาท\n"
                   f"👤 คนจ่าย: {data['payer_name']}\n"
                   f"💸 ส่วนของคุณ: {share:,.2f} บาท")
            conn.execute(
                "INSERT INTO notifications(trip_id,to_user,from_user,message,is_auto,is_read,timestamp) VALUES(?,?,'ระบบสรุปยอด',?,1,0,?)",
                (trip_id, member, msg, now_local())
            )
    conn.commit()
    conn.close()
    return jsonify({'id': exp_id}), 201

@app.route('/api/expenses/<int:exp_id>', methods=['PUT'])
def update_expense(exp_id):
    data = request.json
    split = data.get('split_members', [])
    conn = get_conn()
    conn.execute(
        'UPDATE expenses SET description=?,amount=?,payer_name=?,split_members=?,image_blob=? WHERE id=?',
        (data['description'], data['amount'], data['payer_name'], ','.join(split), data.get('image_blob'), exp_id)
    )
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/expenses/<int:exp_id>', methods=['DELETE'])
def delete_expense(exp_id):
    conn = get_conn()
    conn.execute('DELETE FROM expenses WHERE id=?', (exp_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

# ─── NOTIFICATIONS ───────────────────────────────────────────
@app.route('/api/trips/<int:trip_id>/notifications/<username>', methods=['GET'])
def get_notifications(trip_id, username):
    conn = get_conn()
    rows = [dict(r) for r in conn.execute(
        '''SELECT * FROM notifications
           WHERE trip_id=? AND (to_user=? OR from_user=?)
           ORDER BY timestamp ASC, id ASC''',
        (trip_id, username, username)
    ).fetchall()]
    conn.close()
    return jsonify(rows)

@app.route('/api/notifications/send', methods=['POST'])
def send_notification():
    data = request.json
    conn = get_conn()
    conn.execute(
        'INSERT INTO notifications(trip_id,to_user,from_user,message,is_auto,is_read,timestamp) VALUES(?,?,?,?,0,0,?)',
        (data['trip_id'], data['to_user'], data['from_user'], data['message'], now_local())
    )
    conn.commit()
    conn.close()
    return jsonify({'ok': True}), 201

@app.route('/api/notifications/read', methods=['POST'])
def mark_read():
    data = request.json
    trip_id = data['trip_id']
    to_user = data['to_user']
    from_user = data.get('from_user')
    conn = get_conn()
    if from_user == 'ระบบสรุปยอด':
        conn.execute(
            'UPDATE notifications SET is_read=1 WHERE trip_id=? AND to_user=? AND is_auto=1',
            (trip_id, to_user)
        )
    else:
        conn.execute(
            'UPDATE notifications SET is_read=1 WHERE trip_id=? AND to_user=? AND from_user=?',
            (trip_id, to_user, from_user)
        )
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/notifications/<int:notif_id>', methods=['DELETE'])
def delete_notification(notif_id):
    conn = get_conn()
    conn.execute('DELETE FROM notifications WHERE id=?', (notif_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

# ─── RUN ─────────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
