import asyncio
import concurrent.futures
import json
import pytest
from backend import budget,db
from backend.admission import Admission

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
    assert r.json()['limit_usd']<=250
    assert client.get('/api/operations/datasets').json()['local_only_inventory']['files']==0

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
