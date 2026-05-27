/**
 * realtime_client.js
 * วางไว้ใน static/ แล้ว import ใน index.html
 *
 * ใช้งาน:
 *   const rt = new RealtimeClient(tripId, currentUser);
 *   rt.on('expenses_changed', ({ expenses }) => renderExpenses(expenses));
 *   rt.on('members_changed',  ({ members })  => renderMembers(members));
 *   rt.on('notifications_new',() => fetchNotifications());
 *   rt.on('online_changed',  ({ online })   => renderOnline(online));
 *   rt.on('snapshot',        (data)         => renderAll(data));
 *   rt.connect();
 *   // เมื่อออกจากหน้า / เปลี่ยน trip
 *   rt.disconnect();
 */

class RealtimeClient {
  constructor(tripId, username) {
    this.tripId   = tripId;
    this.username = username;
    this._handlers = {};
    this._es       = null;
    this._heartbeatTimer = null;
    this._reconnectDelay = 2000;   // ms – doubles on each failure, max 30 s
  }

  // ── public ──────────────────────────────────────────────
  on(event, fn) {
    (this._handlers[event] = this._handlers[event] || []).push(fn);
    return this;
  }

  connect() {
    this._openSSE();
    this._startHeartbeat();
  }

  disconnect() {
    clearInterval(this._heartbeatTimer);
    if (this._es) { this._es.close(); this._es = null; }
  }

  // ── SSE ─────────────────────────────────────────────────
  _openSSE() {
    const url = `/api/trips/${this.tripId}/stream?user=${encodeURIComponent(this.username)}`;
    const es  = new EventSource(url);
    this._es  = es;

    // Named events the server pushes
    const named = ['snapshot','expenses_changed','members_changed',
                   'notifications_new','online_changed','ping'];
    named.forEach(evt => {
      es.addEventListener(evt, e => {
        let data = {};
        try { data = JSON.parse(e.data); } catch (_) {}
        (this._handlers[evt] || []).forEach(fn => fn(data));
      });
    });

    es.onerror = () => {
      es.close();
      this._es = null;
      console.warn(`[SSE] connection lost – reconnecting in ${this._reconnectDelay}ms`);
      setTimeout(() => {
        this._reconnectDelay = Math.min(this._reconnectDelay * 2, 30_000);
        this._openSSE();
      }, this._reconnectDelay);
    };

    es.onopen = () => { this._reconnectDelay = 2000; };  // reset on success
  }

  // ── Heartbeat (online status) ────────────────────────────
  _startHeartbeat() {
    const send = () =>
      fetch('/api/heartbeat', {
        method : 'POST',
        headers: { 'Content-Type': 'application/json' },
        body   : JSON.stringify({ name: this.username, trip_id: this.tripId }),
      }).catch(() => {});   // swallow network errors silently

    send();                              // immediate first ping
    this._heartbeatTimer = setInterval(send, 10_000);   // every 10 s
  }
}
