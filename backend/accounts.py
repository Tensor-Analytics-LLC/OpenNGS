"""Administrator-managed accounts with immutable scientific provenance."""
import json
import re
import secrets
import time

from . import db
from .config import CLOUD


def admin_only():
    row = db.query("SELECT value FROM controls WHERE key='accounts:admin_only'", one=True)
    return bool(json.loads(row['value'])) if row else CLOUD


def validate_credentials(email, password):
    email = str(email).strip().lower()
    if not re.fullmatch(r'[^\s@]{1,100}@[^\s@]{1,100}', email):
        raise ValueError('Enter a valid email address')
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        raise ValueError('Use a password of 12–256 characters')
    return email


def create(actor, payload):
    from . import security
    if actor['role'] != 'admin':
        raise ValueError('Administrator permission required')
    if set(payload) - {'email', 'password', 'reason'}:
        raise ValueError('Create the account first, then assign its research allowance')
    reason = str(payload.get('reason', '')).strip()
    if len(reason) < 5:
        raise ValueError('Enter an account creation reason')
    email = validate_credentials(payload.get('email', ''), payload.get('password', ''))
    password = security.password_hash(payload['password'])
    uid = db.ident('u_')
    with db.tx() as c:
        if c.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone():
            raise ValueError('An account already uses this email')
        c.execute('INSERT INTO users(id,email,password,role,tier,quota_seconds,created) VALUES(?,?,?,?,?,?,?)',
                  (uid, email, password, 'user', 'explore', 28800, time.time()))
        db.audit(c, actor['id'], 'admin.account.created', uid,
                 {'email': email, 'reason': reason, 'access': 'explore', 'research_funding_granted': False})
    return {'user': security.public_user(db.query('SELECT * FROM users WHERE id=?', (uid,), one=True))}


def remove(actor, uid, reason):
    """Remove login and account access while preserving linked research history.

    Empty accounts are deleted. Accounts with referenced runs/workflows keep
    only a disabled internal principal so foreign keys and audit attribution
    survive; they cannot authenticate or receive new work.
    """
    if actor['role'] != 'admin' or len(str(reason).strip()) < 5:
        raise ValueError('Administrator permission and a removal reason are required')
    if uid == actor['id']:
        raise ValueError('You cannot remove your current administrator account')
    with db.tx() as c:
        row = c.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
        if not row or row['role'] == 'retired':
            return {'removed': True, 'already_removed': True}
        if row['role'] == 'admin' and c.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0] <= 1:
            raise ValueError('The last administrator must be preserved')
        active = c.execute("SELECT id FROM runs WHERE user_id=? AND state NOT IN ('succeeded','failed','cancelled','interrupted') LIMIT 1", (uid,)).fetchone()
        if active:
            raise ValueError('Finish or stop active jobs before removing this account')
        transfer = c.execute("SELECT id FROM multipart_transfers WHERE user_id=? AND state IN ('creating','uploading','completing','verifying') LIMIT 1", (uid,)).fetchone()
        if transfer:
            raise ValueError('Finish or cancel active uploads before removing this account')
        # Cost reservations and historical usage are deliberately untouched.
        c.execute('DELETE FROM sessions WHERE user_id=?', (uid,))
        c.execute('DELETE FROM mfa WHERE user_id=?', (uid,))
        c.execute('UPDATE secrets SET revoked=1 WHERE user_id=?', (uid,))
        c.execute("UPDATE research_allowances SET state='revoked',updated=?,actor=? WHERE user_id=?", (time.time(), actor['id'], uid))
        c.execute("DELETE FROM controls WHERE key=?", ('tier-request:'+uid,))
        linked = sum(c.execute('SELECT COUNT(*) FROM '+table+' WHERE user_id=?', (uid,)).fetchone()[0]
                     for table in ['graphs', 'runs', 'assets', 'artifacts', 'datasets', 'secrets'])
        if linked:
            c.execute("UPDATE users SET email=?,password=?,role='retired',tier='revoked',frozen=1 WHERE id=?",
                      ('removed+'+uid+'@accounts.invalid', 'removed:'+secrets.token_hex(32), uid))
            mode = 'access-removed-history-preserved'
        else:
            c.execute('DELETE FROM settings WHERE user_id=?', (uid,))
            c.execute('DELETE FROM users WHERE id=?', (uid,))
            mode = 'deleted'
        db.audit(c, actor['id'], 'admin.account.removed', uid,
                 {'reason': str(reason).strip(), 'mode': mode, 'previous_email': row['email'],
                  'source_data_deleted': False, 'cost_history_preserved': True})
    return {'removed': True, 'mode': mode, 'history_preserved': True}
