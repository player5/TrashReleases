"""Administrative APIs and staff-facing read APIs for the development core.

Uses core's development store; production persistence must be PostgreSQL.
"""
import re
import secrets
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from . import core

router = APIRouter(prefix='/api')


class NewUser(BaseModel):
    username: str = Field(min_length=3, max_length=40)
    password: str = Field(min_length=12, max_length=200)
    roles: list[str] = Field(default_factory=list)


class UserChange(BaseModel):
    active: bool | None = None
    roles: list[str] | None = None


def validated_roles(roles):
    if len(roles) > 20 or any(not re.fullmatch(r'[a-z][a-z0-9_.:-]{1,63}', x) for x in roles):
        raise HTTPException(400, 'Invalid role')
    return sorted(set(roles))


@router.get('/users')
def list_users(user=Depends(core.require_admin)):
    with core.db() as con:
        people = con.execute('SELECT username,active FROM users ORDER BY username').fetchall()
        return [{'username': p['username'], 'active': bool(p['active']),
                 'roles': [r['role'] for r in con.execute('SELECT role FROM roles WHERE username=? ORDER BY role', (p['username'],))]} for p in people]


@router.post('/users', status_code=201)
def create_user(data: NewUser, user=Depends(core.require_admin)):
    if not re.fullmatch(r'[A-Za-z0-9_.-]{3,40}', data.username):
        raise HTTPException(400, 'Invalid username')
    roles = validated_roles(data.roles)
    with core.DB_LOCK, core.db() as con:
        if con.execute('SELECT 1 FROM users WHERE username=?', (data.username,)).fetchone():
            raise HTTPException(409, 'User exists')
        salt = secrets.token_hex(16)
        con.execute('INSERT INTO users(id,username,salt,password_hash) VALUES(?,?,?,?)',
                    (str(uuid.uuid4()), data.username, salt, core.password_hash(data.password, salt)))
        for role in roles:
            con.execute('INSERT INTO roles VALUES(?,?)', (data.username, role))
        core.audit(con, user, 'user.create', data.username)
    return {'username': data.username, 'roles': roles}


@router.patch('/users/{username}')
def update_user(username: str, data: UserChange, user=Depends(core.require_admin)):
    if data.active is None and data.roles is None:
        raise HTTPException(400, 'No changes')
    roles = validated_roles(data.roles) if data.roles is not None else None
    with core.DB_LOCK, core.db() as con:
        current = con.execute('SELECT username,active FROM users WHERE username=?', (username,)).fetchone()
        if not current:
            raise HTTPException(404, 'User not found')
        was_admin = bool(con.execute('SELECT 1 FROM roles WHERE username=? AND role=?', (username, 'admin')).fetchone())
        will_admin = ('admin' in roles) if roles is not None else was_admin
        will_active = data.active if data.active is not None else bool(current['active'])
        if was_admin and (not will_admin or not will_active):
            others = con.execute('''SELECT 1 FROM users u JOIN roles r ON r.username=u.username
                WHERE r.role='admin' AND u.active=1 AND u.username<>? LIMIT 1''', (username,)).fetchone()
            if not others:
                raise HTTPException(409, 'Cannot disable the last active administrator')
        if data.active is not None:
            con.execute('UPDATE users SET active=? WHERE username=?', (int(data.active), username))
        if roles is not None:
            con.execute('DELETE FROM roles WHERE username=?', (username,))
            for role in roles:
                con.execute('INSERT INTO roles VALUES(?,?)', (username, role))
        if data.active is False or (was_admin and not will_admin):
            con.execute('DELETE FROM sessions WHERE username=?', (username,))
        core.audit(con, user, 'user.update', username)
    return {'updated': True, 'username': username}


@router.get('/jobs')
def list_jobs(status: str | None = None, limit: int = Query(50, ge=1, le=200), user=Depends(core.get_user)):
    with core.db() as con:
        where, params = [], []
        if not core.is_admin(user):
            where.append('owner=?')
            params.append(user)
        if status:
            where.append('status=?')
            params.append(status)
        clause = (' WHERE ' + ' AND '.join(where)) if where else ''
        rows = con.execute('SELECT id,module_id,kind,status,progress,owner,created FROM jobs' + clause +
                           ' ORDER BY created DESC,id DESC LIMIT ?', (*params, limit)).fetchall()
    return [dict(row) for row in rows]


@router.get('/files')
def list_files(limit: int = Query(50, ge=1, le=200), user=Depends(core.get_user)):
    with core.db() as con:
        if core.is_admin(user):
            rows = con.execute('SELECT id,name,size,sha256,module_id,owner,created FROM files ORDER BY created DESC LIMIT ?', (limit,)).fetchall()
        else:
            rows = con.execute('SELECT id,name,size,sha256,module_id,owner,created FROM files WHERE owner=? ORDER BY created DESC LIMIT ?', (user, limit)).fetchall()
    return [dict(row) for row in rows]


@router.get('/audit')
def list_audit(limit: int = Query(100, ge=1, le=500), user=Depends(core.require_admin)):
    with core.db() as con:
        rows = con.execute('SELECT id,actor,action,subject,created FROM audit ORDER BY created DESC,id DESC LIMIT ?', (limit,)).fetchall()
    return [dict(row) for row in rows]


class NotificationIn(BaseModel):
    recipient: str = Field(min_length=3, max_length=40)
    message: str = Field(min_length=1, max_length=2000)


@router.post('/notifications', status_code=201)
def send_notification(data: NotificationIn, user=Depends(core.require_admin)):
    notification_id = str(uuid.uuid4())
    with core.DB_LOCK, core.db() as con:
        if not con.execute('SELECT 1 FROM users WHERE username=? AND active=1', (data.recipient,)).fetchone():
            raise HTTPException(404, 'Recipient not found')
        con.execute('INSERT INTO notifications(id,recipient,message,actor,created,read_at) VALUES(?,?,?,?,?,NULL)',
                    (notification_id, data.recipient, data.message, user, int(time.time())))
        core.audit(con, user, 'notification.send', notification_id)
    return {'id': notification_id}


@router.get('/notifications')
def list_notifications(user=Depends(core.get_user)):
    with core.db() as con:
        rows = con.execute('SELECT id,message,actor,created,read_at FROM notifications WHERE recipient=? ORDER BY created DESC LIMIT 100', (user,)).fetchall()
    return [dict(row) for row in rows]


@router.post('/notifications/{notification_id}/read')
def read_notification(notification_id: str, user=Depends(core.get_user)):
    with core.DB_LOCK, core.db() as con:
        result = con.execute('UPDATE notifications SET read_at=COALESCE(read_at,?) WHERE id=? AND recipient=?',
                             (int(time.time()), notification_id, user))
        if not result.rowcount:
            raise HTTPException(404)
    return {'read': True}