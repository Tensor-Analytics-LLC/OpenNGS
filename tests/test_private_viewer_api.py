"""HTTP ownership, durable admission and quota behavior; no AWS calls."""
import time

import pytest

from backend import budget, data, db, entitlements, graph, remote_viewer, runner, workbench


def committed_index(request):
    row=db.query('SELECT * FROM viewer_requests WHERE id=?',(request['request_id'],),one=True)
    path=workbench.index_path(row['user_id'],row['asset_id'])
    with workbench.connection(path) as c:
        c.execute('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT)')
        for key,value in {'identity':row['identity'],'isolated_run':row['run_id']}.items():
            c.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(key,db.encode(value)))


@pytest.fixture
def private_viewer(client, owner, monkeypatch):
    uid = owner['id']
    aid = db.ident('private_')
    with db.tx() as c:
        c.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?,?)',
                  (aid, uid, f's3://private/tenants/{uid}/source.vcf', 'source.vcf',
                   'VCF', 100, time.time(), 'variants', 'engineering',
                   db.encode({'sha256': 'a'*64, 'version_id': 'immutable-version'})))
    original_settings = data.settings
    monkeypatch.setattr(data, 'settings', lambda u: {
        **original_settings(u),
        'research_profile': {'runtime_profiles': {'results.prepare': {'image': 'repo/viewer@sha256:'+'b'*64}}}})
    monkeypatch.setattr(entitlements, 'restricted', lambda u: False)
    original_limits = entitlements.limits
    monkeypatch.setattr(entitlements, 'limits', lambda u: {
        **original_limits(u), 'max_scratch_mb': 20480, 'max_node_seconds': 120,
        'max_storage_bytes': 128*1024**2})
    monkeypatch.setattr(remote_viewer, 'CLOUD', True)
    monkeypatch.setattr(workbench, 'source', lambda *a, **k: pytest.fail('Private data reached native application-host reader'))
    calls = []

    def admit(user, body, operation_id):
        graph.validate_shape(body)
        calls.append((body, operation_id))
        rid = db.ident('run_')
        with db.tx() as c:
            c.execute('INSERT INTO runs(id,user_id,operation_id,state,graph,created,reserved) VALUES(?,?,?,?,?,?,?)',
                      (rid, uid, operation_id, 'queued', db.encode(body), time.time(), 240))
        return {'id': rid}, True
    monkeypatch.setattr(runner, 'admit', admit)
    return {'uid': uid, 'asset': aid, 'calls': calls}


def test_private_index_header_and_omics_use_durable_bounded_admission(client, private_viewer):
    aid = private_viewer['asset']
    response = client.post('/api/results/'+aid+'/index')
    assert response.status_code == 200
    first = response.json()
    assert first['state'] == 'queued' and first['request_id'].startswith('viewer_')
    again = client.post('/api/results/'+aid+'/index').json()
    assert again['request_id'] == first['request_id'] and again['run_id'] == first['run_id']
    assert len(private_viewer['calls']) == 1
    body, operation_id = private_viewer['calls'][0]
    assert body['maximum_output_bytes'] == 64*1024**2
    assert body['nodes'][-1]['resources'] == {
        'threads': 2, 'memory_mb': 4096, 'scratch_mb': 20480, 'timeout_seconds': 120}
    assert operation_id == first['request_id']
    header = client.post('/api/assets/'+aid+'/inspect', json={}).json()
    assert header['state'] == 'queued' and header['request_id'] != first['request_id']
    assert private_viewer['calls'][-1][0]['nodes'][-1]['params']['operation'] == 'header'
    assert client.get('/api/viewer/'+header['request_id']).json()['run_id'] == header['run_id']
    assert client.get('/api/results/'+aid+'/status').json()['request_id'] == first['request_id']


def test_private_viewer_explore_and_foreign_workspace_are_denied(client, owner, private_viewer, monkeypatch):
    aid = private_viewer['asset']
    result = client.post('/api/results/'+aid+'/index').json()
    monkeypatch.setattr(entitlements, 'restricted', lambda uid: True)
    denied = client.post('/api/assets/'+aid+'/inspect', json={})
    assert denied.status_code == 422 and 'funded Research Pro' in denied.text
    foreign = client.post('/api/auth/signup', json={'email': 'foreign-viewer@example.test', 'password': 'Foreign-test-password-987!'}).json()
    client.headers['X-CSRF-Token'] = foreign['csrf']
    for method, url, payload in [
        ('GET', '/api/viewer/'+result['request_id'], None),
        ('POST', '/api/results/'+aid+'/query', {'filters': {}}),
        ('POST', '/api/results/'+aid+'/export/tsv', {'filters': {}}),
        ('POST', '/api/assets/'+aid+'/inspect', {}),
    ]:
        response = client.request(method, url, **({'json': payload} if payload is not None else {}))
        assert response.status_code == 422
        assert 'workspace' in response.text
    assert len(private_viewer['calls']) == 1


def test_budget_rejection_never_creates_a_viewer_request(client, private_viewer, monkeypatch):
    def reject(*args):
        raise budget.BudgetFull('Assigned validation allowance exhausted')
    monkeypatch.setattr(runner, 'admit', reject)
    response = client.post('/api/results/'+private_viewer['asset']+'/index')
    assert response.status_code == 429
    assert response.headers['Retry-After'] == '3600'
    assert not db.query('SELECT id FROM viewer_requests WHERE user_id=?', (private_viewer['uid'],))


def test_successful_run_claims_ingestion_once_and_returns_only_owner_result(client, private_viewer, monkeypatch):
    request = client.post('/api/results/'+private_viewer['asset']+'/index').json()
    with db.tx() as c:
        c.execute("UPDATE runs SET state='succeeded' WHERE id=?", (request['run_id'],))
    submissions = []
    monkeypatch.setattr(workbench.POOL, 'submit', lambda *args: submissions.append(args))
    assert client.get('/api/viewer/'+request['request_id']).json()['state'] == 'building'
    assert client.get('/api/viewer/'+request['request_id']).json()['state'] == 'building'
    assert len(submissions) == 1
    with db.tx() as c:
        c.execute("UPDATE viewer_requests SET state='ready',result=? WHERE id=?",
                  (db.encode({'rows': 3, 'assembly': 'engineering'}), request['request_id']))
    committed_index(request)
    ready = client.get('/api/viewer/'+request['request_id']).json()
    assert ready['state'] == 'ready' and ready['rows'] == 3
    assert len(submissions) == 1


@pytest.mark.parametrize('terminal', ['ready', 'failed'])
def test_stale_poll_cannot_reclaim_concurrently_finished_ingestion(client, private_viewer, monkeypatch, terminal):
    request = client.post('/api/results/'+private_viewer['asset']+'/index').json()
    with db.tx() as c:
        c.execute("UPDATE runs SET state='succeeded' WHERE id=?", (request['run_id'],))
    query = db.query
    def racing_query(sql, *args, **kwargs):
        result = query(sql, *args, **kwargs)
        if sql.startswith('SELECT state,error FROM runs'):
            with db.tx() as c:
                c.execute('UPDATE viewer_requests SET state=?,result=?,error=?,updated=? WHERE id=?',
                          (terminal,db.encode({'rows':7}),'Rejected malformed output' if terminal=='failed' else None,time.time(),request['request_id']))
        return result
    monkeypatch.setattr(db,'query',racing_query)
    if terminal=='ready':committed_index(request)
    monkeypatch.setattr(workbench.POOL,'submit',lambda *args:pytest.fail('Terminal ingestion was reclaimed'))
    response=client.get('/api/viewer/'+request['request_id'])
    assert response.status_code==200
    assert response.json()['state']==terminal
    if terminal=='ready':assert response.json()['rows']==7
    else:assert response.json()['error']=='Rejected malformed output'
    assert query('SELECT state FROM viewer_requests WHERE id=?',(request['request_id'],),one=True)['state']==terminal


def test_explicit_retry_reuses_committed_worker_and_never_retries_on_poll(client, private_viewer, monkeypatch):
    request=client.post('/api/results/'+private_viewer['asset']+'/index').json()
    with db.tx() as c:
        c.execute("UPDATE runs SET state='succeeded' WHERE id=?",(request['run_id'],))
        c.execute("UPDATE viewer_requests SET state='failed',error='Transient result read' WHERE id=?",(request['request_id'],))
    submissions=[]
    monkeypatch.setattr(workbench.POOL,'submit',lambda *args:submissions.append(args))
    assert client.get('/api/viewer/'+request['request_id']).json()['state']=='failed'
    assert not submissions
    retried=client.post('/api/results/'+private_viewer['asset']+'/index').json()
    assert retried['request_id']==request['request_id'] and retried['run_id']==request['run_id']
    assert retried['state']=='building'
    assert len(submissions)==1 and len(private_viewer['calls'])==1
    assert client.post('/api/results/'+private_viewer['asset']+'/index').json()['state']=='building'
    assert len(submissions)==1
    assert any('"viewer.ingestion.retry"' in row['body'] for row in db.query('SELECT body FROM audit'))


def test_explicit_retry_does_not_resubmit_a_failed_worker(client, private_viewer):
    request=client.post('/api/results/'+private_viewer['asset']+'/index').json()
    with db.tx() as c:
        c.execute("UPDATE runs SET state='failed',error='Tool failure' WHERE id=?",(request['run_id'],))
        c.execute("UPDATE viewer_requests SET state='failed',error='Tool failure' WHERE id=?",(request['request_id'],))
    retried=client.post('/api/results/'+private_viewer['asset']+'/index').json()
    assert retried['state']=='failed' and retried['run_id']==request['run_id']
    assert len(private_viewer['calls'])==1


def test_missing_index_explicit_reprepare_reuses_worker(client,private_viewer,monkeypatch):
    request=client.post('/api/results/'+private_viewer['asset']+'/index').json()
    with db.tx() as c:
        c.execute("UPDATE runs SET state='succeeded' WHERE id=?",(request['run_id'],))
        c.execute("UPDATE viewer_requests SET state='ready',result=? WHERE id=?",(db.encode({'rows':3}),request['request_id']))
    assert client.get('/api/viewer/'+request['request_id']).json()['state']=='regenerate'
    submissions=[];monkeypatch.setattr(workbench.POOL,'submit',lambda *args:submissions.append(args))
    rebuilt=client.post('/api/results/'+private_viewer['asset']+'/index').json()
    assert rebuilt['state']=='building' and rebuilt['run_id']==request['run_id']
    assert len(submissions)==1 and len(private_viewer['calls'])==1


def test_expired_worker_requires_new_bounded_admission_on_explicit_prepare(client,private_viewer):
    request=client.post('/api/results/'+private_viewer['asset']+'/index').json();uid=private_viewer['uid'];now=time.time()
    with db.tx() as c:
        c.execute("UPDATE runs SET state='succeeded' WHERE id=?",(request['run_id'],))
        c.execute("INSERT INTO retention_plans VALUES(?,?,'deleted',?,?,?,?,?,NULL,0,?,?,NULL)",
                  (request['run_id'],uid,'private','tenants/'+uid+'/runs/'+request['run_id']+'/',now,now,'{}',now,now))
    assert client.get('/api/viewer/'+request['request_id']).json()['state']=='expired'
    rebuilt=client.post('/api/results/'+private_viewer['asset']+'/index').json()
    assert rebuilt['state']=='queued' and rebuilt['run_id']!=request['run_id']
    assert len(private_viewer['calls'])==2
    assert client.post('/api/results/'+private_viewer['asset']+'/index').json()['run_id']==rebuilt['run_id']
    assert len(private_viewer['calls'])==2
