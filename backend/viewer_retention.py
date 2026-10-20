"""Retire only derived viewer data whose exact owning run was retention-deleted."""
import hashlib
import json
import re
import sqlite3
import stat
import time
from . import db, retention
from .config import DATA

EXPIRED = 'These prepared results expired under the workspace retention policy. Prepare again if the source remains available; otherwise rerun the saved workflow.'
REGENERATE = 'The prepared index is unavailable. Prepare again to rebuild it from the retained worker output.'


def cache_path(uid, aid):
    """Derive one canonical cache filename; never consume a stored source path."""
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', uid):
        raise ValueError('Invalid viewer cache owner')
    root = DATA.resolve()
    folder = root / 'workbench' / uid
    for parent in [root / 'workbench', folder]:
        if parent.is_symlink() or parent.resolve() != parent:
            raise ValueError('Viewer cache parent must be canonical and cannot be a symbolic link')
    return folder / (hashlib.sha256(aid.encode()).hexdigest() + '.sqlite')


def index_metadata(uid, aid):
    path = cache_path(uid, aid)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(mode) or path.is_symlink():
        raise ValueError('Viewer cache must be a regular application-owned file')
    # Only the application-generated index is parsed, read-only; input datasets
    # and worker-supplied SQLite files never enter this directory.
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)
    try:
        return {key: json.loads(value) for key, value in connection.execute('SELECT key,value FROM meta')}
    finally:
        connection.close()


def dependencies(request):
    uid = request['user_id']
    resources = {request['run_id'], request['asset_id']}
    run = db.query('SELECT graph FROM runs WHERE id=? AND user_id=?', (request['run_id'], uid), one=True)
    if run:
        graph = json.loads(run['graph'])
        resources.update(node.get('params', {}).get('asset_id') for node in graph.get('nodes', [])
                         if node.get('tool') == 'asset.file' and isinstance(node.get('params', {}).get('asset_id'), str))
    return resources


def deleted_dependencies(request):
    with db.tx() as c:
        result = []
        for resource in dependencies(request):
            rid = retention._resource_run(c, request['user_id'], resource)
            if rid and c.execute("SELECT run_id FROM retention_plans WHERE user_id=? AND run_id=? AND state='deleted'",
                                 (request['user_id'], rid)).fetchone():
                result.append(rid)
        return result


def lease_request(request):
    # This shares the retention admission fence. The derived worker output and
    # reference inputs remain protected while an index/JSON result is read.
    for resource in dependencies(request):
        retention.lease(request['user_id'], resource, seconds=900)


def ready(request):
    if request['operation'] != 'variant-index':
        return request.get('result') is not None
    try:
        meta = index_metadata(request['user_id'], request['asset_id'])
        return bool(meta and meta.get('identity') == request['identity'] and
                    meta.get('isolated_run') == request['run_id'])
    except (ValueError, sqlite3.Error, OSError):
        return False


def lease_index(uid, aid):
    request = db.query("SELECT * FROM viewer_requests WHERE user_id=? AND asset_id=? AND operation='variant-index' ORDER BY created DESC,id DESC LIMIT 1",
                       (uid, aid), one=True)
    if request:
        if request['state'] != 'ready':
            raise ValueError(request.get('error') or 'Prepare the results index before viewing it')
        lease_request(request)
        if not ready(request):
            mark_missing(request)
            raise ValueError(REGENERATE)


def mark_missing(request):
    with db.tx() as c:
        changed = c.execute("UPDATE viewer_requests SET state='regenerate',result=NULL,error=?,updated=? WHERE id=? AND user_id=? AND state='ready'",
                            (REGENERATE, time.time(), request['id'], request['user_id']))
        if changed.rowcount:
            db.audit(c, 'viewer-cache', 'viewer.cache.missing', request['id'], {'run': request['run_id']})


def _unlink(uid, aid, request=None):
    path = cache_path(uid, aid)
    if request:
        meta = index_metadata(uid, aid)
        # A later preparation can replace an older index at the same asset path.
        # Expiry of the old worker must never remove that replacement.
        if meta and (meta.get('identity') != request['identity'] or meta.get('isolated_run') != request['run_id']):
            return 0
    paths = [path, path.with_name(path.name + '-wal'), path.with_name(path.name + '-shm'),
             path.with_name(path.name + '-journal')]
    if request:
        paths.append(path.with_suffix('.' + hashlib.sha256(request['id'].encode()).hexdigest()[:20] + '.building'))
    else:
        paths.append(path.with_suffix('.building'))
    removed = 0
    for candidate in paths:
        try:
            item = candidate.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(item.st_mode) or candidate.is_symlink():
            raise ValueError('Refusing non-regular viewer cache cleanup target')
        # The filename was derived above; no glob, source URI or stored path is
        # used. unlink removes exactly this directory entry, never its contents.
        candidate.unlink()
        removed += item.st_size
    return removed


def expire_request(request):
    deleted = deleted_dependencies(request)
    if not deleted:
        return False
    uid = request['user_id']
    with db.tx() as c:
        for resource in dependencies(request):
            rid = retention._resource_run(c, uid, resource)
            if rid and c.execute('SELECT run_id FROM retention_leases WHERE user_id=? AND run_id=? AND expires>?',
                                 (uid, rid, time.time())).fetchone():
                return False
        if c.execute('SELECT h.id FROM viewer_cache_holds h JOIN viewer_requests v ON v.id=h.id AND v.user_id=h.user_id WHERE v.user_id=? AND v.asset_id=? AND h.expires>?',
                     (uid, request['asset_id'], time.time())).fetchone():
            return False
        if c.execute("SELECT id FROM viewer_requests WHERE user_id=? AND asset_id=? AND state='ingesting' AND updated>?",
                     (uid, request['asset_id'], time.time()-1800)).fetchone():
            return False
        changed = c.execute("UPDATE viewer_requests SET state='expired',result=NULL,error=?,updated=? WHERE id=? AND user_id=? AND (state!='expired' OR result IS NOT NULL)",
                            (EXPIRED, time.time(), request['id'], uid))
        if changed.rowcount:
            db.audit(c, 'retention-service', 'viewer.results.expired', request['id'],
                     {'deleted_runs': deleted, 'source_datasets_preserved': True})
        # Index publishers use this same cross-process database fence. Identity
        # inspection and unlink cannot race a newer generation's atomic replace.
        removed = _unlink(uid, request['asset_id'], request) if request['operation'] == 'variant-index' else 0
        c.execute('DELETE FROM viewer_cache_holds WHERE id=? AND expires<=?', (request['id'], time.time()))
        if removed:
            db.audit(c, 'retention-service', 'viewer.cache.cleaned', request['id'], {'bytes': removed, 'deleted_runs': deleted})
    return True


def _page(table, limit):
    key = 'viewer-retention:cursor:' + table
    cursor = db.query('SELECT value FROM controls WHERE key=?', (key,), one=True)
    rows = db.query('SELECT * FROM ' + table + ' WHERE id>? ORDER BY id LIMIT ?', (cursor['value'] if cursor else '', limit))
    return key, rows


def cleanup(limit=40):
    """Bounded durable sweep; failures retry on the next cursor pass.

    Only completed retention plans authorize erasure. JSON state is fenced
    before filesystem deletion; crashes are safe because tombstones and request
    records survive and every subsequent sweep retries the exact cache path.
    """
    if not 1 <= limit <= 100:
        raise ValueError('Viewer cleanup batches are limited to 100 records')
    result = {'requests_expired': 0, 'cache_bytes': 0, 'deferred': 0}
    for table in ['viewer_requests', 'retention_tombstones']:
        key, rows = _page(table, limit)
        for row in rows:
            try:
                if table == 'viewer_requests':
                    result['requests_expired'] += int(expire_request(row) and row['state'] != 'expired')
                    continue
                with db.tx() as c:
                    if not c.execute("SELECT run_id FROM retention_plans WHERE user_id=? AND run_id=? AND state='deleted'",
                                     (row['user_id'], row['run_id'])).fetchone():
                        continue
                    if c.execute('SELECT run_id FROM retention_leases WHERE user_id=? AND run_id=? AND expires>?',
                                 (row['user_id'], row['run_id'], time.time())).fetchone():
                        continue
                    if c.execute('SELECT id FROM assets WHERE id=? AND user_id=?', (row['id'], row['user_id'])).fetchone():
                        continue
                    # A retained uploaded source never has a retention tombstone.
                    removed = _unlink(row['user_id'], row['id'])
                    if removed:
                        db.audit(c, 'retention-service', 'viewer.cache.cleaned', row['id'],
                                 {'bytes': removed, 'deleted_run': row['run_id']})
                    result['cache_bytes'] += removed
            except (ValueError, OSError, sqlite3.Error):
                result['deferred'] += 1
        with db.tx() as c:
            c.execute('INSERT INTO controls VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                      (key, rows[-1]['id'] if len(rows) == limit else ''))
    return result
