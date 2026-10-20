import base64
import hashlib
import json

import pytest
from botocore.exceptions import ClientError
from backend import curated_cloud, data, db, entitlements
from test_entitlements import allowance


class S3:
    def __init__(self):self.objects={};self.puts=0
    def head_object(self,**args):
        if args['Key'] not in self.objects:raise ClientError({'Error':{'Code':'404'}},'HeadObject')
        return self.objects[args['Key']]
    def put_object(self,**args):
        body=args['Body'].read();digest=hashlib.sha256(body).digest();self.puts+=1
        assert base64.b64encode(digest).decode()==args['ChecksumSHA256']
        assert args['ServerSideEncryption']=='aws:kms'
        self.objects[args['Key']]={'ContentLength':len(body),'VersionId':'version-'+str(self.puts),'ChecksumSHA256':args['ChecksumSHA256'],'Metadata':args['Metadata']}
        return {'VersionId':'version-'+str(self.puts)}


def configure(owner,monkeypatch):
    entitlements.grant(owner,owner['id'],{'reason':'Fund exact curated cloud source validation','funding_confirmed':True,'allowance':allowance()})
    monkeypatch.setattr(data,'settings',lambda uid:{'s3_bucket':'tenant-cache','kms_key_arn':'arn:test:key','research_profile':{'state':'Ready'}})
    s3=S3()
    class Session:
        def client(self,name):assert name=='s3';return s3
    monkeypatch.setattr(curated_cloud,'aws_session',lambda:Session())
    return s3


def test_exact_curated_release_is_tenant_owned_idempotent_and_has_real_companions(client,owner,monkeypatch):
    s3=configure(owner,monkeypatch);result=curated_cloud.publish(owner,owner['id'])
    assert result['state']=='Ready' and len(result['sources'])==7
    reads=curated_cloud.outputs(owner['id'],'sample.fastq')['reads'][0]
    assert reads['metadata']['reference_contig']=='engineering_contig'
    assert 'reference' not in reads['metadata']
    assert reads['metadata']['mate'].startswith('s3://tenant-cache/tenants/'+owner['id']+'/datasets/')
    assert reads['metadata']['mate_sha256'] and reads['metadata']['mate_version_id']
    assert reads['metadata']['secondary_objects'][0]['bytes']>0
    before=s3.puts;curated_cloud.publish(owner,owner['id']);assert s3.puts==before
    assert len(db.query('SELECT * FROM datasets'))==result['objects']
    with pytest.raises(ValueError,match='has not been staged'):curated_cloud.outputs('other-tenant','sample.fastq')


def test_cloud_source_rejects_different_version_and_missing_funding(client,owner,monkeypatch):
    with pytest.raises(ValueError,match='funded workspace'):curated_cloud.publish(owner,owner['id'])
    s3=configure(owner,monkeypatch);curated_cloud.publish(owner,owner['id'])
    next(iter(s3.objects.values()))['VersionId']='changed-version'
    with pytest.raises(ValueError,match='immutable identity'):curated_cloud.publish(owner,owner['id'])


def test_source_output_snapshot_survives_later_catalogue_record_updates(client,owner,monkeypatch):
    configure(owner,monkeypatch);curated_cloud.publish(owner,owner['id'])
    first=curated_cloud.outputs(owner['id'],'sample.fastq');first['reads'][0]['path']='untrusted alteration'
    assert curated_cloud.outputs(owner['id'],'sample.fastq')['reads'][0]['path']!='untrusted alteration'
    with db.tx() as c:
        row=c.execute('SELECT value FROM controls WHERE key=?',('curated-cloud:'+owner['id'],)).fetchone();value=json.loads(row['value']);value['manifest_sha256']='0'*64
        c.execute('UPDATE controls SET value=? WHERE key=?',(db.encode(value),'curated-cloud:'+owner['id']))
    with pytest.raises(ValueError,match='exact curated source'):curated_cloud.outputs(owner['id'],'sample.fastq')


def test_funded_admission_freezes_exact_curated_sources_and_rolls_back_missing_release(client,owner,monkeypatch):
    from backend import graph,runner
    configure(owner,monkeypatch);curated_cloud.publish(owner,owner['id'])
    monkeypatch.setattr(data,'settings',lambda uid:{**data.DEFAULTS,'backend':'aws-batch','s3_bucket':'tenant-cache','research_profile':{'state':'Ready'}})
    monkeypatch.setattr(runner,'CLOUD',True)
    g={'name':'Curated cloud admission','nodes':[{'id':'source','tool':'sample.fastq','params':{},'resources':{'threads':1,'memory_mb':1024,'timeout_seconds':30}}],'edges':[],'maximum_output_bytes':1024**2}
    monkeypatch.setattr(graph,'preflight',lambda *args:{'ok':True,'graph_digest':graph.graph_digest(g),'reservation_cpu_seconds':30,'order':['source']})
    run,created=runner.admit(owner,g,'curated-exact-admission')
    frozen=json.loads(run['graph'])['_execution']['curated_sources']['sample.fastq']
    assert created and frozen['reads'][0]['metadata']['mate_version_id']
    with db.tx() as c:c.execute('DELETE FROM controls WHERE key=?',('curated-cloud:'+owner['id'],))
    assert frozen['reads'][0]['metadata']['curated_manifest_sha256']
    before=len(db.query('SELECT * FROM cost_reservations'))
    with pytest.raises(ValueError,match='has not been staged'):runner.admit(owner,g,'missing-curated-admission')
    assert len(db.query('SELECT * FROM cost_reservations'))==before
