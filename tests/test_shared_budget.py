import pytest
from backend import budget, db, entitlements
from test_entitlements import allowance


def hold(owner, operation='public-reference-acquisition', amount=1):
    with db.tx() as c:
        return budget.reserve_shared(c, owner['id'], operation, 'catalogue-infrastructure', amount,
                                     {'scope': 'Exact public resource acquisition'})


def test_pro_admin_shared_work_counts_only_against_baseline(client, owner):
    body = allowance(); body['monthly_budget_usd'] = 500
    entitlements.grant(owner, owner['id'], {'reason': 'Owner separately funded Pro workspace',
                                         'funding_confirmed': True, 'allowance': body})
    before = budget.snapshot()['available_after_safety_usd']
    record = hold(owner)
    assert budget.snapshot()['available_after_safety_usd'] == pytest.approx(before - 1.18)
    assert entitlements.remaining(owner['id'])['budget_usd'] == 500
    assert db.query('SELECT account FROM cost_allocations WHERE reservation_id=?', (record['id'],), one=True)['account'] == 'shared-platform'
    budget.reconcile('public-reference-acquisition', owner['id'], 'catalogue-infrastructure', .5)
    assert budget.snapshot()['reconciled_variable_usd'] == .5
    assert entitlements.remaining(owner['id'])['budget_usd'] == 500


def test_shared_work_cannot_bypass_baseline_hold_or_tax(client, owner, monkeypatch):
    accepted = hold(owner)
    with db.tx() as c:
        c.execute('INSERT INTO controls VALUES(?,?)', ('baseline-admission-hold:' + budget.period(), 'true'))
    assert hold(owner)['id'] == accepted['id']
    with pytest.raises(budget.BudgetFull):
        hold(owner, 'new-shared-work')
    with db.tx() as c:
        c.execute('DELETE FROM controls WHERE key=?', ('baseline-admission-hold:' + budget.period(),))
    remaining = budget.snapshot()['available_after_safety_usd']
    with pytest.raises(budget.BudgetFull):
        hold(owner, 'tax-overflow', remaining)


def test_shared_work_requires_live_admin_and_cannot_reassign_existing_pool(client, owner):
    with db.tx() as c:
        budget.reserve(c, owner['id'], 'existing-explore', 'catalogue-infrastructure', .1, {})
    with pytest.raises(ValueError, match='another cost pool'):
        hold(owner, 'existing-explore')
    with db.tx() as c:
        c.execute('UPDATE users SET frozen=1 WHERE id=?', (owner['id'],))
    with pytest.raises(ValueError, match='active administrator'):
        hold(owner)
