import json
import time
import pytest
from backend import budget, db, entitlements
from test_entitlements import allowance


def user():
    uid=db.ident('u_')
    with db.tx() as c:
        c.execute('INSERT INTO users(id,email,password,role,created) VALUES(?,?,?,?,?)',
                  (uid,uid+'@example.test','test-only-unusable','user',time.time()))
    return uid


def activate(owner,uid,value=500,**extra):
    body=allowance();body['monthly_budget_usd']=value
    return entitlements.grant(owner,uid,{'reason':'Explicit additional tenant funding','funding_confirmed':True,'allowance':body,**extra})


def reserve(uid,operation,amount):
    with db.tx() as c:return budget.reserve(c,uid,operation,'scientific',amount,{})


def test_normal_grants_require_exact_independent_500_and_no_baseline_option(client,owner):
    uid=user()
    for amount in [0,4,50,499,501,1000]:
        with pytest.raises(ValueError,match=r'exactly \$500'):
            activate(owner,uid,amount)
    with pytest.raises(ValueError,match='separately funded'):
        activate(owner,uid,funding_source='baseline')
    assert not entitlements.research(uid)
    activate(owner,uid)
    assert entitlements.limits(uid)['monthly_budget_usd']==500
    assert db.query('SELECT * FROM cost_reservations')==[]  # Grant is no purchase.


def test_each_pro_can_spend_own_500_when_explore_has_zero_capacity(client,owner,monkeypatch):
    first,second=user(),user()
    activate(owner,first);activate(owner,second)
    monkeypatch.setenv('NGS_FIXED_MONTHLY_USD','200')
    with db.tx() as c:
        c.execute('INSERT INTO controls VALUES(?,?)',('baseline-admission-hold:'+budget.period(),'true'))
    assert budget.snapshot()['available_after_safety_usd']==0
    with pytest.raises(budget.BudgetFull):reserve(owner['id'],'explore-paused',.01)
    reserve(first,'pro-first',400);reserve(second,'pro-second',400)
    assert entitlements.remaining(first)['budget_usd']==pytest.approx(28)
    assert entitlements.remaining(second)['budget_usd']==pytest.approx(28)
    assert entitlements.remaining(first)['admission_open']
    with pytest.raises(budget.BudgetFull):reserve(first,'over-own-ceiling',24)
    reserve(second,'independent-remaining',20)
    assert entitlements.remaining(first)['budget_usd']==pytest.approx(28)
    assert entitlements.remaining(second)['budget_usd']==pytest.approx(4.4)
    assert budget.snapshot()['reserved_usd']==0


def test_tax_and_posted_costs_cannot_hide_new_pro_commitments(client,owner):
    uid=user();activate(owner,uid)
    with db.tx() as c:
        c.execute('INSERT INTO controls VALUES(?,?)',('pro-posted:'+uid+':'+budget.period(),str(480*budget.MICRO)))
    reserve(uid,'first-after-posted',10)
    assert entitlements.remaining(uid)['budget_usd']==pytest.approx(8.2)
    with pytest.raises(budget.BudgetFull):reserve(uid,'second-after-posted',10)
    assert len(db.query('SELECT * FROM cost_reservations'))==1


def test_baseline_posted_cost_floor_also_preserves_all_outstanding_holds(client,owner):
    with db.tx() as c:c.execute('INSERT INTO billing_checks VALUES(?,?,?)',(budget.period(),170*budget.MICRO,time.time()))
    before=budget.snapshot()['available_after_safety_usd']
    reserve(owner['id'],'explore-first',1)
    reserve(owner['id'],'explore-second',1)
    assert budget.snapshot()['available_after_safety_usd']==pytest.approx(before-2.36)


def test_revoke_reactivate_and_month_change_do_not_erase_commitments(client,owner):
    uid=user();activate(owner,uid);reservation=reserve(uid,'accepted-before-revoke',400)
    with db.tx() as c:c.execute('UPDATE cost_reservations SET period=? WHERE id=?',('2020-01',reservation['id']))
    entitlements.grant(owner,uid,{'state':'revoked','reason':'Pause this tenant additional funding'})
    assert db.query('SELECT * FROM cost_reservations',one=True)['state']=='reserved'
    activate(owner,uid)
    assert entitlements.remaining(uid)['budget_usd']==pytest.approx(28)
    with pytest.raises(budget.BudgetFull):reserve(uid,'over-budget-after-reactivation',24)
    assert db.query('SELECT account FROM cost_allocations',one=True)['account']=='research-pro'


def test_global_safety_freeze_blocks_new_pro_work_but_preserves_accepted(client,owner):
    uid=user();activate(owner,uid);accepted=reserve(uid,'accepted-before-freeze',1)
    with db.tx() as c:c.execute("INSERT INTO controls VALUES('global_freeze','true')")
    assert not entitlements.remaining(uid)['admission_open']
    assert reserve(uid,'accepted-before-freeze',1)['id']==accepted['id']
    with pytest.raises(budget.BudgetFull,match='safety freeze'):reserve(uid,'new-after-freeze',1)
    budget.reconcile('accepted-before-freeze',uid,'scientific',.5)
    assert db.query('SELECT charged_micro FROM cost_reservations',one=True)['charged_micro']==500000


def test_internal_validation_is_bounded_and_still_charged_to_baseline(client,owner):
    uid=user()
    with db.tx() as c:c.execute('INSERT INTO controls VALUES(?,?)',('validation-tenant:'+uid,'true'))
    activate(owner,uid,4);grant_before=db.query('SELECT body FROM research_allowances WHERE user_id=?',(uid,),one=True)['body']
    reserve(uid,'existing-validation',.1)
    assert entitlements.limits(uid)['monthly_budget_usd']==4
    assert db.query('SELECT account FROM cost_allocations',one=True)['account']=='validation'
    assert budget.snapshot()['reserved_usd']==.1
    with pytest.raises(ValueError,match='at most'):activate(owner,uid,500)
    assert db.query('SELECT body FROM research_allowances WHERE user_id=?',(uid,),one=True)['body']==grant_before
    with db.tx() as c:c.execute('INSERT INTO controls VALUES(?,?)',('baseline-admission-hold:'+budget.period(),'true'))
    with pytest.raises(budget.BudgetFull):reserve(uid,'validation-after-baseline-pause',.1)
    assert not entitlements.remaining(uid)['admission_open']


def test_provisioning_keeps_500_user_and_4_validation_budgets_independent(client,owner,monkeypatch):
    from backend import provisioning
    normal,internal=user(),user();activate(owner,normal)
    with db.tx() as c:c.execute('INSERT INTO controls VALUES(?,?)',('validation-tenant:'+internal,'true'))
    activate(owner,internal,4);created=[]
    class CloudFormation:
        def create_stack(self,**request):created.append(request);return {'StackId':'test-stack'}
    class Session:
        def client(self,name):assert name=='cloudformation';return CloudFormation()
    monkeypatch.setattr(provisioning,'aws_session',lambda:Session())
    monkeypatch.setenv('NGS_RESEARCH_TEMPLATE_URL','https://example.test/reviewed-template')
    monkeypatch.setenv('NGS_RESEARCH_CFN_ROLE','arn:test:reviewed-cfn-role')
    assert provisioning.once()
    configurations={next(p['ParameterValue'] for p in r['Parameters'] if p['ParameterKey']=='TenantId'):
                    {p['ParameterKey']:p['ParameterValue'] for p in r['Parameters']} for r in created}
    assert configurations[normal]['MonthlyBudget']=='500'
    assert configurations[internal]['MonthlyBudget']=='4'
    assert all('TemplateURL' in r and r['Tags'][1]['Key']=='TenantId' for r in created)
    reservations=db.query('SELECT * FROM cost_reservations')
    assert len(reservations)==2 and all(r['kind']=='retained-capacity' for r in reservations)
    assert sum(r['reserved_micro'] for r in reservations)==460000  # No $504 grant reservation.
