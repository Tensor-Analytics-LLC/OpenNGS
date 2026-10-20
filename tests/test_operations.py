import asyncio
import concurrent.futures
import json
import pytest
from backend import budget,db
from backend.admission import Admission

def test_budget_accepts_postgres_numeric_aggregates():
    from decimal import Decimal
    class Query:
        def __init__(self,value):self.value=value
        def fetchone(self):return self.value
    class Connection:
        def execute(self,sql,args=()):return Query(None if 'billing_checks' in sql or 'controls' in sql else [Decimal(0)])
    snapshot=budget.snapshot(Connection())
    assert snapshot['reserved_usd']==0 and snapshot['limit_usd']<=200

def test_atomic_money_reservation_and_replay(client,owner,monkeypatch):
    monkeypatch.setenv('NGS_MONTHLY_LIMIT_USD','10');monkeypatch.setenv('NGS_FIXED_MONTHLY_USD','0')
    monkeypatch.setenv('NGS_BUDGET_SAFETY_USD','0');monkeypatch.setenv('NGS_TAX_FRACTION','0')
    def reserve(i):
        try:
            with db.tx() as c:budget.reserve(c,owner['id'],'test-'+str(i),'scientific',1,{'test':True})
            return True
        except budget.BudgetFull:return False
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:results=list(pool.map(reserve,range(50)))
    assert sum(results)==10
    assert reserve(0)  # A replay does not spend twice.
    assert budget.snapshot()['reserved_usd']==10
    budget.reconcile('test-0',owner['id'],'scientific',0)
    assert reserve(49)

def test_budget_api_and_cloud_inventory(client,owner):
    r=client.get('/api/operations/budget');assert r.status_code==200
    assert r.json()['limit_usd']<=200
    assert client.get('/api/operations/datasets').json()['preparing_inventory']['files']==0

def test_admission_rejects_without_discarding_existing_work():
    async def exercise():
        gate=asyncio.Event()
        async def slow(scope,receive,send):
            await gate.wait()
            await send({'type':'http.response.start','status':200,'headers':[]})
            await send({'type':'http.response.body','body':b'ok'})
        middleware=Admission(slow);middleware.limit=2;middleware.tenant_limit=2
        results=[]
        async def request(i):
            events=[]
            async def receive():return {'type':'http.request','body':b''}
            async def send(event):events.append(event)
            await middleware({'type':'http','path':'/api/graphs','headers':[(b'cookie',str(i).encode())],'client':('127.0.0.1',1)},receive,send)
            results.append(next(e['status'] for e in events if e['type']=='http.response.start'))
        admitted=[asyncio.create_task(request(i)) for i in range(2)]
        await asyncio.sleep(.01)
        await request(3)
        assert results==[503]
        gate.set();await asyncio.gather(*admitted)
        assert sorted(results)==[200,200,503]
        assert middleware.active==0 and middleware.rejected['concurrency']==1
    asyncio.run(exercise())

def test_postgres_translation_preserves_required_semantics():
    from backend.postgres import sql
    assert sql("SELECT MAX(0,used_seconds-?) WHERE path NOT LIKE 's3://%'")=="SELECT GREATEST(0,used_seconds-%s) WHERE path NOT LIKE 's3://%%'"
    assert '::jsonb' in sql("SELECT json_extract(metadata,'$.uploaded')=1")

def test_execution_streams_do_not_exhaust_interactive_admission():
    async def exercise():
        gate=asyncio.Event()
        async def serve(scope,receive,send):
            if scope['path'].endswith('/events'):await gate.wait()
            await send({'type':'http.response.start','status':200,'headers':[]})
            await send({'type':'http.response.body','body':b'ok'})
        middleware=Admission(serve);middleware.limit=2
        async def invoke(path,cookie):
            events=[]
            async def send(x):events.append(x)
            async def receive():return {'type':'http.request','body':b''}
            await middleware({'type':'http','path':path,'headers':[(b'cookie',cookie)],'client':('127.0.0.1',1)},receive,send)
            return events[0]['status']
        streams=[asyncio.create_task(invoke('/api/runs/fixture/events',str(i).encode())) for i in range(2)]
        await asyncio.sleep(.01)
        assert middleware.streams==2 and middleware.active==0
        assert await invoke('/api/graphs',b'normal')==200
        gate.set();await asyncio.gather(*streams)
        assert middleware.streams==0 and middleware.active==0
    asyncio.run(exercise())

def test_totp_standard_vector_and_recovery_replay(client,owner,monkeypatch):
    from backend import mfa
    # RFC 6238 SHA-1 vector at 59 seconds, truncated to this application's six digits.
    assert mfa.totp('GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ',1)=='287082'
    setup=client.post('/api/account/mfa/setup',json={});assert setup.status_code==200
    seed=setup.json()['secret'];code=mfa.totp(seed,int(mfa.time.time()//30))
    enabled=client.post('/api/account/mfa/enable',json={'code':code});assert enabled.status_code==200
    assert not mfa.check(owner['id'],code)
    recovery=enabled.json()['recovery_codes'][0]
    assert mfa.check(owner['id'],recovery)
    assert not mfa.check(owner['id'],recovery)

def test_sandbox_hardlinks_are_copied_without_creating_links(tmp_path):
    import io,tarfile
    from backend.demo_jobs import extract_outputs
    def bundle(target):
        buffer=io.BytesIO()
        with tarfile.open(fileobj=buffer,mode='w:gz') as archive:
            data=b'known scientific bytes';member=tarfile.TarInfo('files/data/value');member.size=len(data);archive.addfile(member,io.BytesIO(data))
            link=tarfile.TarInfo('files/copy/value');link.type=tarfile.LNKTYPE;link.linkname=target;archive.addfile(link)
        return buffer.getvalue()
    extract_outputs(bundle('files/data/value'),tmp_path/'safe')
    a=tmp_path/'safe/files/data/value';b=tmp_path/'safe/files/copy/value'
    assert a.read_bytes()==b.read_bytes() and a.stat().st_ino!=b.stat().st_ino
    with pytest.raises(ValueError):extract_outputs(bundle('../../outside'),tmp_path/'unsafe')

def test_posted_costs_keep_pro_separate_from_explore(client,owner,monkeypatch):
    from backend import billing
    def group(project,tenant,amount):return {'Keys':['Project$'+project,'TenantId$'+tenant],'Metrics':{'UnblendedCost':{'Amount':str(amount)}}}
    class CE:
        def get_cost_and_usage(self,**kw):
            assert len(kw['GroupBy'])==2
            return {'ResultsByTime':[{'Groups':[group('OpenNGS','',10),group('OpenNGS-Pro',owner['id'],30),group('OpenNGS-Pro','',2),group('OpenNGS-Pro','internal',1)]}]}
    class Session:
        def client(self,name):assert name=='ce';return CE()
    with db.tx() as c:c.execute('INSERT INTO controls VALUES(?,?)',('validation-tenant:internal','true'))
    monkeypatch.setattr(billing,'aws_session',lambda:Session())
    result=billing.reconcile_posted()
    assert result['tagged_baseline_service_cost_usd']==11
    assert result['research_pro_tenants_pretax_usd']=={owner['id']:30}
    assert result['research_pro_shared_pretax_usd']==2
    assert db.query('SELECT gross_micro FROM billing_checks',one=True)['gross_micro']==12980000


def test_immutable_audit_checkpoint_detects_prefix_tampering(client,owner,monkeypatch):
    import io,hashlib
    from datetime import datetime,timedelta,timezone
    from backend import audit_anchor
    objects={};retain=datetime.now(timezone.utc)+timedelta(days=30)
    class S3:
        def put_object(self,**kw):objects[kw['Key']]=kw;return {'VersionId':'v1'}
        def head_object(self,**kw):return {'VersionId':'v1','ObjectLockMode':'GOVERNANCE','ObjectLockRetainUntilDate':retain}
        def get_object(self,**kw):
            assert kw['VersionId']=='v1';o=objects[kw['Key']]
            return {'Body':io.BytesIO(o['Body']),'Metadata':o['Metadata'],'ObjectLockMode':'GOVERNANCE','ObjectLockRetainUntilDate':retain}
    class Session:
        def client(self,name):assert name=='s3';return S3()
    monkeypatch.setenv('NGS_AUDIT_BUCKET','test-audit');monkeypatch.setattr(audit_anchor,'aws_session',lambda:Session())
    checkpoint=audit_anchor.anchor();assert checkpoint['state']=='Anchored'
    assert audit_anchor.verify()['valid']
    with db.tx() as c:c.execute('UPDATE audit SET hash=? WHERE seq=?',('f'*64,checkpoint['seq']))
    assert not audit_anchor.verify()['valid']

def test_s3_fasta_preview_uses_exact_bounded_ranges_and_index():
    import io
    from backend.range_fasta import S3Fasta
    fasta=b'>chr1\nACGT\nTGCA\nAACC\n';calls=[]
    class S3:
        def get_object(self,**args):
            calls.append(args)
            if args['Key']=='reference.fai':return {'Body':io.BytesIO(b'chr1\t12\t6\t4\t5\n')}
            a,b=map(int,args['Range'][6:].split('-'));return {'Body':io.BytesIO(fasta[a:b+1])}
    reader=S3Fasta(S3(),'tenant','reference.fa','reference.fai','immutable-version')
    assert reader.get_reference_length('chr1')==12
    assert reader.fetch('chr1',2,10)=='GTTGCAAA'
    assert calls[-1]['Range']=='bytes=8-17' and calls[-1]['VersionId']=='immutable-version'
    with pytest.raises(ValueError):reader.fetch('chr1',10,13)

def test_cache_companions_are_part_of_integrity_and_identity(client,owner):
    import hashlib
    from backend import cache,registry
    from backend.config import DATA
    folder=DATA/'uploads'/owner['id']/'cache-companions';folder.mkdir(parents=True)
    primary=folder/'reference.fa';index=folder/'reference.fa.fai';dictionary=folder/'reference.dict'
    primary.write_text('>c\nACGT\n');index.write_text('c\t4\t3\t4\t5\n');dictionary.write_text('@SQ\tSN:c\tLN:4\n')
    item={'path':str(primary),'format':'FASTA','bytes':primary.stat().st_size,'digest':'sha256:'+hashlib.sha256(primary.read_bytes()).hexdigest(),'metadata':{'dictionary':str(dictionary),'index_paths':[str(index)]}}
    inputs={'reference':[item]};node={'params':{}};spec=registry.BASE['qc.summary']
    first=cache.key(owner['id'],spec,node,inputs,'portable');cache.store(owner['id'],'companion-proof',{'reference':[item]})
    assert cache.load(owner['id'],'companion-proof')
    index.write_text('changed index with different size')
    assert cache.key(owner['id'],spec,node,inputs,'portable')!=first
    assert cache.load(owner['id'],'companion-proof') is None

def test_provider_metering_preserves_unknown_reservations_and_prices_observed_allocation(client,owner):
    from backend import metering
    assert metering.fargate({'createdAt':1000},2,4096) is None
    usage=metering.fargate({'createdAt':1000,'stoppedAt':61000,'container':{'ephemeralStorage':{'sizeInGiB':200}}},2,4096)
    expected=(2*.04048+4*.004445+180*.000111)/60
    assert usage['upper_pretax_usd']==pytest.approx(expected,abs=1e-8)
    with db.tx() as c:
        c.execute("INSERT INTO runs(id,user_id,operation_id,state,graph,created,reserved) VALUES(?,?,?,'succeeded','{}',1,0)",('meter-run',owner['id'],'meter-operation'))
        c.execute("INSERT INTO attempts(id,run_id,node_id,state,provider_id) VALUES(?,?,?,'succeeded',?)",('meter-attempt','meter-run','n1','provider-fixture'))
    assert metering.scientific_cost('meter-run') is None
    metering.record('meter-attempt',usage)
    assert metering.scientific_cost('meter-run')==pytest.approx(.10+expected,abs=1e-8)

def test_cloud_log_binding_and_secret_redaction(client,owner,monkeypatch):
    from backend import cloud_logs
    assert 'private-token' not in cloud_logs.redact('authorization=private-token https://example.test/?X-Amz-Signature=value')
    assert 'X-Amz' not in cloud_logs.redact('https://example.test/?X-Amz-Signature=value')
    with pytest.raises(ValueError,match='tenant'):
        cloud_logs.drain(owner['id'],'r','a','n',{'tags':{'TenantId':'foreign'},'container':{'logStreamName':'research/stream'}})
    cloud_logs.drain(owner['id'],'r','a','n',{'container':{'logStreamName':'research/stream'}},sensitive=True)

def test_object_access_trail_is_scoped_and_does_not_recursively_log_its_archive():
    from infra.audit_stack import template
    resources=template('123456789012','arn:key','app-role')['Resources']
    trail=resources['ObjectAccessTrail']['Properties']
    assert trail['EnableLogFileValidation'] and not trail['IsMultiRegionTrail']
    selectors=trail['AdvancedEventSelectors'][0]['FieldSelectors']
    prefix=next(x['StartsWith'] for x in selectors if x['Field']=='resources.ARN')
    assert 'arn:aws:s3:::openngs-audit-123456789012-us-east-1/anchors/' in prefix
    assert not any('/cloudtrail/' in x for x in prefix)
    assert not any(x['Field']=='eventCategory' and x['Equals']==['Management'] for x in selectors)
    assert resources['Archive']['DeletionPolicy']=='Retain'

def test_vm_routing_uses_the_smallest_sustained_cpu_profile():
    from backend.vm import select
    assert select({'threads':8,'memory_mb':32768,'scratch_mb':1024000,'timeout_seconds':43200})['instance_type']=='m7i.2xlarge'
    assert select({'threads':2,'memory_mb':4096,'scratch_mb':32768,'timeout_seconds':3600})['instance_type']=='m7i.large'
    assert select({'threads':2,'memory_mb':4096,'scratch_mb':32768,'timeout_seconds':120})['instance_type']=='t3.medium'

def test_idempotent_execution_binds_the_output_reservation(client,owner):
    from backend import registry
    g=next(x['graph'] for x in registry.templates() if x['id']=='quickstart')
    g['maximum_output_bytes']=128*1024**2
    first=client.post('/api/runs',headers={'Idempotency-Key':'output-identity-proof'},json={'graph':g})
    assert first.status_code==200,first.text
    g['maximum_output_bytes']=64*1024**2
    replay=client.post('/api/runs',headers={'Idempotency-Key':'output-identity-proof'},json={'graph':g})
    assert replay.status_code==422 and 'Idempotency' in replay.text


def test_request_rate_limits_remain_active_and_tenant_isolated():
    async def exercise():
        async def serve(scope,receive,send):
            await send({'type':'http.response.start','status':200,'headers':[]})
            await send({'type':'http.response.body','body':b'ok'})
        middleware=Admission(serve)
        async def invoke(identity):
            events=[]
            async def send(event):events.append(event)
            async def receive():return {'type':'http.request','body':b''}
            await middleware({'type':'http','path':'/api/graphs','headers':[],'client':(identity,1)},receive,send)
            return events[0]['status']
        assert [await invoke('first') for _ in range(180)]==[200]*180
        assert await invoke('first')==429
        assert await invoke('second')==200
        assert middleware.rejected['request-rate']==1 and middleware.active==0
    asyncio.run(exercise())
