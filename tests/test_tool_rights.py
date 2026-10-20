import json
from backend import db


def test_exact_tool_permission_is_audited_and_does_not_grant_compute_or_runtime(client,owner):
    body={'approved':True,'confirm_authorization':True,'terms_url':'https://provider.example.test/academic-terms','reason':'Verified institutional noncommercial authorization'}
    response=client.post('/api/admin/tool-rights/'+owner['id']+'/alphamissense',json=body)
    assert response.status_code==200,response.text
    settings=client.get('/api/settings').json()
    assert settings['rights_approved']==['alphamissense']
    assert client.get('/api/account/entitlements').json()['access']=='explore'
    assert db.query('SELECT * FROM cost_reservations')==[] and db.query('SELECT * FROM secrets')==[]
    assert any(json.loads(row['body'])['action']=='tool.permission.recorded' for row in db.query('SELECT body FROM audit'))
    body.update(approved=False,confirm_authorization=False,reason='Institutional authorization has expired')
    assert client.post('/api/admin/tool-rights/'+owner['id']+'/alphamissense',json=body).status_code==200
    assert client.get('/api/settings').json()['rights_approved']==[]


def test_user_or_unconfirmed_grants_and_retired_targets_are_denied(client,owner):
    url='/api/admin/tool-rights/'+owner['id']+'/alphamissense'
    body={'approved':True,'confirm_authorization':False,'terms_url':'https://provider.example.test/terms','reason':'Review academic licence permission'}
    assert client.post(url,json=body).status_code==422
    body.update(confirm_authorization='false')
    assert client.post(url,json=body).status_code==422
    body.update(confirm_authorization=True,terms_url='http://insecure.example/terms')
    assert client.post(url,json=body).status_code==422
    body.update(terms_url='https://provider.example.test/terms')
    assert client.post('/api/admin/tool-rights/missing/alphamissense',json=body).status_code==422
    assert client.post('/api/admin/tool-rights/'+owner['id']+'/fastqc',json=body).status_code==422
    with db.tx() as c:c.execute("UPDATE users SET role='user' WHERE id=?",(owner['id'],))
    assert client.post(url,json=body).status_code==403
