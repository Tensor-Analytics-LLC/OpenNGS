import json
import time

from backend import accounts, db, security


def managed_signup():
    with db.tx() as c:
        c.execute("INSERT INTO controls VALUES('accounts:admin_only','true')")


def test_admin_creates_explore_account_without_funding_or_public_signup(client, owner):
    managed_signup()
    assert client.get('/api/bootstrap').json()['signup_allowed'] is False
    assert client.post('/api/auth/signup', json={
        'email': 'visitor@example.test', 'password': 'Visitor-password-123!'}).status_code == 403
    response = client.post('/api/admin/users', json={
        'email': 'Researcher@example.test', 'password': 'Researcher-password-123!',
        'reason': 'Invited laboratory researcher'})
    assert response.status_code == 200, response.text
    created = response.json()['user']
    assert created['email'] == 'researcher@example.test'
    assert created['role'] == 'user' and created['entitlements']['access'] == 'explore'
    assert 'password' not in response.text
    assert db.query('SELECT * FROM research_allowances WHERE user_id=?', (created['id'],)) == []
    assert client.post('/api/admin/users', json={
        'email': created['email'], 'password': 'Researcher-password-123!',
        'reason': 'Duplicate account attempt'}).status_code == 422
    login = client.post('/api/auth/login', json={'email': created['email'], 'password': 'Researcher-password-123!'})
    assert login.status_code == 200
    client.headers['X-CSRF-Token'] = login.json()['csrf']
    assert client.post('/api/admin/users', json={
        'email': 'intruder@example.test', 'password': 'Researcher-password-123!',
        'reason': 'Not an administrator'}).status_code == 403


def test_empty_account_deleted_and_last_admin_preserved(client, owner):
    account = accounts.create(owner, {'email': 'temporary@example.test', 'password': 'Temporary-password-123!',
                                     'reason': 'Temporary test account'})['user']
    with db.tx() as c:
        token, _ = security.issue_session(c, account['id'])
    result = accounts.remove(owner, account['id'], 'Remove temporary account')
    assert result['mode'] == 'deleted'
    assert not security.session_for(token)
    assert not db.query('SELECT id FROM users WHERE id=?', (account['id'],))
    response = client.post('/api/admin/users/'+owner['id']+'/remove', json={'reason': 'Attempt self removal'})
    assert response.status_code == 422
    assert client.get('/api/session').status_code == 200


def test_removal_preserves_research_provenance_and_cost_history(client, owner):
    account = accounts.create(owner, {'email': 'past@example.test', 'password': 'Historical-password-123!',
                                     'reason': 'Historical results owner'})['user']
    uid = account['id']
    with db.tx() as c:
        c.execute('INSERT INTO graphs VALUES(?,?,?,?,?,?)', ('saved-history', uid, 'Saved results', 1, '{"nodes":[],"edges":[]}', time.time()))
        c.execute("INSERT INTO cost_reservations(id,user_id,operation,period,kind,reserved_micro,charged_micro,state,details,created) VALUES(?,?,?,?,?,?,?,'reconciled','{}',?)",
                  ('past-cost', uid, 'past-run', '2026-10', 'scientific', 500000, 100000, time.time()))
        token, _ = security.issue_session(c, uid)
    result = accounts.remove(owner, uid, 'Remove account while retaining audit history')
    assert result['mode'] == 'access-removed-history-preserved'
    assert not security.session_for(token)
    assert db.query('SELECT id FROM graphs WHERE user_id=?', (uid,)) == [{'id': 'saved-history'}]
    assert db.query('SELECT charged_micro FROM cost_reservations WHERE id=?', ('past-cost',), one=True)['charged_micro'] == 100000
    assert uid not in {u['id'] for u in client.get('/api/admin').json()['users']}
    assert client.post('/api/auth/login', json={'email': account['email'], 'password': 'Historical-password-123!'}).status_code == 401
    assert security.verify_audit()['valid']
    from backend import entitlements
    from test_entitlements import allowance
    import pytest
    grant=allowance();grant['monthly_budget_usd']=500
    with pytest.raises(ValueError,match='active user'):
        entitlements.grant(owner,uid,{'reason':'Do not fund removed test accounts','funding_confirmed':True,'allowance':grant})


def test_active_job_blocks_account_removal(client, owner):
    account = accounts.create(owner, {'email': 'busy@example.test', 'password': 'Busy-user-password-123!',
                                     'reason': 'User with accepted work'})['user']
    with db.tx() as c:
        c.execute("INSERT INTO runs(id,user_id,operation_id,state,graph,created,reserved) VALUES(?,?,?,'queued','{}',?,0)",
                  ('busy-run', account['id'], 'accepted', time.time()))
    response = client.post('/api/admin/users/'+account['id']+'/remove', json={'reason': 'Remove busy account'})
    assert response.status_code == 422
    assert db.query('SELECT role FROM users WHERE id=?', (account['id'],), one=True)['role'] == 'user'
