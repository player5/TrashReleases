"""SEKA WEB core. Development SQLite backend; PostgreSQL migrations owned by SEKA SQL."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
import uuid

from fastapi import FastAPI, Depends, HTTPException, Header, UploadFile, File, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get('SEKA_WEB_DATA', str(ROOT / 'var'))).resolve()
MODULES = Path(os.environ.get('SEKA_WEB_MODULES', str(ROOT / 'modules'))).resolve()
MAX_UPLOAD = 25 * 1024 * 1024
DB_LOCK = threading.RLock()
app = FastAPI(title='SEKA WEB Core', version='0.1.0')


def db():
    DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DATA / 'core.sqlite'), timeout=10)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    return con


def setup():
    with DB_LOCK, db() as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, salt TEXT NOT NULL, password_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS roles(username TEXT NOT NULL, role TEXT NOT NULL, PRIMARY KEY(username,role));
        CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, username TEXT NOT NULL, expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS modules(id TEXT PRIMARY KEY, version TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0, error TEXT);
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, module_id TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0, owner TEXT NOT NULL, created INTEGER NOT NULL, idempotency_key TEXT, UNIQUE(module_id,idempotency_key));
        CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, module_id TEXT NOT NULL, type TEXT NOT NULL, payload TEXT NOT NULL, actor TEXT NOT NULL, created INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS files(id TEXT PRIMARY KEY, name TEXT NOT NULL, storage_key TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL, size INTEGER NOT NULL, owner TEXT NOT NULL, module_id TEXT NOT NULL, created INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS audit(id TEXT PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL, subject TEXT NOT NULL, created INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS notifications(id TEXT PRIMARY KEY, recipient TEXT NOT NULL, message TEXT NOT NULL, actor TEXT NOT NULL, created INTEGER NOT NULL, read_at INTEGER);
        ''')


@app.on_event('startup')
def startup():
    setup()
    (DATA / 'uploads').mkdir(parents=True, exist_ok=True)


def audit(con, actor, action, subject):
    con.execute('INSERT INTO audit VALUES(?,?,?,?,?)', (str(uuid.uuid4()), actor, action, subject, int(time.time())))


def password_hash(password, salt):
    return hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 260000).hex()


def get_user(authorization: str = Header(default='')):
    if not authorization.startswith('Bearer '):
        raise HTTPException(401, 'Bearer token required')
    token_hash = hashlib.sha256(authorization[7:].encode()).hexdigest()
    with db() as con:
        row = con.execute('SELECT u.username FROM sessions s JOIN users u ON u.username=s.username WHERE s.token_hash=? AND s.expires>? AND u.active=1', (token_hash, int(time.time()))).fetchone()
    if not row:
        raise HTTPException(401, 'Invalid or expired session')
    return row['username']


def is_admin(user):
    with db() as con:
        return bool(con.execute('SELECT 1 FROM roles WHERE username=? AND role=?', (user, 'admin')).fetchone())


def require_admin(user=Depends(get_user)):
    if not is_admin(user):
        raise HTTPException(403, 'Admin permission required')
    return user


class Credentials(BaseModel):
    username: str
    password: str


@app.post('/api/bootstrap')
def bootstrap(data: Credentials, request: Request):
    if request.client is None or request.client.host not in ('127.0.0.1', '::1', 'testclient'):
        raise HTTPException(403, 'Bootstrap only via loopback')
    if not re.fullmatch(r'[a-zA-Z0-9_.-]{3,40}', data.username) or len(data.password) < 12:
        raise HTTPException(400, 'Username invalid or password too short')
    with DB_LOCK, db() as con:
        if con.execute('SELECT 1 FROM users LIMIT 1').fetchone():
            raise HTTPException(409, 'Already initialized')
        salt = secrets.token_hex(16)
        con.execute('INSERT INTO users(id,username,salt,password_hash) VALUES(?,?,?,?)', (str(uuid.uuid4()), data.username, salt, password_hash(data.password, salt)))
        con.execute('INSERT INTO roles VALUES(?,?)', (data.username, 'admin'))
        audit(con, data.username, 'bootstrap', 'admin')
    return {'created': True}


@app.post('/api/login')
def login(data: Credentials):
    with DB_LOCK, db() as con:
        row = con.execute('SELECT salt,password_hash FROM users WHERE username=? AND active=1', (data.username,)).fetchone()
        dummy_salt = '00' * 16
        expected = row['password_hash'] if row else password_hash('dummy-password', dummy_salt)
        actual = password_hash(data.password, row['salt'] if row else dummy_salt)
        if not hmac.compare_digest(actual, expected):
            raise HTTPException(401, 'Invalid credentials')
        token = secrets.token_urlsafe(40)
        con.execute('INSERT INTO sessions VALUES(?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), data.username, int(time.time()) + 8*3600))
        audit(con, data.username, 'login', data.username)
    return {'access_token': token, 'token_type': 'bearer', 'expires_in': 8*3600}


@app.post('/api/logout')
def logout(authorization: str = Header(default=''), user=Depends(get_user)):
    with db() as con:
        con.execute('DELETE FROM sessions WHERE token_hash=?', (hashlib.sha256(authorization[7:].encode()).hexdigest(),))
        audit(con, user, 'logout', user)
    return {'ok': True}


@app.get('/api/me')
def me(user=Depends(get_user)):
    with db() as con:
        roles = [r['role'] for r in con.execute('SELECT role FROM roles WHERE username=?', (user,))]
    return {'username': user, 'roles': roles}


def manifests():
    result = {}
    if not MODULES.exists():
        return result
    for directory in sorted(MODULES.iterdir()):
        path = directory / 'manifest.json'
        if not directory.is_dir() or directory.is_symlink() or not path.is_file() or path.is_symlink():
            continue
        try:
            obj = json.loads(path.read_text(encoding='utf-8'))
            mid = obj['id']
            if not re.fullmatch(r'[a-z][a-z0-9_.-]{2,80}', mid) or obj['version'] == '' or not isinstance(obj.get('permissions', []), list):
                continue
            if mid in result:
                continue
            result[mid] = {'id': mid, 'version': str(obj['version']), 'name': str(obj.get('name', mid)), 'permissions': obj.get('permissions', []), 'path': str(directory)}
        except (ValueError, KeyError, TypeError, OSError):
            continue
    return result


@app.get('/api/modules')
def list_modules(user=Depends(get_user)):
    discovered = manifests()
    with db() as con:
        installed = {r['id']: dict(r) for r in con.execute('SELECT * FROM modules')}
    return [{**{k:v for k,v in m.items() if k!='path'}, 'installed': mid in installed, 'enabled': bool(installed.get(mid, {}).get('enabled', 0)), 'installed_version': installed.get(mid, {}).get('version')} for mid,m in discovered.items()]


@app.post('/api/modules/{module_id}/enable')
def enable_module(module_id: str, user=Depends(require_admin)):
    found = manifests().get(module_id)
    if not found:
        raise HTTPException(404, 'Module not discovered')
    # SQL integration must be verified before activation of modules requiring a schema.
    path = Path(found['path']) / 'manifest.json'
    obj = json.loads(path.read_text(encoding='utf-8'))
    if obj.get('database'):
        raise HTTPException(409, 'SQL schema provider not connected; module cannot be activated')
    with DB_LOCK, db() as con:
        con.execute('INSERT INTO modules(id,version,enabled,error) VALUES(?,?,1,NULL) ON CONFLICT(id) DO UPDATE SET version=excluded.version,enabled=1,error=NULL', (module_id, found['version']))
        audit(con, user, 'module.enable', module_id)
    return {'enabled': True, 'module_id': module_id}


@app.post('/api/modules/{module_id}/disable')
def disable_module(module_id: str, user=Depends(require_admin)):
    with DB_LOCK, db() as con:
        con.execute('UPDATE modules SET enabled=0 WHERE id=?', (module_id,))
        audit(con, user, 'module.disable', module_id)
    return {'enabled': False, 'module_id': module_id}


def enabled(module_id):
    with db() as con:
        return bool(con.execute('SELECT 1 FROM modules WHERE id=? AND enabled=1', (module_id,)).fetchone())


class JobIn(BaseModel):
    kind: str = Field(min_length=1, max_length=100)
    payload: dict = Field(default_factory=dict)
    idempotency_key: str | None = None


@app.post('/api/modules/{module_id}/jobs')
def submit_job(module_id: str, data: JobIn, user=Depends(get_user)):
    if not enabled(module_id):
        raise HTTPException(409, 'Module disabled')
    job_id = str(uuid.uuid4())
    with DB_LOCK, db() as con:
        try:
            con.execute('INSERT INTO jobs(id,module_id,kind,payload,status,owner,created,idempotency_key) VALUES(?,?,?,?,?,?,?,?)', (job_id,module_id,data.kind,json.dumps(data.payload), 'queued',user,int(time.time()),data.idempotency_key))
        except sqlite3.IntegrityError:
            row = con.execute('SELECT id FROM jobs WHERE module_id=? AND idempotency_key=?', (module_id,data.idempotency_key)).fetchone()
            if not row:
                raise
            job_id = row['id']
        audit(con, user, 'job.submit', job_id)
    return {'id': job_id, 'status': 'queued'}


@app.get('/api/jobs/{job_id}')
def job_status(job_id: str, user=Depends(get_user)):
    with db() as con:
        row = con.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    if row['owner'] != user and not is_admin(user):
        raise HTTPException(403)
    return {k:row[k] for k in ('id','module_id','kind','status','progress','owner','created')}


class EventIn(BaseModel):
    type: str = Field(min_length=1, max_length=120)
    payload: dict = Field(default_factory=dict)


@app.post('/api/modules/{module_id}/events')
def publish_event(module_id: str, data: EventIn, user=Depends(get_user)):
    if not enabled(module_id):
        raise HTTPException(409, 'Module disabled')
    event_id = str(uuid.uuid4())
    with db() as con:
        con.execute('INSERT INTO events VALUES(?,?,?,?,?,?)', (event_id,module_id,data.type,json.dumps(data.payload),user,int(time.time())))
        audit(con,user,'event.publish',event_id)
    return {'id': event_id}


@app.get('/api/events')
def events(user=Depends(require_admin)):
    with db() as con:
        rows = con.execute('SELECT id,module_id,type,actor,created FROM events ORDER BY created DESC LIMIT 100').fetchall()
    return [dict(r) for r in rows]


@app.post('/api/modules/{module_id}/files')
async def upload_file(module_id: str, file: UploadFile = File(...), user=Depends(get_user)):
    if not enabled(module_id):
        raise HTTPException(409, 'Module disabled')
    file_id = str(uuid.uuid4())
    key = uuid.uuid4().hex
    target = DATA / 'uploads' / key
    digest = hashlib.sha256()
    total = 0
    try:
        with target.open('xb') as out:
            while True:
                chunk = await file.read(1024*1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD:
                    raise HTTPException(413, 'File exceeds size limit')
                digest.update(chunk)
                out.write(chunk)
        with db() as con:
            con.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?,?)', (file_id, Path(file.filename or 'file').name, key,digest.hexdigest(),total,user,module_id,int(time.time())))
            audit(con,user,'file.upload',file_id)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return {'id':file_id,'sha256':digest.hexdigest(),'size':total}


@app.get('/api/files/{file_id}')
def download_file(file_id: str, user=Depends(get_user)):
    with db() as con:
        row = con.execute('SELECT * FROM files WHERE id=?', (file_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    if row['owner'] != user and not is_admin(user):
        raise HTTPException(403)
    path = DATA / 'uploads' / row['storage_key']
    if not path.is_file():
        raise HTTPException(503, 'File missing from storage')
    return FileResponse(path, filename=row['name'], media_type='application/octet-stream')


@app.get('/health')
def health():
    return {'status':'ok','database':'sqlite-development','sql_migrations':'not-connected'}


@app.get('/', response_class=HTMLResponse)
def home():
    return (Path(__file__).parent / 'static' / 'index.html').read_text(encoding='utf-8')


# Import after the core helpers are defined to avoid a circular initialization.
from .management import router as management_router
app.include_router(management_router)