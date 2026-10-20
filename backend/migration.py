"""Private migration verification and reversible source-asset alias registration.

Only administrator-registered immutable plans are discovered. Transferred bytes
are not Ready until AWS's validated full-object SHA256 or a bounded-memory raw
stream hash matches the inventory. No source object or local record is deleted.
"""
import base64
import hashlib
import json
import os
import re
import threading
import time
from collections import OrderedDict
from botocore.config import Config
from . import db, cloud_data
from .config import aws_session

_PLAN_CACHE = OrderedDict()
_PLAN_LOCK = threading.Lock()
_PLAN_CACHE_LIMIT = 2
RATE = 64 * 1024**2


def initialize():
    with db.tx() as c:
        c.execute('CREATE TABLE IF NOT EXISTS migration_objects(plan_id TEXT NOT NULL,sha256 TEXT NOT NULL,user_id TEXT NOT NULL,state TEXT NOT NULL,receipt TEXT NOT NULL,verified_bytes BIGINT NOT NULL DEFAULT 0,version_id TEXT NOT NULL,lease_until REAL NOT NULL DEFAULT 0,error TEXT,updated REAL NOT NULL,PRIMARY KEY(plan_id,sha256))')
        c.execute('CREATE TABLE IF NOT EXISTS migration_assets(asset_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,dataset_id TEXT NOT NULL,canonical_asset_id TEXT NOT NULL,source_path TEXT NOT NULL,source_record TEXT NOT NULL)')
        c.execute('CREATE INDEX IF NOT EXISTS migration_assets_owner ON migration_assets(user_id,dataset_id)')


def client():
    return aws_session().client('s3', config=Config(connect_timeout=5, read_timeout=60, retries={'max_attempts': 2}))


def _get_json(s3, bucket, key, version=None, maximum=32 * 1024**2):
    request = {'Bucket': bucket, 'Key': key}
    if version:
        request['VersionId'] = version
    response = s3.get_object(**request);stream = response['Body']
    try:
        if response['ContentLength'] > maximum:
            raise ValueError('Migration manifest exceeds its bounded metadata size')
        body = stream.read(maximum + 1)
        if len(body) > maximum:
            raise ValueError('Migration manifest exceeds its bounded metadata size')
        return json.loads(body), hashlib.sha256(body).hexdigest()
    finally:
        stream.close()


def load_plan(registration):
    identity = (registration['bucket'], registration['key'], registration['version_id'], registration['sha256'])
    # Discovery and verification start together. Keep the entire cold fetch,
    # parse and validation under one lock so they never hold duplicate expanded
    # JSON trees. Each manifest is also bounded to 32 MiB by _get_json.
    with _PLAN_LOCK:
        if identity in _PLAN_CACHE:
            plan = _PLAN_CACHE[identity]
            if plan['owner_id'] != registration['owner_id'] or plan['plan_id'] != registration['plan_id']:
                raise ValueError('Immutable migration plan identity verification failed')
            _PLAN_CACHE.move_to_end(identity)
            return plan
        # Evict before parsing, not after a third large tree has been retained.
        while len(_PLAN_CACHE) >= _PLAN_CACHE_LIMIT:
            _PLAN_CACHE.popitem(last=False)
        plan, digest = _get_json(client(), *identity[:3])
        if digest != identity[3] or plan.get('owner_id') != registration['owner_id'] or plan.get('plan_id') != registration['plan_id'] or plan.get('bucket') != registration['bucket']:
            raise ValueError('Immutable migration plan identity verification failed')
        if len(plan.get('objects', [])) > 100000 or sum(o['bytes'] for o in plan['objects']) != plan['total_bytes']:
            raise ValueError('Migration plan totals or object count are invalid')
        plan['_by_sha'] = {o['sha256']: o for o in plan['objects']}
        if len(plan['_by_sha']) != len(plan['objects']):
            raise ValueError('Migration plan has duplicate content identities')
        _PLAN_CACHE[identity] = plan
        return plan


def register_plan(actor, registration):
    required = {'plan_id', 'owner_id', 'bucket', 'key', 'version_id', 'sha256'}
    if set(registration) != required or not re.fullmatch(r'migration_[a-f0-9]{24}', registration['plan_id']) or not re.fullmatch(r'[a-f0-9]{64}', registration['sha256']):
        raise ValueError('Provide an exact versioned migration plan registration')
    if registration['bucket'] != os.environ.get('NGS_BUCKET') or registration['key'] != f"operations/checkpoints/migration/{registration['plan_id']}/plan.json" or registration['version_id'] in ['', 'null']:
        raise ValueError('Migration plan must reside in the private configured OpenNGS cache')
    if not db.query('SELECT id FROM users WHERE id=?', (registration['owner_id'],), one=True):
        raise ValueError('Migration owner does not exist')
    plan = load_plan(registration)
    if plan['total_bytes'] > 500 * 1024**3:
        raise ValueError('Migration exceeds the reviewed 500 GiB batch envelope')
    for obj in plan['objects']:
        prefix = f"tenants/{plan['owner_id']}/datasets/{obj['dataset_id']}/"
        if not re.fullmatch(r'[a-f0-9]{64}', obj['sha256']) or not obj['key'].startswith(prefix) or '/..' in obj['key'] or obj['bytes'] <= 0:
            raise ValueError('Migration object escaped the owner-private dataset scope')
        if any(a['user_id'] != plan['owner_id'] or a['bytes'] != obj['bytes'] for a in obj['aliases']):
            raise ValueError('Migration aliases have inconsistent ownership or sizes')
    with db.tx() as c:
        c.execute('INSERT INTO controls VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', ('migration:plan:' + plan['plan_id'], db.encode(registration)))
        db.audit(c, actor, 'migration.plan.registered', plan['plan_id'], {'owner': plan['owner_id'], 'bytes': plan['total_bytes'], 'objects': len(plan['objects']), 'version_id': registration['version_id'], 'private_only': True, 'source_deleted': False})
    return {'plan_id': plan['plan_id'], 'bytes': plan['total_bytes'], 'objects': len(plan['objects']), 'state': 'Awaiting transferred objects'}


def discover(limit=100):
    added = 0;s3 = client()
    for row in db.query("SELECT value FROM controls WHERE key LIKE 'migration:plan:%'"):
        registration = json.loads(row['value']);plan = load_plan(registration)
        prefix = f"operations/checkpoints/migration/{plan['plan_id']}/uploaded/"
        cursor_key = 'migration:cursor:' + plan['plan_id'];cursor = db.query('SELECT value FROM controls WHERE key=?', (cursor_key,), one=True)
        request = {'Bucket': plan['bucket'], 'Prefix': prefix, 'MaxKeys': limit}
        if cursor and cursor['value']:
            request['StartAfter'] = cursor['value']
        page = s3.list_objects_v2(**request)
        for entry in page.get('Contents', []):
            digest = entry['Key'].removeprefix(prefix).removesuffix('.json')
            if digest not in plan['_by_sha']:
                raise ValueError('Unknown transfer checkpoint is outside the registered plan')
            if db.query('SELECT sha256 FROM migration_objects WHERE plan_id=? AND sha256=?', (plan['plan_id'], digest), one=True):
                continue
            receipt, _ = _get_json(s3, plan['bucket'], entry['Key'], maximum=16384)
            obj = plan['_by_sha'][digest]
            if any(receipt.get(k) != obj[k] for k in ['sha256', 'key', 'bytes']) or receipt.get('plan_id') != plan['plan_id'] or receipt.get('version_id') in [None, '', 'null']:
                raise ValueError('Transferred object checkpoint differs from its immutable plan')
            with db.tx() as c:
                c.execute("INSERT INTO migration_objects VALUES(?,?,?,'Verifying',?,0,?,0,NULL,?) ON CONFLICT(plan_id,sha256) DO NOTHING", (plan['plan_id'], digest, plan['owner_id'], db.encode(receipt), receipt['version_id'], time.time()))
            added += 1
        progress = {}
        try:
            raw, _ = _get_json(s3, plan['bucket'], f"operations/checkpoints/migration/{plan['plan_id']}/progress.json", maximum=32768)
            if raw.get('plan_id') == plan['plan_id']:
                progress = {k:raw.get(k) for k in ['updated','uploaded_objects','uploaded_bytes','checkpointed_multipart_bytes','transfer_complete']}
                progress['blocked_objects_reported'] = len(raw.get('blocked', []))
        except Exception:
            pass  # Telemetry availability never changes scientific readiness.
        summary = {'plan_id':plan['plan_id'],'owner_id':plan['owner_id'],'total_bytes':plan['total_bytes'],'total_objects':len(plan['objects']),'total_aliases':sum(len(o['aliases']) for o in plan['objects']),'storage_monthly_pretax_usd':round(plan['total_bytes']/1024**3*.023,4),'aws_copy_bytes':sum(o['bytes'] for o in plan['objects'] if o.get('cloud_source')),'progress':progress,'checked_at':time.time(),'source_deleted':False}
        with db.tx() as c:
            c.execute('INSERT INTO controls VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', ('migration:summary:'+plan['plan_id'], db.encode(summary)))
        next_cursor = page['Contents'][-1]['Key'] if page.get('IsTruncated') and page.get('Contents') else ''
        with db.tx() as c:
            c.execute('INSERT INTO controls VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (cursor_key, next_cursor))
    return added


def provenance_warnings(uid, sha256=None):
    prefix = 'migration:provenance:' + uid + ':'
    if sha256:
        row = db.query('SELECT value FROM controls WHERE key=?', (prefix + sha256,), one=True)
        return json.loads(row['value']) if row else []
    rows = db.query('SELECT value FROM controls WHERE substr(key,1,?)=?', (len(prefix), prefix))
    return [warning for row in rows for warning in json.loads(row['value'])]


def record_provenance_warning(actor, uid, sha256, warning):
    """Internal operator metadata correction; never changes source/object bytes."""
    if not re.fullmatch('[0-9a-f]{64}', sha256) or not db.query('SELECT id FROM users WHERE id=?', (uid,), one=True):
        raise ValueError('Provenance warning requires an existing owner and exact source checksum')
    if not isinstance(warning, dict) or not warning.get('code') or not warning.get('message'):
        raise ValueError('Provenance warning requires a code and explanation')
    warning = {**warning, 'source_sha256': sha256, 'release_qualified': False}
    key = 'migration:provenance:' + uid + ':' + sha256
    warnings = [w for w in provenance_warnings(uid, sha256) if w['code'] != warning['code']] + [warning]
    with db.tx() as c:
        c.execute('INSERT INTO controls VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, db.encode(warnings)))
        for dataset in c.execute('SELECT id,manifest FROM datasets WHERE user_id=? AND sha256=?', (uid, sha256)).fetchall():
            manifest = json.loads(dataset['manifest']);manifest['provenance_warnings'] = warnings
            c.execute('UPDATE datasets SET manifest=? WHERE id=? AND user_id=?', (db.encode(manifest), dataset['id'], uid))
            for asset in c.execute('SELECT a.id,a.metadata FROM assets a JOIN migration_assets m ON m.asset_id=a.id AND m.user_id=a.user_id WHERE m.user_id=? AND m.dataset_id=?', (uid, dataset['id'])).fetchall():
                metadata = json.loads(asset['metadata']);metadata['provenance_warnings'] = warnings
                c.execute('UPDATE assets SET metadata=? WHERE id=? AND user_id=?', (db.encode(metadata), asset['id'], uid))
        db.audit(c, actor, 'migration.provenance.warning', sha256, {'owner': uid, 'warning': warning, 'source_identity_changed': False})
    return warnings


def import_manifest(plan, obj, receipt, verified_sha256, verification_method):
    """Internal verifier commit. No public HTTP route accepts this proof."""
    if verified_sha256 != obj['sha256'] or verification_method not in ['s3-full-object-sha256', 'raw-cloud-sha256']:
        raise ValueError('Verified destination SHA256 differs from the source inventory')
    uid = plan['owner_id'];aliases = obj['aliases'];canonical = aliases[0]
    uri = 's3://' + plan['bucket'] + '/' + obj['key'];now = time.time()
    warnings = provenance_warnings(uid, obj['sha256'])
    manifest = {'format': canonical['format'], 'family': canonical['family'], 'assembly': canonical['assembly'], 'migration_plan': plan['plan_id'], 'source_aliases': [a['id'] for a in aliases], 'source_sha256': obj['sha256'], 'verification_method': verification_method, 'private_owner_only': True, 'provenance_warnings': warnings, 'licensing': 'Source licence and use rights remain subject to owner review; migration does not grant redistribution or commercial rights'}
    with db.tx() as c:
        old_dataset = c.execute('SELECT user_id,sha256,version_id FROM datasets WHERE id=?', (obj['dataset_id'],)).fetchone()
        if old_dataset and (old_dataset['user_id'] != uid or old_dataset['sha256'] != obj['sha256'] or old_dataset['version_id'] != receipt['version_id']):
            raise ValueError('Existing dataset registration has a different owner or immutable object identity')
        c.execute("INSERT INTO datasets VALUES(?,?,?,?,?,?,'Verifying',?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET updated=excluded.updated", (obj['dataset_id'], uid, canonical['id'], canonical['name'], obj['bytes'], obj['sha256'], obj['key'], receipt['version_id'], db.encode(manifest), now, now))
    # Keep each global write-lock interval short even when thousands of source
    # names deduplicate to one object. Ready is committed only after all aliases.
    for offset in range(0,len(aliases),50):
        with db.tx() as c:
            for alias in aliases[offset:offset+50]:
                existing = c.execute('SELECT * FROM assets WHERE id=?', (alias['id'],)).fetchone()
                if existing and existing['user_id'] != uid:
                    raise ValueError('Source asset ID belongs to a different workspace')
                metadata = {**alias['metadata'], **(json.loads(existing['metadata']) if existing else {}), 'cloud_dataset_id': obj['dataset_id'], 'cloud_asset_id': canonical['id'], 'migration_plan': plan['plan_id'], 'migration_source_path': alias['path'], 'private_owner_only': True}
                if warnings:
                    metadata['provenance_warnings'] = warnings
                is_primary = alias['id'] == canonical['id']
                if is_primary:
                    metadata.update(dataset_id=obj['dataset_id'], sha256=obj['sha256'], version_id=receipt['version_id'], uploaded=True)
                path = uri if is_primary else (existing['path'] if existing else alias['path'])
                c.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET path=excluded.path,metadata=excluded.metadata', (alias['id'], uid, path, alias['name'], alias['format'], alias['bytes'], alias['mtime'], alias['family'], alias['assembly'], db.encode(metadata)))
                c.execute('INSERT INTO migration_assets VALUES(?,?,?,?,?,?) ON CONFLICT(asset_id) DO UPDATE SET dataset_id=excluded.dataset_id,canonical_asset_id=excluded.canonical_asset_id,source_record=excluded.source_record', (alias['id'], uid, obj['dataset_id'], canonical['id'], alias['path'], db.encode(alias)))
    with db.tx() as c:
        c.execute("UPDATE datasets SET state='Ready',updated=? WHERE id=? AND user_id=?", (time.time(),obj['dataset_id'],uid))
        c.execute("UPDATE migration_objects SET state='Ready',verified_bytes=?,lease_until=0,error=NULL,updated=? WHERE plan_id=? AND sha256=?", (obj['bytes'], now, plan['plan_id'], obj['sha256']))
        db.audit(c, 'migration-verifier', 'migration.object.verified', obj['dataset_id'], {'bytes': obj['bytes'], 'sha256': obj['sha256'], 'version_id': receipt['version_id'], 'alias_count': len(aliases), 'verification_method': verification_method, 'private_only': True, 'source_deleted': False})
    return {'dataset_id': obj['dataset_id'], 'state': 'Ready', 'aliases': len(aliases)}


def resolve_asset(uid, row):
    """Return verified canonical AWS content while preserving the user's asset ID."""
    row = dict(row);metadata = json.loads(row['metadata']) if isinstance(row['metadata'], str) else dict(row['metadata'])
    dataset_id = metadata.get('cloud_dataset_id')
    from .public_resource_metadata import enrich as enrich_resource
    if not dataset_id:
        return enrich_resource(row)
    mapping = db.query('SELECT * FROM migration_assets WHERE asset_id=? AND user_id=? AND dataset_id=?', (row['id'], uid, dataset_id), one=True)
    dataset = db.query("SELECT * FROM datasets WHERE id=? AND user_id=? AND state='Ready'", (dataset_id, uid), one=True)
    canonical = db.query('SELECT * FROM assets WHERE id=? AND user_id=?', (mapping['canonical_asset_id'], uid), one=True) if mapping else None
    if not mapping or not dataset or not canonical:
        raise ValueError('Migrated source data is not fully verified and registered')
    cloud_meta = json.loads(canonical['metadata'])
    metadata.update({k: cloud_meta[k] for k in ['dataset_id', 'sha256', 'version_id', 'uploaded']})
    warnings = provenance_warnings(uid, cloud_meta['sha256'])
    if warnings:
        metadata['provenance_warnings'] = warnings
    metadata['migration_source_path'] = mapping['source_path'];metadata['private_owner_only'] = True
    # Companion paths resolve only when that exact source is also Ready in AWS.
    missing = []
    def companion(path):
        if not isinstance(path, str) or path.startswith('s3://'):
            return path
        alias = db.query('SELECT m.canonical_asset_id,d.sha256 FROM migration_assets m JOIN datasets d ON d.id=m.dataset_id WHERE m.user_id=? AND m.source_path=? AND d.state=\'Ready\'', (uid, path), one=True)
        if not alias:
            missing.append(path)
            return None
        ready = db.query('SELECT path FROM assets WHERE id=? AND user_id=?', (alias['canonical_asset_id'], uid), one=True)
        return ready['path'] if ready else None
    for field in ['mate', 'index', 'dictionary', 'reference']:
        if field in metadata:
            converted = companion(metadata[field])
            if converted:
                metadata[field] = converted
            else:
                metadata.pop(field)
    if metadata.get('index_paths'):
        metadata['index_paths'] = [value for path in metadata['index_paths'] if (value := companion(path))]
    metadata['migration_companions_pending'] = len(missing)
    return enrich_resource({**row, 'path': canonical['path'], 'metadata': db.encode(metadata)})


def authorize_source(uid, uri, version_id=None, sha256=None):
    """Authorize only exact Ready migration objects owned by this tenant.

    Returns frozen Bucket/Key/VersionId/sha256/bytes or None for a non-migration
    URI. Callers must retain their ordinary tenant-bucket checks for None.
    """
    if not isinstance(uri, str) or not uri.startswith('s3://'):
        return None
    asset = db.query('SELECT a.path,m.dataset_id FROM assets a JOIN migration_assets m ON m.canonical_asset_id=a.id AND m.user_id=a.user_id WHERE a.user_id=? AND a.path=? LIMIT 1', (uid, uri), one=True)
    if not asset:
        return None
    dataset = db.query("SELECT * FROM datasets WHERE id=? AND user_id=? AND state='Ready'", (asset['dataset_id'], uid), one=True)
    if not dataset:
        raise ValueError('Migrated source is not Ready for this tenant')
    bucket, key = uri[5:].split('/', 1)
    if bucket != os.environ.get('NGS_BUCKET') or key != dataset['s3_key'] or not key.startswith('tenants/' + uid + '/datasets/') or not dataset['version_id'] or dataset['version_id'] == 'null':
        raise ValueError('Migrated source escaped its verified private cloud identity')
    manifest = json.loads(dataset['manifest'])
    if manifest.get('verification_method') not in ['raw-cloud-sha256', 's3-full-object-sha256'] or not manifest.get('private_owner_only'):
        raise ValueError('Migration raw content verification is incomplete')
    if version_id is not None and version_id != dataset['version_id']:
        raise ValueError('Migrated source version differs from its committed dataset')
    if sha256 is not None and sha256.removeprefix('sha256:') != dataset['sha256']:
        raise ValueError('Migrated source checksum differs from its committed dataset')
    return {'Bucket': bucket, 'Key': key, 'VersionId': dataset['version_id'], 'sha256': dataset['sha256'], 'bytes': dataset['bytes'], 'dataset_id': dataset['id']}


def source_manifest(uid, item):
    meta = item.get('metadata', {})
    paths = [item.get('path')] + [meta.get(k) for k in ['mate', 'index', 'dictionary', 'reference']] + meta.get('index_paths', [])
    result = []
    for path in dict.fromkeys(p for p in paths if isinstance(p, str)):
        proof = authorize_source(uid, path, meta.get('version_id') if path == item.get('path') else None, item.get('digest') if path == item.get('path') else None)
        if proof:
            result.append(proof)
    return result


def verify_one(plan_id=None, sha256=None):
    # Operator priority is an exact registered object, never a new URI or a
    # reset of somebody else's lease/retry counter. The normal daemon uses the
    # oldest eligible record; both paths share the same transactional claim.
    if (plan_id is None) != (sha256 is None):
        raise ValueError('Priority verification requires both plan and SHA-256')
    if plan_id is not None and (not re.fullmatch(r'migration_[a-f0-9]{24}', plan_id)
                               or not re.fullmatch(r'[a-f0-9]{64}', sha256)):
        raise ValueError('Priority verification requires an exact migration identity')
    with db.tx() as c:
        where = ' AND plan_id=? AND sha256=?' if plan_id is not None else ''
        args = (time.time(), plan_id, sha256) if plan_id is not None else (time.time(),)
        row = c.execute("SELECT * FROM migration_objects WHERE state='Verifying' AND lease_until<?" + where + " ORDER BY updated LIMIT 1", args).fetchone()
        if not row:
            return None
        row = dict(row)
        receipt = json.loads(row['receipt']);attempts = int(receipt.get('_verification_attempts', 0))
        if attempts >= 3:
            c.execute("UPDATE migration_objects SET state='Blocked',error='Three bounded verification attempts were exhausted; operator reconciliation is required',updated=? WHERE plan_id=? AND sha256=?", (time.time(), row['plan_id'], row['sha256']))
            return {'plan_id': row['plan_id'], 'sha256': row['sha256'], 'state': 'Blocked', 'reason': 'Bounded verification retries exhausted'}
        receipt['_verification_attempts'] = attempts + 1;row['receipt'] = db.encode(receipt)
        c.execute('UPDATE migration_objects SET lease_until=?,receipt=?,updated=? WHERE plan_id=? AND sha256=?', (time.time() + 120, row['receipt'], time.time(), row['plan_id'], row['sha256']))
    registration = db.query('SELECT value FROM controls WHERE key=?', ('migration:plan:' + row['plan_id'],), one=True)
    if not registration:
        raise ValueError('Migration plan authorization disappeared')
    plan = load_plan(json.loads(registration['value']));obj = plan['_by_sha'][row['sha256']];receipt = json.loads(row['receipt']);s3 = client()
    try:
        request = {'Bucket': plan['bucket'], 'Key': obj['key'], 'VersionId': receipt['version_id']}
        head = s3.head_object(**request, ChecksumMode='ENABLED')
        if head['ContentLength'] != obj['bytes'] or head.get('Metadata', {}).get('sha256') != obj['sha256'] or head.get('VersionId') != receipt['version_id']:
            raise ValueError('Destination identity changed before verification')
        expected = base64.b64encode(bytes.fromhex(obj['sha256'])).decode()
        if head.get('ChecksumType', 'FULL_OBJECT') == 'FULL_OBJECT' and head.get('ChecksumSHA256') == expected:
            return import_manifest(plan, obj, receipt, obj['sha256'], 's3-full-object-sha256')
        response = s3.get_object(**request);stream = response['Body'];digest = hashlib.sha256();total = 0;started = time.monotonic();last = 0
        try:
            for block in iter(lambda: stream.read(4 * 1024**2), b''):
                total += len(block)
                if total > obj['bytes']:
                    raise ValueError('Cloud stream exceeded its exact declared size')
                digest.update(block)
                delay = total / RATE - (time.monotonic() - started)
                if delay > 0:
                    time.sleep(min(delay, 1))
                if time.monotonic() - last > 15:
                    last = time.monotonic()
                    with db.tx() as c:
                        c.execute('UPDATE migration_objects SET verified_bytes=?,lease_until=?,updated=? WHERE plan_id=? AND sha256=?', (total, time.time() + 120, time.time(), row['plan_id'], row['sha256']))
        finally:
            stream.close()
        if total != obj['bytes']:
            raise ValueError('Cloud object was truncated during raw checksum verification')
        return import_manifest(plan, obj, receipt, digest.hexdigest(), 'raw-cloud-sha256')
    except Exception as error:
        # Connectivity failures remain resumable; integrity/ownership errors need
        # explicit reconciliation and never become apparently Ready datasets.
        state = 'Blocked' if isinstance(error, ValueError) else 'Verifying'
        with db.tx() as c:
            c.execute('UPDATE migration_objects SET state=?,lease_until=?,error=?,updated=? WHERE plan_id=? AND sha256=?', (state, time.time() + 300, str(error)[:500], time.time(), row['plan_id'], row['sha256']))
        return {'plan_id': row['plan_id'], 'sha256': row['sha256'], 'state': state, 'reason': str(error)[:300]}


def status(uid=None):
    rows = db.query('SELECT plan_id,state,COUNT(*) AS objects,COALESCE(SUM(verified_bytes),0) AS verified_bytes FROM migration_objects' + (' WHERE user_id=?' if uid else '') + ' GROUP BY plan_id,state', (uid,) if uid else ())
    for row in rows:row['objects']=int(row['objects']);row['verified_bytes']=int(row['verified_bytes'])
    plans = [json.loads(r['value']) for r in db.query("SELECT value FROM controls WHERE key LIKE 'migration:summary:%'")]
    plans = [p for p in plans if uid is None or p['owner_id']==uid]
    for plan in plans:
        progress=plan.get('progress',{})
        plan.update(uploaded_bytes=progress.get('uploaded_bytes') or 0,uploaded_objects=progress.get('uploaded_objects') or 0,transfer_complete=bool(progress.get('transfer_complete')),transfer_updated=progress.get('updated'))
        ready = next((r for r in rows if r['plan_id']==plan['plan_id'] and r['state']=='Ready'), {})
        plan['verified_objects'] = ready.get('objects',0);plan['verified_bytes'] = ready.get('verified_bytes',0)
        plan['complete'] = plan['verified_objects']==plan['total_objects'] and plan['verified_bytes']==plan['total_bytes']
        plan.pop('owner_id',None)
    return {'plans':plans, 'objects': rows, 'provenance_warnings': provenance_warnings(uid), 'scope': 'Private cloud acquisition with exact source identity. Ready requires immutable AWS object versions and raw SHA256 verification. Provenance and original asset bindings are preserved; scientific dependency compatibility and use rights remain separate checks.'}


def refresh_loop(stop):
    """Refresh transfer telemetry even while a multi-gigabyte SHA stream runs."""
    while not stop.is_set():
        try:
            discover(500)
        except Exception as error:
            print(db.encode({'event': 'migration.discovery.retry', 'exception': type(error).__name__}), flush=True)
        stop.wait(30)


initialize()

if __name__ == '__main__':
    stop = threading.Event()
    threading.Thread(target=refresh_loop, args=(stop,), daemon=True, name='migration-discovery').start()
    while True:
        result = None
        try:
            result = verify_one()
            if result:
                print(db.encode({'event': 'migration.verification', 'result': result}), flush=True)
        except Exception as error:
            print(db.encode({'event': 'migration.retry', 'exception': type(error).__name__}), flush=True)
        time.sleep(.02 if result else 2)
