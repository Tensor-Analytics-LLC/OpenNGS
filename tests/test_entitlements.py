import copy
import pytest
from backend import budget,db,entitlements,demo,registry

def allowance():
    p={k:v for k,v in entitlements.EXPLORE.items() if k in entitlements.REQUIRED}
    p.update(monthly_budget_usd=50,max_upload_bytes=1024**3,max_storage_bytes=10*1024**3,max_vcpus=8,max_memory_mb=32768,max_scratch_mb=102400,max_node_seconds=3600,max_job_seconds=14400,daily_cpu_seconds=28800,monthly_cpu_seconds=288000,max_concurrent_jobs=2,max_queued_jobs=10)
    return p

def test_research_needs_explicit_funding_and_preserves_existing_data(client,owner):
    url='/api/admin/entitlements/'+owner['id'];body={'reason':'Approved funded research allowance','allowance':allowance()}
    assert client.post(url,json=body).status_code==422
    assert entitlements.limits(owner['id'])['access']=='explore'
    body['funding_confirmed']=True;r=client.post(url,json=body);assert r.status_code==200,r.text
    assert r.json()['access']=='research-pro'
    assert client.get('/api/session').json()['user']['email']==owner['email']
    assert client.post(url,json={'state':'revoked','reason':'Suspend additional funded execution'}).status_code==200
    assert entitlements.limits(owner['id'])['access']=='explore'

def test_research_reservations_do_not_consume_explore_budget(client,owner,monkeypatch):
    entitlements.grant(owner,owner['id'],{'reason':'Fund isolated research usage','funding_confirmed':True,'allowance':allowance()})
    before=budget.snapshot()['available_after_safety_usd']
    with db.tx() as c:budget.reserve(c,owner['id'],'funded-test','scientific',10,{})
    assert budget.snapshot()['available_after_safety_usd']==before
    with pytest.raises(budget.BudgetFull):
        with db.tx() as c:budget.reserve(c,owner['id'],'over-budget-test','scientific',50,{})
    assert len(db.query('SELECT * FROM cost_reservations'))==1
    assert db.query('SELECT account FROM cost_allocations',one=True)['account']=='research-pro'
    monkeypatch.setenv('NGS_MONTHLY_LIMIT_USD','250')
    assert budget.policy()['limit_usd']==200

def test_explore_rejects_arbitrary_tools_flags_and_resources(client,owner,monkeypatch):
    monkeypatch.setattr(entitlements,'DEMO',True)
    g=copy.deepcopy(registry.templates()[0]['graph']);demo.validate(g,owner['id'])
    g['nodes'][0]['tool']='asset.file'
    with pytest.raises(ValueError):demo.validate(g,owner['id'])
    g=copy.deepcopy(registry.templates()[0]['graph']);g['nodes'][1]['advanced']='--input https://example.test/private.bam'
    with pytest.raises(ValueError):demo.validate(g,owner['id'])
    g['nodes'][1]['advanced']='';g['nodes'][1]['resources']['threads']=3
    with pytest.raises(ValueError):demo.validate(g,owner['id'])

def test_period_usage_and_pending_work_remain_reserved(client,owner,monkeypatch):
    monkeypatch.setattr(entitlements,'CLOUD',True)
    with db.tx() as c:entitlements.hold(c,owner,'future-run-a',3000)
    with pytest.raises(ValueError):
        with db.tx() as c:entitlements.hold(c,owner,'future-run-b',1000)
    with db.tx() as c:entitlements.settle(c,'future-run-a',10)
    with db.tx() as c:entitlements.hold(c,owner,'future-run-b',1000)
    assert db.query("SELECT state FROM usage_holds WHERE run_id='future-run-b'",one=True)['state']=='reserved'

def test_non_admin_cannot_fund_their_own_account(client,owner):
    with db.tx() as c:c.execute("UPDATE users SET role='user' WHERE id=?",(owner['id'],))
    r=client.post('/api/admin/entitlements/'+owner['id'],json={'reason':'Unapproved quota escalation','funding_confirmed':True,'allowance':allowance()})
    assert r.status_code==403
