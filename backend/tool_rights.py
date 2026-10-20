"""Audited per-tool permission records, separate from funding and credentials."""
import json
import time
from urllib.parse import urlsplit

from . import db, registry


def set_permission(actor, uid, tool_id, payload):
    if actor.get('role') != 'admin' or actor.get('frozen'):
        raise ValueError('An active administrator must review tool permissions')
    allowed = {'approved','confirm_authorization','reason','terms_url'}
    if set(payload)-allowed or type(payload.get('approved')) is not bool:
        raise ValueError('Choose an explicit permission state')
    reason = str(payload.get('reason','')).strip()
    if not 10 <= len(reason) <= 2000:
        raise ValueError('Record the permission basis in at least ten characters')
    spec = registry.definitions(uid).get(tool_id)
    if not spec or spec['license'] not in ['Academic','Review required','Commercial BYOL']:
        raise ValueError('Select a tool requiring a licence review')
    terms = str(payload.get('terms_url','')).strip()
    if payload['approved']:
        parsed = urlsplit(terms)
        if payload.get('confirm_authorization') is not True or parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or len(terms)>2048:
            raise ValueError('Confirm authorization and record the HTTPS source terms or provider agreement reference')
    with db.tx() as c:
        target = c.execute("SELECT id,frozen FROM users WHERE id=? AND role!='retired'",(uid,)).fetchone()
        if not target or target['frozen']:
            raise ValueError('Choose an active workspace')
        row = c.execute('SELECT body FROM settings WHERE user_id=?',(uid,)).fetchone()
        settings = json.loads(row['body']) if row else {}
        approved = set(settings.get('rights_approved',[]))
        if payload['approved']:
            approved.add(tool_id)
        else:
            approved.discard(tool_id)
        settings['rights_approved'] = sorted(approved)
        c.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET body=excluded.body',(uid,db.encode(settings)))
        record = {'approved':payload['approved'],'tool':tool_id,'version':spec['version'],
                  'licence':spec['license'],'terms_url':terms,'reason':reason,'actor':actor['id'],'at':time.time()}
        c.execute('INSERT INTO controls VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',('tool-rights:'+uid+':'+tool_id,db.encode(record)))
        db.audit(c,actor['id'],'tool.permission.recorded' if payload['approved'] else 'tool.permission.revoked',uid,record)
    return {**record,'note':'Records the administrator’s authorization. Runtime qualification, dataset rights, provider credentials and compute allowances remain enforced.'}
