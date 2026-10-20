"""Durable isolated results preparation with a bounded, validated data boundary."""
import hashlib
import json
import os
import time
from contextlib import closing
from pathlib import Path
from . import db, data, entitlements, retention
from .config import CLOUD, DATA, aws_session
from .viewer_protocol import COLUMNS, SCHEMA, INDICES, MAX_LINE, decode, validate_row, validate_metadata, validate_result

INDEX_LIMIT = 512 * 1024**2
TOTAL_INDEX_LIMIT = 4 * 1024**3

def initialize():
    with db.tx() as c:
        c.execute('CREATE TABLE IF NOT EXISTS viewer_requests(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,asset_id TEXT NOT NULL,operation TEXT NOT NULL,identity TEXT NOT NULL,run_id TEXT NOT NULL,state TEXT NOT NULL,result TEXT,error TEXT,created REAL NOT NULL,updated REAL NOT NULL)')
        c.execute('CREATE INDEX IF NOT EXISTS viewer_owner_asset ON viewer_requests(user_id,asset_id,operation)')
        c.execute('CREATE TABLE IF NOT EXISTS viewer_cache_holds(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,bytes BIGINT NOT NULL,expires REAL NOT NULL)')

def required(uid, row):
    if not CLOUD or not row['path'].startswith('s3://'):
        return False
    if row.get('run_id'):
        run = db.query("SELECT r.graph FROM runs r JOIN demo_tasks d ON d.run_id=r.id WHERE r.id=? AND r.user_id=? AND d.state='published'", (row['run_id'], uid), one=True)
        if run and json.loads(run['graph']).get('_execution', {}).get('access') == 'explore':
            return False
    return True

def start(uid, aid, operation='variant-index', params=None, reference_id=None):
    from . import runner, workbench, viewer_retention
    row = workbench.owned(uid, aid)
    if entitlements.restricted(uid):
        raise ValueError('Private results preparation requires an active funded Research Pro allowance')
    profile = data.settings(uid).get('research_profile', {}).get('runtime_profiles', {}).get('results.prepare')
    if not profile:
        raise ValueError('The isolated private-results runtime is not yet published for this workspace')
    if operation not in {'variant-index', 'header', 'pileup', 'omics'}:
        raise ValueError('Unsupported viewer operation')
    params = dict(params or {})
    if set(params) - {'chrom', 'start', 'end', 'x', 'y'}:
        raise ValueError('Unknown viewer parameter')
    sources = [('source', aid)]
    if reference_id:
        sources.append(('reference', reference_id))
    identities = []
    for _, source_id in sources:
        source = workbench.owned(uid, source_id)
        retention.lease(uid, source_id, seconds=900)
        identities.append([source_id, source.get('digest') or source['metadata'].get('sha256'),
                           source['metadata'].get('version_id'), source['bytes']])
    stamp = hashlib.sha256(db.encode([1, operation, params, identities, profile['image']]).encode()).hexdigest()
    request_id = 'viewer_' + hashlib.sha256((uid + stamp).encode()).hexdigest()
    prior = db.query('SELECT * FROM viewer_requests WHERE identity=? AND user_id=? ORDER BY created DESC,id DESC LIMIT 1', (stamp, uid), one=True)
    if prior and (prior['state'] == 'expired' or viewer_retention.deleted_dependencies(prior)):
        # Explicit prepare after expiry creates a separately admitted generation;
        # preserve the prior run and its billing/audit history.
        request_id = 'viewer_' + hashlib.sha256((uid + stamp + prior['id']).encode()).hexdigest()
        prior = db.query('SELECT * FROM viewer_requests WHERE id=? AND user_id=?', (request_id, uid), one=True)
    if prior:
        request_id = prior['id']
        if prior['state'] == 'ready' and not viewer_retention.ready(prior):
            viewer_retention.mark_missing(prior)
            prior['state'] = 'regenerate'
        # An explicit preparation POST may retry only application-side ingestion
        # of already committed worker output. Polling never retries failed work.
        # Preserve the run, image and spend reservation; never resubmit a worker.
        if prior['state'] in {'failed', 'regenerate'}:
            with db.tx() as c:
                finished = c.execute("""SELECT v.state,r.state AS run_state,v.run_id
                  FROM viewer_requests v JOIN runs r ON r.id=v.run_id AND r.user_id=v.user_id
                  WHERE v.id=? AND v.user_id=?""", (request_id,uid)).fetchone()
                if finished and finished['state'] in {'failed', 'regenerate'} and finished['run_state'] == 'succeeded':
                    changed = c.execute("UPDATE viewer_requests SET state='queued',error=NULL,updated=? WHERE id=? AND user_id=? AND state IN ('failed','regenerate')",
                                        (time.time(),request_id,uid))
                    if changed.rowcount:
                        db.audit(c,uid,'viewer.ingestion.retry',aid,{'run':finished['run_id'],'operation':operation,'additional_worker_submission':False})
        return status(uid, request_id)
    allowance = entitlements.limits(uid)
    maximum_output = min(64 * 1024**2, allowance.get('max_storage_bytes', 64 * 1024**2))
    nodes = [{'id': name, 'tool': 'asset.file', 'params': {'asset_id': source_id},
              'position': {'x': 0, 'y': i * 180}} for i, (name, source_id) in enumerate(sources)]
    nodes.append({'id': 'prepare', 'tool': 'results.prepare', 'params': {'operation': operation, **params},
                  'position': {'x': 300, 'y': 0}, 'resources': {'threads': 2, 'memory_mb': 4096,
                  'scratch_mb': min(32768, allowance['max_scratch_mb']),
                  'timeout_seconds': min(900, allowance['max_node_seconds'])}})
    edges = [{'id': 'e' + str(i), 'source': name, 'sourcePort': 'data', 'target': 'prepare',
              'targetPort': 'data' if name == 'source' else 'reference'} for i, (name, _) in enumerate(sources)]
    graph = {'name': 'Private results · ' + operation, 'nodes': nodes, 'edges': edges,
             'maximum_output_bytes': maximum_output}
    user = db.query('SELECT * FROM users WHERE id=?', (uid,), one=True)
    run, _ = runner.admit(user, graph, request_id)
    with db.tx() as c:
        c.execute("INSERT INTO viewer_requests VALUES(?,?,?,?,?,?,'queued',NULL,NULL,?,?) ON CONFLICT(id) DO NOTHING",
                  (request_id, uid, aid, operation, stamp, run['id'], time.time(), time.time()))
        db.audit(c, uid, 'viewer.preparation.admitted', aid, {'run': run['id'], 'operation': operation})
    return status(uid, request_id)

def stream(uid, artifact, maximum):
    """Only raw bytes cross this boundary; no native genomic or SQLite parser."""
    from . import workbench
    retention.lease(uid, artifact['id'], seconds=900)
    args = workbench.object_args(uid, artifact)
    response = aws_session().client('s3').get_object(**args)
    if response['ContentLength'] > maximum:
        response['Body'].close()
        raise ValueError('Prepared results exceed the admitted interchange allowance')
    return response['Body']


def bounded_lines(body, maximum=64 * 1024**2):
    """Read fixed byte chunks without wrapping an HTTP response in BufferedReader.

    urllib3 closes its underlying file after the last received byte. The
    StreamingBody context manager exposes that raw file, which breaks Python's
    buffered readline at EOF. Keep the StreamingBody wrapper and its length
    verification, and enforce both record and complete-response bounds here.
    """
    pending = bytearray()
    received = 0
    while True:
        chunk = body.read(64 * 1024)
        if not chunk:
            if pending:
                yield bytes(pending)
            return
        received += len(chunk)
        if received > maximum:
            raise ValueError('Viewer interchange exceeds its admitted byte allowance')
        pending.extend(chunk)
        while (end := pending.find(b'\n')) >= 0:
            if end + 1 > MAX_LINE:
                raise ValueError('Viewer record exceeds its 1 MiB interchange limit')
            line = bytes(pending[:end+1])
            del pending[:end+1]
            yield line
        if len(pending) > MAX_LINE:
            raise ValueError('Viewer record exceeds its 1 MiB interchange limit')


def ingest_index(uid, request, artifact):
    from . import workbench
    path = workbench.index_path(uid, request['asset_id'])
    request_key=request.get('id') or hashlib.sha256(request['identity'].encode()).hexdigest()
    temp = path.with_suffix('.'+hashlib.sha256(request_key.encode()).hexdigest()[:20]+'.building')
    folder = DATA / 'workbench'
    with db.tx() as c:
        c.execute('DELETE FROM viewer_cache_holds WHERE expires<?',(time.time(),))
        held=c.execute('SELECT COALESCE(SUM(bytes),0) FROM viewer_cache_holds WHERE id<>?',(request_key,)).fetchone()[0]
        used=sum(p.stat().st_size for p in folder.rglob('*') if p.is_file())
        if used+held+INDEX_LIMIT>TOTAL_INDEX_LIMIT or __import__('shutil').disk_usage(folder).free<2*INDEX_LIMIT:
            raise ValueError('Interactive index storage is full. Existing cloud outputs are preserved; expand the funded viewer cache before retrying')
        c.execute('INSERT INTO viewer_cache_holds VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET expires=excluded.expires',
                  (request_key,uid,INDEX_LIMIT,time.time()+1800))
    digest = hashlib.sha256()
    count = 0
    temp.unlink(missing_ok=True)
    try:
        with closing(stream(uid, artifact, 64 * 1024**2)) as body, workbench.connection(temp) as c:
            c.executescript(SCHEMA)
            lines = bounded_lines(body)
            line = next(lines, b'')
            if not line:
                raise ValueError('Viewer interchange is empty')
            digest.update(line)
            metadata = validate_metadata(decode(line))
            sql = 'INSERT INTO variants(' + ','.join(COLUMNS) + ') VALUES(' + ','.join('?' for _ in COLUMNS) + ')'
            pending = []
            for line in lines:
                digest.update(line)
                pending.append(validate_row(decode(line)))
                count += 1
                if count > 100_000_000:
                    raise ValueError('Results exceed the 100M-allele safety ceiling')
                if len(pending) >= 1000:
                    c.executemany(sql, pending); pending = []; c.commit()
                    if temp.stat().st_size > INDEX_LIMIT:
                        raise ValueError('Complete results exceed the 512 MiB interactive index allowance')
            if pending:
                c.executemany(sql, pending)
            if digest.hexdigest() != artifact['digest'].removeprefix('sha256:'):
                raise ValueError('Prepared result content identity changed')
            c.executescript(INDICES)
            source = workbench.owned(uid, request['asset_id'])
            values = {'identity': request['identity'], 'rows': count, 'header': metadata['header'],
                      'assembly': source.get('assembly') or source['metadata'].get('assembly'),
                      'created': time.time(), 'schema': metadata['annotation_schema'],
                      'isolated_run': request['run_id']}
            for key, value in values.items():
                c.execute('INSERT INTO meta VALUES(?,?)', (key, db.encode(value)))
            c.commit()
        if temp.stat().st_size > INDEX_LIMIT:
            raise ValueError('Complete indexed results exceed the interactive cache allowance')
        from . import viewer_retention
        with db.tx() as c:
            for resource in viewer_retention.dependencies({**request, 'user_id': uid}):
                retention.ensure_available(uid, resource, c)
            temp.replace(path)
        return {'rows': count, 'assembly': values['assembly'], 'isolated_run': request['run_id']}
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    finally:
        with db.tx() as c:c.execute('DELETE FROM viewer_cache_holds WHERE id=?',(request_key,))

def finish(uid, request):
    from . import workbench, viewer_retention
    try:
        viewer_retention.lease_request(request)
        candidates = db.query("SELECT * FROM artifacts WHERE user_id=? AND run_id=? AND node_id='prepare'", (uid, request['run_id']))
        if len(candidates) != 1:
            raise ValueError('Isolated viewer did not commit exactly one result')
        artifact = workbench.owned(uid, candidates[0]['id'])
        if artifact['metadata'].get('viewer_operation') != request['operation']:
            raise ValueError('Viewer result operation mismatch')
        if request['operation'] == 'variant-index':
            result = ingest_index(uid, request, artifact)
        else:
            with closing(stream(uid, artifact, 16 * 1024**2)) as body:
                raw = body.read(16 * 1024**2 + 1)
            if hashlib.sha256(raw).hexdigest() != artifact['digest'].removeprefix('sha256:'):
                raise ValueError('Viewer result digest mismatch')
            result = json.loads(raw,parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite viewer value')))
            if result.get('schema_version') != 1 or result.get('operation') != request['operation'] or not isinstance(result.get('result'), dict):
                raise ValueError('Viewer result schema mismatch')
            result = validate_result(request['operation'],result['result'])
            if request['operation'] == 'header':
                source = workbench.owned(uid, request['asset_id'])
                permitted = {'samples', 'info_fields', 'sort_order', 'contig_style', 'contig_count',
                             'contig_preview', 'header_inspected', 'assembly_verification'}
                metadata = {**source['metadata'], **{k: v for k, v in result.get('metadata', {}).items() if k in permitted}}
                with db.tx() as c:
                    for table in ['assets', 'artifacts']:
                        c.execute(f'UPDATE {table} SET metadata=? WHERE id=? AND user_id=?',
                                  (db.encode(metadata), request['asset_id'], uid))
                result = {'id': request['asset_id'], 'format': source['format'], 'metadata': metadata}
        with db.tx() as c:
            c.execute("UPDATE viewer_requests SET state='ready',result=?,updated=? WHERE id=? AND user_id=? AND state='ingesting'",
                      (db.encode(result), time.time(), request['id'], uid))
            db.audit(c, uid, 'viewer.preparation.completed', request['asset_id'],
                     {'operation': request['operation'], 'run': request['run_id']})
    except Exception as exc:
        with db.tx() as c:
            c.execute("UPDATE viewer_requests SET state='failed',error=?,updated=? WHERE id=? AND user_id=? AND state='ingesting'",
                      (str(exc)[:500], time.time(), request['id'], uid))

def status(uid, request_id):
    from . import workbench, viewer_retention
    row = db.query('SELECT * FROM viewer_requests WHERE id=? AND user_id=?', (request_id, uid), one=True)
    if not row:
        raise ValueError('Viewer request not found in this workspace')
    base = {'request_id': row['id'], 'run_id': row['run_id'], 'state': row['state'], 'rows': 0,
            'note': 'Private genomic parsing runs in an isolated, metered worker; queue and cold-start delays apply'}
    if row['state'] == 'expired' or viewer_retention.deleted_dependencies(row):
        return {**base, 'state': 'expired', 'error': viewer_retention.EXPIRED}
    workbench.owned(uid, row['asset_id'])
    viewer_retention.lease_request(row)
    if row['state'] == 'ready':
        if not viewer_retention.ready(row):
            viewer_retention.mark_missing(row)
            return {**base, 'state': 'regenerate', 'error': viewer_retention.REGENERATE}
        return {**base, **json.loads(row['result']), 'state': 'ready'}
    if row['state'] in {'failed', 'regenerate'}:
        return {**base, 'error': row['error']}
    run = db.query('SELECT state,error FROM runs WHERE id=? AND user_id=?', (row['run_id'], uid), one=True)
    if not run or run['state'] in {'failed', 'cancelled', 'interrupted'}:
        return {**base, 'state': 'failed', 'error': run['error'] if run else 'Preparation run is unavailable'}
    if run['state'] == 'succeeded':
        claimed = False
        now = time.time()
        with db.tx() as c:
            current = c.execute('SELECT state,updated,result,error FROM viewer_requests WHERE id=? AND user_id=?', (request_id,uid)).fetchone()
            if current['state'] == 'queued' or current['state'] == 'ingesting' and current['updated'] < now - 900:
                changed=c.execute("UPDATE viewer_requests SET state='ingesting',updated=? WHERE id=? AND user_id=? AND (state='queued' OR (state='ingesting' AND updated<?))", (now,request_id,uid,now-900))
                claimed = bool(changed.rowcount)
        # A previous poll may finish between the initial read and this claim.
        # Completed/failed preparations are terminal and must never be reclaimed.
        if current['state'] == 'ready':
            fresh = {**row, **dict(current)}
            if not viewer_retention.ready(fresh):
                viewer_retention.mark_missing(fresh)
                return {**base, 'state': 'regenerate', 'error': viewer_retention.REGENERATE}
            return {**base, **json.loads(current['result']), 'state': 'ready'}
        if current['state'] in {'failed', 'expired', 'regenerate'}:
            return {**base, 'state': current['state'], 'error': current['error']}
        if claimed:
            workbench.POOL.submit(finish, uid, row)
        return {**base, 'state': 'building'}
    return {**base, 'state': run['state']}

def index_status(uid, aid):
    row = db.query("SELECT id FROM viewer_requests WHERE user_id=? AND asset_id=? AND operation='variant-index' ORDER BY created DESC LIMIT 1", (uid, aid), one=True)
    return status(uid, row['id']) if row else {'state': 'absent'}

initialize()
