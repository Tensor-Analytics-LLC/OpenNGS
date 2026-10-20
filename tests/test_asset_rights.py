import json
import time

import pytest

from backend import asset_rights, db, graph, runner
from backend.config import DATA
from test_studio import wait_run


def insert_asset(uid, tmp_path, name='dbNSFP5.3.1a_grch38.gz', family='dbnsfp', fmt='BGZIP_VCF'):
    path=tmp_path/name;path.write_bytes(b'fixture')
    row={'id':db.ident('rights_'),'user_id':uid,'path':str(path),'name':name,'format':fmt,
         'bytes':7,'mtime':time.time(),'family':family,'assembly':'GRCh38',
         'metadata':db.encode({'sha256':'a'*64,'version_id':'v1'})}
    with db.tx() as c:c.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?,?)',tuple(row.values()))
    return row


def grant(owner, row):
    requirement=asset_rights.disclosure(owner['id'],row)['requirements'][0]
    return asset_rights.acknowledge(owner,owner['id'],row['id'],{
        'identity':requirement['identity'],'permitted_use':requirement['permitted_uses'][0],
        'terms_url':requirement['terms_url'] or 'https://example.org/exact-release-terms',
        'reason':'Explicit test-only authorization record for noncommercial academic analysis.',
        'confirm_authorization':True})


def preflight(uid, row, owner):
    return graph.preflight({'nodes':[{'id':'source','tool':'asset.file','params':{'asset_id':row['id']}}],'edges':[]},owner)


def test_generic_source_cannot_bypass_exact_database_rights(client, owner, tmp_path):
    row=insert_asset(owner['id'],tmp_path)
    info=client.get('/api/assets/'+row['id']+'/rights').json()
    assert info['label']=='Academic / noncommercial'
    assert info['release']=='5.3.1a'
    assert not info['execution_allowed']
    assert any(x['code']=='DATA_RIGHTS_REQUIRED' for x in preflight(owner['id'],row,owner)['issues'])
    with pytest.raises(ValueError,match='Data rights review'):
        runner.input_asset(owner['id'],row['id'])
    assert grant(owner,row)['execution_allowed']
    assert not any(x['code']=='DATA_RIGHTS_REQUIRED' for x in preflight(owner['id'],row,owner)['issues'])


def test_changed_digest_or_version_invalidates_acknowledgment(client, owner, tmp_path):
    row=insert_asset(owner['id'],tmp_path)
    grant(owner,row)
    for changed in [{'sha256':'b'*64,'version_id':'v1'},{'sha256':'a'*64,'version_id':'v2'}]:
        with db.tx() as c:c.execute('UPDATE assets SET metadata=? WHERE id=?',(db.encode(changed),row['id']))
        current=asset_rights.owned(owner['id'],row['id'])
        assert not asset_rights.disclosure(owner['id'],current)['execution_allowed']
        with pytest.raises(ValueError,match='Data rights review'):asset_rights.require(owner['id'],current)


def test_derived_alias_keeps_rights_and_live_revocation(client, owner, tmp_path):
    row=insert_asset(owner['id'],tmp_path);grant(owner,row)
    obligations=asset_rights.require(owner['id'],row)
    alias={'name':'renamed-results.vcf','format':'VCF','metadata':{'data_rights':obligations}}
    assert asset_rights.require(owner['id'],alias)==obligations
    asset_rights.revoke(owner,owner['id'],obligations[0]['identity'],'Rights agreement was revoked')
    with pytest.raises(ValueError,match='5.3.1a'):asset_rights.require_inputs(owner['id'],{'variants':[alias]})


def test_old_artifact_snapshot_retains_source_restrictions(client, owner, tmp_path):
    row=insert_asset(owner['id'],tmp_path)
    rid=db.ident('run_')
    with db.tx() as c:
        c.execute('INSERT INTO runs(id,user_id,operation_id,state,graph,created,reserved) VALUES(?,?,?,?,?,?,?)',
                  (rid,owner['id'],rid,'succeeded',db.encode({'_execution':{'assets':{'source':row}}}),time.time(),0))
    artifact={'run_id':rid,'name':'renamed-output.vcf','format':'VCF','metadata':{'data_rights':[]}}
    with pytest.raises(ValueError,match='5.3.1a'):asset_rights.require(owner['id'],artifact)
    grant(owner,row)
    assert len(asset_rights.require(owner['id'],artifact))==1


def test_renamed_identical_bytes_keep_known_source_restrictions(client, owner, tmp_path):
    original=insert_asset(owner['id'],tmp_path)
    renamed=insert_asset(owner['id'],tmp_path,'ordinary.vcf','cloud','VCF')
    with pytest.raises(ValueError,match='5.3.1a'):
        asset_rights.require(owner['id'],renamed)
    grant(owner,original)
    assert asset_rights.disclosure(owner['id'],renamed)['execution_allowed']
    changed={**renamed,'metadata':db.encode({'sha256':'a'*64,'version_id':'v2'})}
    assert not asset_rights.disclosure(owner['id'],changed)['execution_allowed']


def test_migration_original_family_survives_display_rename(client, owner, tmp_path):
    original=insert_asset(owner['id'],tmp_path);now=time.time();dataset=db.ident('ds_')
    with db.tx() as c:
        c.execute('INSERT INTO datasets VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                  (dataset,owner['id'],original['id'],original['name'],7,'a'*64,'Ready','key','v1','{}',now,now))
        c.execute('INSERT INTO migration_assets VALUES(?,?,?,?,?,?)',
                  (original['id'],owner['id'],dataset,original['id'],original['path'],db.encode(original)))
        c.execute("UPDATE assets SET name='ordinary.vcf',family='cloud',format='VCF' WHERE id=?",(original['id'],))
    renamed=asset_rights.owned(owner['id'],original['id'])
    with pytest.raises(ValueError,match='5.3.1a'):
        asset_rights.require(owner['id'],renamed)


def test_unknown_database_review_and_public_reference_distinction(client, owner, tmp_path):
    unknown=insert_asset(owner['id'],tmp_path,'unclear.db','annotation','DATABASE')
    assert asset_rights.disclosure(owner['id'],unknown)['required']
    reference=insert_asset(owner['id'],tmp_path,'GRCh38.fa','reference','FASTA')
    reference['metadata']=db.encode({'sha256':'c'*64,'version_id':'v1'})
    with db.tx() as c:c.execute('UPDATE assets SET metadata=? WHERE id=?',(reference['metadata'],reference['id']))
    assert not asset_rights.disclosure(owner['id'],reference)['required']
    assert asset_rights.require(owner['id'],reference)==[]
    assert asset_rights.describe({'name':'sample.csv','format':'CSV','metadata':{}})['required'] is False


def test_admin_ack_requires_explicit_authorization_and_cannot_change_identity(client, owner, tmp_path):
    row=insert_asset(owner['id'],tmp_path)
    requirement=asset_rights.disclosure(owner['id'],row)['requirements'][0]
    payload={'identity':requirement['identity'],'permitted_use':'academic-noncommercial',
             'terms_url':requirement['terms_url'],'reason':'Institutional research usage reviewed for these exact bytes.'}
    endpoint='/api/admin/data-rights/'+owner['id']+'/'+row['id']
    assert client.post(endpoint,json=payload).status_code==422
    payload['confirm_authorization']=True
    assert client.post(endpoint,json={**payload,'identity':'different-version'}).status_code==422
    assert client.post(endpoint,json=payload).status_code==200
    user=client.post('/api/auth/signup',json={'email':'reader-rights@example.test','password':'Reader-test-password-777!'}).json()
    client.headers['X-CSRF-Token']=user['csrf']
    assert client.post(endpoint,json=payload).status_code==403
    assert client.get('/api/assets/'+row['id']+'/rights').status_code==422


def test_real_routing_propagates_rights_and_revocation_blocks_admitted_cached_run(client, owner, monkeypatch):
    folder=DATA/('rights-'+owner['id']);folder.mkdir()
    row=insert_asset(owner['id'],folder);grant(owner,row)
    g={'nodes':[{'id':'source','tool':'asset.file','params':{'asset_id':row['id']}},
                {'id':'export','tool':'utility.export'}],
       'edges':[{'id':'edge','source':'source','sourcePort':'data','target':'export','targetPort':'data'}]}
    response=client.post('/api/runs',json={'graph':g},headers={'Idempotency-Key':'rights-propagation'})
    assert response.status_code==200,response.text
    result=wait_run(client,response.json()['run']['id'])
    assert result['run']['state']=='succeeded',result['run']['error']
    output=next(a for a in result['artifacts'] if a['node_id']=='export')
    output=client.get('/api/artifacts/'+output['id']+'?metadata=1').json()
    obligations=output['metadata']['data_rights']
    assert obligations and obligations[0]['release']=='5.3.1a'
    monkeypatch.setattr(runner.POOL,'submit',lambda *args:None)
    admitted,_=runner.admit(owner,g,'rights-revoked-before-worker')
    asset_rights.revoke(owner,owner['id'],obligations[0]['identity'],'Authorization withdrawn before queued execution')
    runner.run(admitted['id'],['source','export'])
    failed=db.query('SELECT state,error FROM runs WHERE id=?',(admitted['id'],),one=True)
    assert failed['state']=='failed' and 'Data rights review' in failed['error']
    with pytest.raises(ValueError,match='Data rights review'):
        runner.input_asset(owner['id'],output['id'])
