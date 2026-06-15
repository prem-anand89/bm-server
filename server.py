#!/usr/bin/env python3
"""
Beyond Mechanics — Cloud SQLite Server
========================================
Works on Railway, Render, Fly.io, or any Linux VPS.
Also works locally (python3 server.py).

The server does two things:
  1. Serves BM_Patient_Assessment.html at /
  2. Provides a REST API for patient data sync

SECURITY: Set a secret token in the SYNC_TOKEN environment variable.
  - Railway / Render: set it in the dashboard under Environment Variables
  - Locally: export SYNC_TOKEN=yourpassword  then  python3 server.py
  - In the app: paste your full URL as  https://yourapp.railway.app?token=yourpassword
    (the app reads the token from the URL automatically)

DATABASE:
  - Cloud: uses /tmp/bm_database.db  (resets on redeploy — use sync+export for backup)
  - Local: uses bm_database.db next to this file
"""

import json
import sqlite3
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

# ── Config ────────────────────────────────────────────────────────────────────

PORT       = int(os.environ.get('PORT', 4242))
SYNC_TOKEN = os.environ.get('SYNC_TOKEN', '')   # empty = no auth (local only)
IS_CLOUD   = os.environ.get('RAILWAY_ENVIRONMENT') or os.environ.get('RENDER') or os.environ.get('FLY_APP_NAME')

HERE    = os.path.dirname(os.path.abspath(__file__))
DB_FILE = '/tmp/bm_database.db' if IS_CLOUD else os.path.join(HERE, 'bm_database.db')
APP_FILE = os.path.join(HERE, 'BM_Patient_Assessment.html')

# ── Helpers ───────────────────────────────────────────────────────────────────

def utcnow():
    return datetime.now(timezone.utc).isoformat()

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS patients (
        id TEXT PRIMARY KEY, name TEXT,
        data TEXT NOT NULL, updated_at TEXT NOT NULL
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS clinic (
        key TEXT PRIMARY KEY, value TEXT NOT NULL
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS sync_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        action TEXT, patient_count INTEGER, synced_at TEXT
    )''')
    conn.commit()
    conn.close()
    log(f"DB ready: {DB_FILE}")

def save_patients(patients):
    if not patients: return
    conn = get_db()
    c    = conn.cursor()
    now  = utcnow()
    for p in patients:
        pid  = p.get('id','')
        name = p.get('info',{}).get('name','')
        p['_server_updated'] = now
        c.execute(
            '''INSERT INTO patients (id,name,data,updated_at) VALUES (?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                   name=excluded.name, data=excluded.data,
                   updated_at=excluded.updated_at''',
            (pid, name, json.dumps(p), now)
        )
    conn.commit()
    conn.close()

def load_patients():
    conn = get_db()
    c    = conn.cursor()
    c.execute('SELECT data FROM patients ORDER BY name ASC')
    rows = c.fetchall()
    conn.close()
    out = []
    for row in rows:
        try: out.append(json.loads(row['data']))
        except: pass
    return out

def save_clinic(clinic):
    conn = get_db()
    c    = conn.cursor()
    for key, val in clinic.items():
        c.execute(
            'INSERT INTO clinic (key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            (key, str(val))
        )
    conn.commit()
    conn.close()

def load_clinic():
    conn = get_db()
    c    = conn.cursor()
    c.execute('SELECT key,value FROM clinic')
    rows = c.fetchall()
    conn.close()
    return {row['key']: row['value'] for row in rows}

def log_sync(action, count):
    conn = get_db()
    conn.execute(
        'INSERT INTO sync_log (action,patient_count,synced_at) VALUES (?,?,?)',
        (action, count, utcnow())
    )
    conn.commit()
    conn.close()

def merge_patients(existing_list, incoming_list):
    """Timestamp-based merge — newest version of each patient wins."""
    by_id = {p['id']: p for p in existing_list}
    for cp in incoming_list:
        pid = cp.get('id','')
        if not pid: continue
        sp = by_id.get(pid)
        if sp is None:
            by_id[pid] = cp
        else:
            ct = cp.get('_local_updated') or cp.get('_server_updated') or '0'
            st = sp.get('_server_updated') or sp.get('_local_updated') or '0'
            if ct > st:
                by_id[pid] = cp
    result = list(by_id.values())
    result.sort(key=lambda p: (p.get('info') or {}).get('name') or '')
    return result

# ── HTTP Handler ──────────────────────────────────────────────────────────────

class BMHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        log(fmt % args)

    def get_token_from_request(self):
        """Extract token from query string: ?token=xxx"""
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        return qs.get('token', [''])[0]

    def check_auth(self):
        """Returns True if auth passes. If SYNC_TOKEN is empty, all pass."""
        if not SYNC_TOKEN:
            return True
        return self.get_token_from_request() == SYNC_TOKEN

    def cors(self):
        self.send_header('Access-Control-Allow-Origin',  '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Token')

    def send_json(self, data, status=200):
        body = json.dumps(data).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.cors()
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, body_bytes, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body_bytes)))
        self.cors()
        self.end_headers()
        self.wfile.write(body_bytes)

    def read_body(self):
        length = int(self.headers.get('Content-Length', 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def do_OPTIONS(self):
        self.send_response(200)
        self.cors()
        self.end_headers()

    def do_GET(self):
        clean = urlparse(self.path).path.rstrip('/')

        # Serve app HTML — public (no auth needed to view the app)
        if clean in ('', '/'):
            if os.path.exists(APP_FILE):
                with open(APP_FILE, 'rb') as f:
                    self.send_html(f.read())
            else:
                msg = (
                    b'<h2 style="font-family:sans-serif;color:#0C4A6E">Beyond Mechanics server is running</h2>'
                    b'<p style="font-family:sans-serif">Place <b>BM_Patient_Assessment.html</b> next to server.py to serve the app here.</p>'
                )
                self.send_html(msg, 200)
            return

        # All API endpoints require token if set
        if not self.check_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return

        if clean == '/ping':
            self.send_json({
                'status':  'ok',
                'server':  'Beyond Mechanics',
                'cloud':   bool(IS_CLOUD),
                'patients': len(load_patients()),
                'time':    datetime.now().isoformat()
            })

        elif clean == '/patients':
            patients = load_patients()
            clinic   = load_clinic()
            self.send_json({'patients': patients, 'clinic': clinic, 'count': len(patients)})

        elif clean == '/stats':
            patients     = load_patients()
            total_visits = sum(len(p.get('visits',[])) for p in patients)
            db_size      = round(os.path.getsize(DB_FILE)/1024,1) if os.path.exists(DB_FILE) else 0
            self.send_json({
                'patients':     len(patients),
                'total_visits': total_visits,
                'db_size_kb':   db_size,
                'cloud':        bool(IS_CLOUD),
                'db_file':      DB_FILE
            })

        elif clean.startswith('/patient/'):
            pid      = clean.split('/patient/',1)[1]
            patients = load_patients()
            found    = next((p for p in patients if p.get('id')==pid), None)
            self.send_json(found if found else {'error':'Not found'}, 200 if found else 404)

        else:
            self.send_json({'error': 'Not found'}, 404)

    def do_POST(self):
        clean = urlparse(self.path).path.rstrip('/')

        if not self.check_auth():
            self.send_json({'error': 'Unauthorized'}, 401)
            return

        if clean == '/sync':
            body            = self.read_body()
            client_patients = body.get('patients', [])
            client_clinic   = body.get('clinic', {})

            existing = load_patients()
            merged   = merge_patients(existing, client_patients)
            save_patients(merged)
            if client_clinic:
                save_clinic(client_clinic)

            all_patients = load_patients()
            clinic       = load_clinic()
            log_sync('sync', len(all_patients))
            log(f"SYNC {len(client_patients)} in → {len(all_patients)} total")
            self.send_json({'patients': all_patients, 'clinic': clinic, 'count': len(all_patients)})

        elif clean == '/patient':
            body = self.read_body()
            if body.get('id'):
                save_patients([body])
                log_sync('save_one', 1)
                self.send_json({'ok': True, 'id': body['id']})
            else:
                self.send_json({'error': 'Missing id'}, 400)

        elif clean == '/delete':
            body = self.read_body()
            pid  = body.get('id')
            if pid:
                conn = get_db()
                conn.execute('DELETE FROM patients WHERE id=?', (pid,))
                conn.commit()
                conn.close()
                log_sync('delete', 1)
                self.send_json({'ok': True})
            else:
                self.send_json({'error': 'Missing id'}, 400)

        else:
            self.send_json({'error': 'Not found'}, 404)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    init_db()

    local_ip = '127.0.0.1'
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass

    app_ok = '✓ Found' if os.path.exists(APP_FILE) else '✗ Not found — place next to server.py'
    auth   = f'Token required: {SYNC_TOKEN[:4]}****' if SYNC_TOKEN else 'No auth (add SYNC_TOKEN env var for security)'

    print(f"""
╔══════════════════════════════════════════════════════════╗
║         Beyond Mechanics — Patient Record Server          ║
╠══════════════════════════════════════════════════════════╣
║  Mode    : {'Cloud ☁' if IS_CLOUD else 'Local 🖥'}
║  App HTML: {app_ok}
║  Auth    : {auth}
╠══════════════════════════════════════════════════════════╣
║  Local   : http://localhost:{PORT}
║  Network : http://{local_ip}:{PORT}
╚══════════════════════════════════════════════════════════╝
""", flush=True)

    server = HTTPServer(('0.0.0.0', PORT), BMHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n[Server stopped]')
        server.server_close()
