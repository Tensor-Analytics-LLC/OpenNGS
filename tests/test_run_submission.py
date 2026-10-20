import json
import time
from backend import db


def test_submission_lookup_is_tenant_bound_and_does_not_reserve(client, owner):
    operation='operation_accepted_but_response_lost'
    body={'name':'original','nodes':[],'edges':[]}
    with db.tx() as c:
        c.execute('INSERT INTO runs(id,user_id,operation_id,state,graph,created,reserved) VALUES(?,?,?,?,?,?,?)',
                  ('run_preserved',owner['id'],operation,'succeeded',json.dumps(body),time.time(),0))
    answer=client.get('/api/run-submissions/'+operation)
    assert answer.status_code==200
    assert answer.json()['run']['id']=='run_preserved' and answer.json()['created'] is False
    assert not db.query('SELECT * FROM cost_reservations')
    other=client.post('/api/auth/signup',json={'email':'other@example.test','password':'Other-test-password-7941!'})
    assert other.status_code==200
    assert client.get('/api/run-submissions/'+operation).status_code==404
    assert client.get('/api/run-submissions/not_here_ever').status_code==404
    assert client.get('/api/run-submissions/short').status_code==404
    assert db.query('SELECT COUNT(*) AS n FROM runs',one=True)['n']==1
