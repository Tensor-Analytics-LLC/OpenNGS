import json
import time
import pytest
from backend import budget, db, entitlements, migration

GIB=1024**3


@pytest.fixture
def allocation(owner,monkeypatch):
    uid=owner['id'];pid='migration_'+'a'*24;sha='b'*64
    with db.tx() as c:
        c.execute('DELETE FROM baseline_storage_allocations')
        c.execute('DELETE FROM migration_assets');c.execute('DELETE FROM migration_objects')
        c.execute('INSERT INTO controls VALUES(?,?)',('migration:plan:'+pid,db.encode({'plan_id':pid,'owner_id':uid,'sha256':sha})))
    objects=[]
    for i,size in enumerate([2*GIB,4*GIB]):
        alias={'id':'baseline_asset_'+str(i),'user_id':uid,'path':'/preserved/source-'+str(i),'name':'source-'+str(i),'format':'DATABASE','bytes':size,'mtime':1,'family':'gnomad','assembly':'GRCh38','metadata':{}}
        objects.append({'sha256':str(i+1)*64,'bytes':size,'aliases':[alias],'dataset_id':'baseline_ds_'+str(i),'key':f'tenants/{uid}/datasets/baseline_ds_{i}/source','name':alias['name']})
    plan={'plan_id':pid,'owner_id':uid,'total_bytes':6*GIB,'objects':objects,'bucket':'private'}
    monkeypatch.setattr(migration,'load_plan',lambda registration:plan)
    return owner,plan


def import_one(plan,obj):
    with db.tx() as c:
        c.execute("INSERT INTO migration_objects VALUES(?,?,?,'Verifying',?,0,?,0,NULL,?)",
                  (plan['plan_id'],obj['sha256'],plan['owner_id'],'{}','immutable',time.time()))
    migration.import_manifest(plan,obj,{'version_id':'immutable'},obj['sha256'],'raw-cloud-sha256')


def unrelated(uid,size):
    with db.tx() as c:
        c.execute("INSERT INTO datasets VALUES(?,?,?,?,?,?,'Ready',?,?,?,?,?)",
                  ('unrelated',uid,'unrelated-asset','upload',size,'f'*64,f'tenants/{uid}/datasets/unrelated/file','v1',
                   db.encode({'migration_plan':'migration_'+'a'*24,'baseline_funded':True}),time.time(),time.time()))


def test_reviewed_allocation_is_idempotent_admin_only_and_globally_bounded(allocation):
    owner,plan=allocation
    with pytest.raises(ValueError,match='administrator'):budget.allocate_baseline_storage('unknown-user',plan['plan_id'],'Explicit test allocation')
    first=budget.allocate_baseline_storage(owner['id'],plan['plan_id'],'Existing fixed baseline storage')
    second=budget.allocate_baseline_storage(owner['id'],plan['plan_id'],'Idempotent baseline allocation')
    assert first==second and len(db.query('SELECT * FROM baseline_storage_allocations'))==1
    assert not entitlements.research(owner['id'])
    with db.tx() as c:
        c.execute('INSERT INTO baseline_storage_allocations VALUES(?,?,?,?,?,?,?)',
                  ('already-funded','another-owner','d'*64,599*GIB,time.time(),owner['id'],'Existing reviewed capacity'))
        c.execute('DELETE FROM baseline_storage_allocations WHERE plan_id=?',(plan['plan_id'],))
    with pytest.raises(budget.BudgetFull,match='baseline storage'):budget.allocate_baseline_storage(owner['id'],plan['plan_id'],'Exceeds available baseline envelope')


def test_partial_migration_offsets_only_actual_verified_bytes_for_explore(allocation):
    owner,plan=allocation;uid=owner['id'];budget.allocate_baseline_storage(uid,plan['plan_id'],'Existing reviewed baseline storage')
    import_one(plan,plan['objects'][0]);unrelated(uid,100*1024**2)
    with db.tx() as c:
        assert budget.baseline_stored_bytes(uid,c)==2*GIB
        assert entitlements.storage_exposure(c,uid)==2*GIB+100*1024**2
        assert entitlements.output_storage_exposure(c,uid,'explore')==100*1024**2
        assert entitlements.output_storage_exposure(c,uid,'research-pro')==2*GIB+100*1024**2
        assert entitlements.reserve_output(c,owner,'sample-run')==64*1024**2
        c.execute('UPDATE datasets SET bytes=? WHERE id=?',(220*1024**2,'unrelated'))
        with pytest.raises(ValueError,match='retained-storage'):entitlements.reserve_output(c,owner,'another-sample')
    assert entitlements.remaining(uid)['physical_stored_bytes']==2*GIB+220*1024**2
    assert entitlements.remaining(uid)['baseline_funded_stored_bytes']==2*GIB


def test_pro_reserves_incremental_financial_capacity_once_but_counts_full_physical_storage(allocation):
    owner,plan=allocation;uid=owner['id'];budget.allocate_baseline_storage(uid,plan['plan_id'],'Existing reviewed baseline storage')
    import_one(plan,plan['objects'][0])
    body={k:entitlements.EXPLORE[k] for k in entitlements.REQUIRED}
    body.update(max_upload_bytes=GIB,max_storage_bytes=7*GIB,monthly_budget_usd=2)
    allowance=entitlements.grant(owner,uid,{'reason':'Explicit funded unit-test allowance','funding_confirmed':True,'allowance':body})
    first=budget.reserve_storage_capacity(uid,allowance)
    assert json.loads(first['details'])['additional_bytes']==GIB
    assert json.loads(first['details'])['baseline_funded_bytes']==6*GIB
    assert first['reserved_micro']==23000
    assert budget.reserve_storage_capacity(uid,allowance) is None
    allowance['max_storage_bytes']=8*GIB
    second=budget.reserve_storage_capacity(uid,allowance)
    assert json.loads(second['details'])['additional_bytes']==GIB
    assert sum(row['reserved_micro'] for row in db.query("SELECT reserved_micro FROM cost_reservations WHERE kind='retained-capacity'"))==46000
    assert entitlements.remaining(uid)['storage_bytes']==5*GIB
    assert all(row['account']=='research-pro' for row in db.query('SELECT account FROM cost_allocations'))


def test_metadata_spoof_and_changed_registration_never_get_baseline_offset(allocation):
    owner,plan=allocation;uid=owner['id'];unrelated(uid,100*1024**2)
    with db.tx() as c:
        assert budget.baseline_stored_bytes(uid,c)==0
        assert budget.billable_storage_capacity(uid,8*GIB,c)==(8*GIB,0)
    budget.allocate_baseline_storage(uid,plan['plan_id'],'Existing reviewed baseline storage')
    import_one(plan,plan['objects'][0])
    with db.tx() as c:
        c.execute('UPDATE controls SET value=? WHERE key=?',
                  (db.encode({'owner_id':uid,'sha256':'c'*64}),'migration:plan:'+plan['plan_id']))
        assert budget.baseline_stored_bytes(uid,c)==0
        assert budget.billable_storage_capacity(uid,8*GIB,c)==(8*GIB,0)
