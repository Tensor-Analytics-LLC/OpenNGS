"""Checkpointed expiry of derived AWS outputs, never primary/source datasets.

All deletes name an immutable S3 VersionId under one terminal run's exact
prefix. A database fence prevents new leases/admissions after deletion starts.
Keep input_asset/cache/read routes wired to ensure_available()/lease().
"""
import hashlib
import json
import os
import re
import signal
import threading
import time
from datetime import datetime, timezone
from botocore.config import Config
from botocore.exceptions import ClientError
from . import db, entitlements, cache, demo_jobs
from .config import aws_session

DAY = 86400
GRACE = DAY  # Allows already issued URLs to expire before derived data removal.
TERMINAL = {'succeeded', 'failed', 'cancelled'}  # Interrupted is NOT reconciled.
BLOCKED = {'deleting', 'deleted'}


def initialize():
    with db.tx() as c:
        c.execute('CREATE TABLE IF NOT EXISTS retention_plans(run_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,state TEXT NOT NULL,bucket TEXT NOT NULL,prefix TEXT NOT NULL,expires REAL NOT NULL,cutoff REAL NOT NULL,cursor TEXT NOT NULL,lease_owner TEXT,lease_until REAL NOT NULL DEFAULT 0,created REAL NOT NULL,updated REAL NOT NULL,error TEXT)')
        c.execute('CREATE TABLE IF NOT EXISTS retention_objects(id TEXT PRIMARY KEY,run_id TEXT NOT NULL,bucket TEXT NOT NULL,object_key TEXT NOT NULL,version_id TEXT NOT NULL,bytes BIGINT NOT NULL,etag TEXT NOT NULL,modified REAL NOT NULL,kind TEXT NOT NULL,state TEXT NOT NULL,updated REAL NOT NULL,UNIQUE(bucket,object_key,version_id))')
        c.execute('CREATE INDEX IF NOT EXISTS retention_objects_run ON retention_objects(run_id,state)')
        c.execute('CREATE TABLE IF NOT EXISTS retention_pins(user_id TEXT NOT NULL,run_id TEXT NOT NULL,reason TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(user_id,run_id))')
        c.execute('CREATE TABLE IF NOT EXISTS retention_leases(user_id TEXT NOT NULL,run_id TEXT NOT NULL,expires REAL NOT NULL,PRIMARY KEY(user_id,run_id))')
        c.execute('CREATE TABLE IF NOT EXISTS retention_tombstones(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,run_id TEXT NOT NULL,body TEXT NOT NULL,deleted REAL NOT NULL)')


def s3_client():
    return aws_session().client('s3', config=Config(connect_timeout=5, read_timeout=20, retries={'total_max_attempts': 2}))


def _json(value, default=None):
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return {} if default is None else default


def _row(c, sql, args=()):
    row = c.execute(sql, args).fetchone()
    return dict(row) if row else None


def _resource_run(c, uid, resource):
    """Resolve an owned artifact, run or AWS output URI without trusting callers."""
    row = _row(c, 'SELECT run_id FROM artifacts WHERE id=? AND user_id=?', (resource, uid))
    if row:
        return row['run_id']
    if _row(c, 'SELECT id FROM runs WHERE id=? AND user_id=?', (resource, uid)):
        return resource
    if isinstance(resource, str) and resource.startswith('s3://'):
        match = re.fullmatch(r's3://[^/]+/tenants/([^/]+)/runs/([^/]+)/.+', resource)
        if match and match[1] == uid and _row(c, 'SELECT id FROM runs WHERE id=? AND user_id=?', (match[2], uid)):
            return match[2]
    tombstone = _row(c, 'SELECT run_id FROM retention_tombstones WHERE id=? AND user_id=?', (resource, uid))
    return tombstone['run_id'] if tombstone else None


def ensure_available(uid, resource, connection=None):
    """Reject expired outputs. Call inside admission's existing transaction."""
    if connection is None:
        with db.tx() as c:
            return ensure_available(uid, resource, c)
    rid = _resource_run(connection, uid, resource)
    if not rid:
        return  # Uploaded dataset/source paths have no derived-output retention.
    plan = _row(connection, 'SELECT state FROM retention_plans WHERE run_id=? AND user_id=?', (rid, uid))
    if plan and plan['state'] in BLOCKED:
        raise ValueError('These derived outputs have expired under the workspace retention policy; rerun the saved workflow to regenerate them')


def ensure_outputs(uid, outputs, connection=None):
    """Cache hook; protects primary and companion output paths."""
    if connection is None:
        with db.tx() as c:
            return ensure_outputs(uid, outputs, c)
    for value in _strings(outputs):
        if value.startswith('s3://'):
            ensure_available(uid, value, connection)


def lease(uid, resource, seconds=900):
    if type(seconds) is not int or not 1 <= seconds <= 3600:
        raise ValueError('Read leases must last between 1 and 3600 seconds')
    with db.tx() as c:
        ensure_available(uid, resource, c)
        rid = _resource_run(c, uid, resource)
        if not rid:
            return None
        until = time.time() + seconds
        old = _row(c, 'SELECT expires FROM retention_leases WHERE user_id=? AND run_id=?', (uid, rid))
        until = max(until, old['expires'] if old else 0)
        c.execute('INSERT INTO retention_leases VALUES(?,?,?) ON CONFLICT(user_id,run_id) DO UPDATE SET expires=excluded.expires', (uid, rid, until))
        return {'run_id': rid, 'expires': until}


def pin(uid, rid, pinned, reason='Keep these derived results'):
    if type(pinned) is not bool or not isinstance(reason, str) or not 5 <= len(reason.strip()) <= 500:
        raise ValueError('Provide a pin choice and a reason of 5–500 characters')
    with db.tx() as c:
        if not _row(c, 'SELECT id FROM runs WHERE id=? AND user_id=?', (rid, uid)):
            raise ValueError('Run not found')
        ensure_available(uid, rid, c)
        if pinned:
            c.execute('INSERT INTO retention_pins VALUES(?,?,?,?) ON CONFLICT(user_id,run_id) DO UPDATE SET reason=excluded.reason', (uid, rid, reason.strip(), time.time()))
        else:
            c.execute('DELETE FROM retention_pins WHERE user_id=? AND run_id=?', (uid, rid))
        db.audit(c, uid, 'retention.pinned' if pinned else 'retention.unpinned', rid, {'reason': reason.strip()})
    return {'run_id': rid, 'pinned': pinned}


def configure(actor, enabled, reason):
    if type(enabled) is not bool or not isinstance(reason, str) or len(reason.strip()) < 10:
        raise ValueError('Explain the automatic retention policy change')
    with db.tx() as c:
        c.execute("INSERT INTO controls VALUES('retention:enabled',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ('true' if enabled else 'false',))
        db.audit(c, actor, 'retention.configured', detail={'enabled': enabled, 'reason': reason.strip(), 'source_data_deleted': False, 'grace_seconds': GRACE})
    return status()


def enabled():
    row = db.query("SELECT value FROM controls WHERE key='retention:enabled'", one=True)
    return bool(row and row['value'] == 'true')


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _mentions(value, ids, uri_prefix):
    return any(text in ids or text.startswith(uri_prefix) for text in _strings(value))


def _expiry(run):
    frozen = _json(run['graph']).get('_execution', {}).get('allowance', {})
    current = entitlements.limits(run['user_id'])
    days = max(int(frozen.get('retention_days', current['retention_days'])), current['retention_days'])
    return float(run['finished'] or time.time()) + max(1, days) * DAY + GRACE


def _scope(run):
    uid, rid = run['user_id'], run['id']
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', uid) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', rid):
        raise ValueError('Invalid retention tenant/run identity')
    snapshot = _json(run['graph']).get('_execution', {})
    bucket = snapshot.get('cloud_settings', {}).get('s3_bucket')
    if snapshot.get('access') == 'explore':
        bucket = os.environ.get('NGS_BUCKET') or bucket
    if not bucket or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', bucket):
        raise ValueError('Run lacks a frozen, valid cloud storage identity')
    return bucket, f'tenants/{uid}/runs/{rid}/'


def _blockers(c, run, plan, now):
    uid, rid = run['user_id'], run['id']
    if run['state'] not in TERMINAL or not run['finished']:
        return ['Run has not reached a reconciled terminal state']
    if _expiry(run) > now:
        return ['Assigned retention period has not elapsed']
    if _row(c, 'SELECT run_id FROM usage_holds WHERE run_id=? AND state=\'reserved\'', (rid,)) or _row(c, 'SELECT run_id FROM output_holds WHERE run_id=? AND state=\'reserved\'', (rid,)):
        return ['Accepted work still has an unresolved resource reservation']
    if _row(c, 'SELECT run_id FROM retention_pins WHERE user_id=? AND run_id=?', (uid, rid)):
        return ['Results are explicitly pinned']
    if _row(c, 'SELECT run_id FROM retention_leases WHERE user_id=? AND run_id=? AND expires>?', (uid, rid, now)):
        return ['A result read is in progress']
    uri_prefix = 's3://' + plan['bucket'] + '/' + plan['prefix']
    ids = {rid} | {r['id'] for r in c.execute('SELECT id FROM artifacts WHERE run_id=? AND user_id=?', (rid, uid))}
    # Saved workflows are intentional durable references, including output assets.
    for row in c.execute('SELECT body FROM graphs WHERE user_id=?', (uid,)):
        if _mentions(_json(row['body']), ids, uri_prefix):
            return ['A saved workflow references these results']
    for row in c.execute("SELECT id,graph FROM runs WHERE user_id=? AND state NOT IN ('succeeded','failed','cancelled')", (uid,)):
        if row['id'] == rid or _mentions(_json(row['graph']), ids, uri_prefix):
            return ['Accepted or interrupted work references these results']
    # A cache-reused artifact is independently retained under its consumer run.
    for row in c.execute('SELECT run_id,path,metadata FROM artifacts WHERE user_id=? AND run_id!=?', (uid, rid)):
        if row['path'].startswith(uri_prefix) or _mentions(_json(row['metadata']), ids, uri_prefix):
            return ['Another retained run references these objects']
    for row in c.execute('SELECT path,metadata FROM assets WHERE user_id=?', (uid,)):
        if row['path'].startswith(uri_prefix) or _mentions(_json(row['metadata']), ids, uri_prefix):
            return ['A registered dataset or reference uses these objects']
    days = max(1, entitlements.limits(uid)['retention_days'])
    for row in c.execute('SELECT outputs,last_hit FROM node_cache WHERE user_id=?', (uid,)):
        if row['last_hit'] + days * DAY + GRACE > now and _mentions(_json(row['outputs']), ids, uri_prefix):
            return ['Recently reused computational cache is still retained']
    return []


def _providers_stopped(run):
    ids = set()
    for attempt in db.query('SELECT provider_id,metrics,state FROM attempts WHERE run_id=?', (run['id'],)):
        if attempt['state'] in ['running', 'interrupted']:
            raise ValueError('An execution attempt still needs reconciliation')
        if attempt['provider_id']:
            ids.add(attempt['provider_id'])
        elif _json(attempt['metrics']).get('submission_intent'):
            raise ValueError('A provider submission outcome is unresolved')
    demo = db.query('SELECT provider_id,state FROM demo_tasks WHERE run_id=?', (run['id'],), one=True)
    if demo and demo['provider_id']:
        ids.add(demo['provider_id'])
    elif demo and demo['state'] not in ['published', 'failed', 'cancelled']:
        raise ValueError('Sandbox submission outcome is unresolved')
    # A committed provider-usage record is created only after a stopped job or
    # terminated VM was observed. Retain that evidence after AWS ages job
    # descriptions out of its short-lived control-plane inventory.
    stopped = {a['provider_id'] for a in db.query('SELECT provider_id,metrics FROM attempts WHERE run_id=?', (run['id'],)) if _json(a['metrics']).get('provider_usage', {}).get('provider') in ['Fargate', 'exclusive-vm']}
    if demo and demo['state'] == 'published' and run['state'] == 'succeeded':
        stopped.add(demo['provider_id'])
    session = aws_session()
    for ident in ids - stopped:
        if ident.startswith('i-'):
            instances = [i for r in session.client('ec2').describe_instances(InstanceIds=[ident]).get('Reservations', []) for i in r['Instances']]
            if len(instances) != 1 or instances[0]['State']['Name'] != 'terminated':
                raise ValueError('Scientific VM termination has not been verified')
        else:
            jobs = session.client('batch').describe_jobs(jobs=[ident]).get('jobs', [])
            if len(jobs) != 1 or jobs[0]['status'] not in ['SUCCEEDED', 'FAILED']:
                raise ValueError('Scientific job termination has not been verified')


def discover(limit=20, now=None):
    now = time.time() if now is None else now
    cursor = db.query("SELECT value FROM controls WHERE key='retention:discovery-cursor'", one=True)
    after = _json(cursor['value']) if cursor else {'finished': 0, 'id': ''}
    runs = db.query("SELECT r.* FROM runs r LEFT JOIN retention_plans p ON p.run_id=r.id WHERE r.state IN ('succeeded','failed','cancelled') AND r.finished IS NOT NULL AND p.run_id IS NULL AND (r.finished>? OR (r.finished=? AND r.id>?)) ORDER BY r.finished,r.id LIMIT ?", (after['finished'], after['finished'], after['id'], limit))
    next_cursor = {'finished': runs[-1]['finished'], 'id': runs[-1]['id']} if runs else {'finished': 0, 'id': ''}
    with db.tx() as c:
        c.execute("INSERT INTO controls VALUES('retention:discovery-cursor',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (db.encode(next_cursor),))
    added = 0
    for run in runs:
        if _expiry(run) > now:
            continue
        try:
            bucket, prefix = _scope(run)
        except ValueError:
            continue  # Desktop/legacy data is outside this cloud deletion scope.
        with db.tx() as c:
            c.execute("INSERT INTO retention_plans VALUES(?,?,'inventory',?,?,?,?,?,NULL,0,?,?,NULL) ON CONFLICT(run_id) DO NOTHING", (run['id'], run['user_id'], bucket, prefix, _expiry(run), now - GRACE, '{}', now, now))
            db.audit(c, 'retention-service', 'retention.planned', run['id'], {'expires': _expiry(run), 'bucket': bucket, 'prefix': prefix, 'source_data_deleted': False})
        added += 1
    return added


def _inventory(plan, s3, now):
    request = {'Bucket': plan['bucket'], 'Prefix': plan['prefix'], 'MaxKeys': 500}
    request.update(_json(plan['cursor']))
    page = s3.list_object_versions(**request)
    records = []
    for kind, entries in [('version', page.get('Versions', [])), ('marker', page.get('DeleteMarkers', []))]:
        for entry in entries:
            if not entry['Key'].startswith(plan['prefix']) or not entry.get('VersionId') or entry['VersionId'] == 'null':
                raise ValueError('Retention requires exact versioned objects within the frozen run prefix')
            modified = entry['LastModified'].timestamp()
            if modified > plan['cutoff']:
                raise ValueError('Run prefix changed after its retention inventory cutoff')
            oid = hashlib.sha256(db.encode([plan['bucket'], entry['Key'], entry['VersionId']]).encode()).hexdigest()
            records.append((oid, plan['run_id'], plan['bucket'], entry['Key'], entry['VersionId'], int(entry.get('Size', 0)), entry.get('ETag', ''), modified, kind, 'pending', now))
    cursor = {}
    if page.get('IsTruncated'):
        cursor = {'KeyMarker': page['NextKeyMarker'], 'VersionIdMarker': page['NextVersionIdMarker']}
    with db.tx() as c:
        for record in records:
            c.execute('INSERT INTO retention_objects VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(bucket,object_key,version_id) DO NOTHING', record)
        c.execute('UPDATE retention_plans SET cursor=?,state=?,updated=? WHERE run_id=?', (db.encode(cursor), 'inventory' if cursor else 'ready', now, plan['run_id']))
    return len(records)


def _missing(error):
    return isinstance(error, ClientError) and error.response.get('Error', {}).get('Code') in ['404', 'NoSuchKey', 'NoSuchVersion', 'NotFound']


def _delete_object(item, plan, s3):
    if item['bucket'] != plan['bucket'] or not item['object_key'].startswith(plan['prefix']) or item['version_id'] in ['', 'null']:
        raise ValueError('Deletion object escaped the immutable plan')
    request = {'Bucket': item['bucket'], 'Key': item['object_key'], 'VersionId': item['version_id']}
    if item['kind'] == 'version':
        try:
            head = s3.head_object(**request)
        except ClientError as error:
            if _missing(error):
                return
            raise
        if int(head['ContentLength']) != item['bytes'] or head.get('ETag', '') != item['etag'] or head.get('VersionId') != item['version_id']:
            raise ValueError('Retained object identity does not match its inventory checkpoint')
        if head.get('ObjectLockLegalHoldStatus') == 'ON' or (head.get('ObjectLockRetainUntilDate') and head['ObjectLockRetainUntilDate'] > datetime.now(timezone.utc)):
            raise ValueError('Object has a legal hold or unexpired immutable retention')
    s3.delete_object(**request)  # Never a key-only delete; never governance bypass.
    if item['kind'] == 'version':
        try:
            s3.head_object(**request)
        except ClientError as error:
            if _missing(error):
                return
            raise
        raise ValueError('Exact object version deletion has not been confirmed')


def _finish(plan, now):
    uri_prefix = 's3://' + plan['bucket'] + '/' + plan['prefix']
    with db.tx() as c:
        # Metadata remains in tombstones and immutable audit/history, but no
        # deleted output is presented as available or counted as retained bytes.
        rows = list(c.execute('SELECT * FROM artifacts WHERE run_id=? AND user_id=?', (plan['run_id'], plan['user_id'])))
        for row in rows:
            c.execute('INSERT INTO retention_tombstones VALUES(?,?,?,?,?) ON CONFLICT(id) DO NOTHING', (row['id'], plan['user_id'], plan['run_id'], db.encode(dict(row)), now))
        c.execute('DELETE FROM artifacts WHERE run_id=? AND user_id=?', (plan['run_id'], plan['user_id']))
        for cache in list(c.execute('SELECT cache_key,outputs FROM node_cache WHERE user_id=?', (plan['user_id'],))):
            if _mentions(_json(cache['outputs']), {plan['run_id']}, uri_prefix):
                c.execute('DELETE FROM node_cache WHERE user_id=? AND cache_key=?', (plan['user_id'], cache['cache_key']))
        c.execute('DELETE FROM demo_cache WHERE run_id=?', (plan['run_id'],))
        c.execute("UPDATE retention_plans SET state='deleted',lease_owner=NULL,lease_until=0,error=NULL,updated=? WHERE run_id=?", (now, plan['run_id']))
        physical = int(c.execute("SELECT COALESCE(SUM(bytes),0) FROM retention_objects WHERE run_id=? AND state='deleted'", (plan['run_id'],)).fetchone()[0])
        db.audit(c, 'retention-service', 'retention.completed', plan['run_id'], {'artifacts_retired': len(rows), 'physical_version_bytes_deleted': physical, 'quota_reconciled_from_live_artifact_registry': True, 'source_data_deleted': False, 'past_billing_credits_claimed': False})


def process_one(now=None, max_objects=25, run_id=None):
    now = time.time() if now is None else now
    if not 1 <= max_objects <= 100:
        raise ValueError('Retention deletion batches are limited to 100 exact versions')
    if run_id is not None and not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', run_id):
        raise ValueError('Invalid retention qualification run identity')
    token = db.ident('retention_')
    with db.tx() as c:
        row = _row(c, "SELECT * FROM retention_plans WHERE state!='deleted' AND lease_until<?" + (' AND run_id=?' if run_id else '') + ' ORDER BY updated LIMIT 1', (now, run_id) if run_id else (now,))
        if not row:
            return None
        run = _row(c, 'SELECT * FROM runs WHERE id=? AND user_id=?', (row['run_id'], row['user_id']))
        if not run:
            raise ValueError('Retention plan lost its owning run')
        blockers = _blockers(c, run, row, now)
        if blockers:
            c.execute('UPDATE retention_plans SET error=?,updated=?,lease_until=? WHERE run_id=?', ('; '.join(blockers), now, now + 300, row['run_id']))
            return {'run_id': row['run_id'], 'state': 'protected', 'reason': blockers[0]}
        c.execute('UPDATE retention_plans SET lease_owner=?,lease_until=?,updated=?,error=NULL WHERE run_id=?', (token, now + 900, now, row['run_id']))
    try:
        if row['state'] != 'deleting':
            _providers_stopped(run)
        s3 = s3_client()
        if row['state'] == 'inventory':
            count = _inventory(row, s3, now)
            return {'run_id': row['run_id'], 'state': 'inventory', 'versions': count}
        # The fence and final reference check serialize with admissions/leases.
        with db.tx() as c:
            current = _row(c, 'SELECT * FROM runs WHERE id=?', (row['run_id'],))
            blockers = _blockers(c, current, row, now)
            if blockers:
                raise ValueError('; '.join(blockers))
            c.execute("UPDATE retention_plans SET state='deleting',updated=? WHERE run_id=? AND lease_owner=?", (now, row['run_id'], token))
        items = db.query("SELECT * FROM retention_objects WHERE run_id=? AND state='pending' ORDER BY object_key,version_id LIMIT ?", (row['run_id'], max_objects))
        for item in items:
            _delete_object(item, row, s3)
            with db.tx() as c:
                c.execute("UPDATE retention_objects SET state='deleted',updated=? WHERE id=?", (now, item['id']))
                c.execute('UPDATE retention_plans SET lease_until=?,updated=? WHERE run_id=? AND lease_owner=?', (time.time() + 900, now, row['run_id'], token))
        pending = db.query("SELECT id FROM retention_objects WHERE run_id=? AND state='pending' LIMIT 1", (row['run_id'],), one=True)
        if not pending:
            page = s3.list_object_versions(Bucket=row['bucket'], Prefix=row['prefix'], MaxKeys=1)
            if page.get('Versions') or page.get('DeleteMarkers'):
                # Do not reopen the availability fence after a partial cleanup.
                raise ValueError('Run prefix contains additional versions; operator reconciliation required')
            _finish(row, now)
        return {'run_id': row['run_id'], 'state': 'deleting' if pending else 'deleted', 'versions_deleted': len(items)}
    except Exception as error:
        with db.tx() as c:
            c.execute('UPDATE retention_plans SET error=?,updated=?,lease_until=? WHERE run_id=?', (str(error)[:500], now, now + 300, row['run_id']))
            db.audit(c, 'retention-service', 'retention.deferred', row['run_id'], {'reason': str(error)[:500], 'exception': type(error).__name__})
        return {'run_id': row['run_id'], 'state': 'deferred', 'reason': str(error)[:500]}
    finally:
        with db.tx() as c:
            c.execute('UPDATE retention_plans SET lease_owner=NULL,lease_until=CASE WHEN error IS NULL THEN 0 ELSE lease_until END WHERE run_id=? AND lease_owner=?', (row['run_id'], token))


def reconcile_cancelled_uploads(limit=10):
    """Abort only explicit cancellations; incomplete personal data never ages out."""
    from . import cloud_data, budget, data
    rows = db.query("SELECT t.*,d.s3_key FROM multipart_transfers t JOIN datasets d ON d.id=t.dataset_id WHERE t.state='cancelled' AND t.upload_id IS NOT NULL ORDER BY t.updated LIMIT ?", (limit,))
    done = 0
    for row in rows:
        prefix = f"tenants/{row['user_id']}/datasets/{row['dataset_id']}/"
        if not row['s3_key'].startswith(prefix):
            continue
        try:
            cloud_data.client().abort_multipart_upload(Bucket=data.settings(row['user_id'])['s3_bucket'], Key=row['s3_key'], UploadId=row['upload_id'])
        except ClientError as error:
            if error.response.get('Error', {}).get('Code') != 'NoSuchUpload':
                raise
        with db.tx() as c:
            c.execute("UPDATE multipart_transfers SET upload_id=NULL,updated=? WHERE id=? AND state='cancelled'", (time.time(), row['id']))
            c.execute("UPDATE datasets SET state='cancelled',updated=? WHERE id=? AND state!='Ready'", (time.time(), row['dataset_id']))
            db.audit(c, 'retention-service', 'retention.cancelled_upload_reconciled', row['dataset_id'], {'transfer_id': row['id'], 'source_data_deleted': False})
        budget.reconcile(row['id'], row['user_id'], 'storage')
        done += 1
    return done


def status(uid=None):
    where, args = ('WHERE user_id=?', (uid,)) if uid else ('', ())
    states = db.query('SELECT state,COUNT(*) AS runs FROM retention_plans ' + where + ' GROUP BY state', args)
    versions = db.query("SELECT COALESCE(SUM(o.bytes),0) AS bytes,COUNT(*) AS versions FROM retention_objects o JOIN retention_plans p ON p.run_id=o.run_id WHERE o.state='deleted'" + (' AND p.user_id=?' if uid else ''), args, one=True)
    versions={key:int(value) for key,value in versions.items()}
    plans = db.query('SELECT run_id,state,expires,created,updated,error FROM retention_plans ' + where + ' ORDER BY updated DESC LIMIT 30', args)
    pins = db.query('SELECT run_id,reason,created FROM retention_pins ' + where + ' ORDER BY created DESC LIMIT 100', args)
    return {'enabled': enabled(), 'output_retention_days': entitlements.limits(uid)['retention_days'] if uid else None, 'grace_hours': GRACE // 3600, 'states': states, 'deleted': versions, 'plans': plans, 'pins': pins, 'scope': 'Expired derived AWS run outputs and explicitly cancelled multipart uploads. Uploaded source datasets, shared references, workflow definitions, accounts, vault entries and audit history are preserved. Pinned, actively read, referenced or unreconciled results are protected.'}


def tick():
    if not enabled():
        return {'state': 'disabled'}
    discovered = discover()
    result = process_one()
    cancelled = reconcile_cancelled_uploads()
    from . import remote_viewer, viewer_retention
    viewer = viewer_retention.cleanup()
    with db.tx() as c:
        c.execute('DELETE FROM retention_leases WHERE expires<?', (time.time(),))
        c.execute("INSERT INTO controls VALUES('retention:last-tick',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (db.encode({'at': time.time(), 'discovered': discovered, 'processed': result, 'cancelled_uploads': cancelled, 'viewer_cleanup': viewer}),))
    return {'discovered': discovered, 'processed': result, 'cancelled_uploads': cancelled, 'viewer_cleanup': viewer}


initialize()

if __name__ == '__main__':
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    while not stopping.is_set():
        try:
            print(db.encode({'event': 'retention.tick', 'result': tick()}), flush=True)
        except Exception as error:
            print(db.encode({'event': 'retention.retry', 'exception': type(error).__name__}), flush=True)
        stopping.wait(60)
