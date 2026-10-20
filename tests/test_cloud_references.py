import base64
import copy
import hashlib
import json

import pytest
from botocore.exceptions import ClientError
from backend import cloud_references as refs, data, db, entitlements
from test_entitlements import allowance


def acquisition(monkeypatch):
    monkeypatch.setenv('NGS_BUCKET', 'reviewed-cache')
    objects=[]
    for i,(bucket,key) in enumerate(refs.REVIEWED):
        digest=hashlib.sha256(key.encode()).digest()
        objects.append({'source_bucket':bucket,'source_key':key,'bucket':'reviewed-cache',
                        'key':'operations/checkpoints/references/'+bucket+'/'+key,
                        'version_id':'source-'+str(i),'bytes':100+i,'checksum_type':'FULL_OBJECT',
                        'checksum_sha256':base64.b64encode(digest).decode()})
    return {'completed':True,'objects':objects,'total_bytes':sum(o['bytes'] for o in objects)}


class S3:
    def __init__(self, record, corrupt=False):
        self.heads={(o['bucket'],o['key']):{'ContentLength':o['bytes'],'ChecksumSHA256':o['checksum_sha256'],
                    'ChecksumType':'FULL_OBJECT','VersionId':o['version_id'],'ETag':'exact-etag'} for o in record['objects']}
        self.copies=0
        self.corrupt=corrupt

    def head_object(self, **args):
        head=self.heads.get((args['Bucket'],args['Key']))
        if head is None:
            raise ClientError({'Error':{'Code':'404'}},'HeadObject')
        return dict(head)

    def copy_object(self, **args):
        source=args['CopySource'];self.copies+=1
        assert args['CopySourceIfMatch']=='exact-etag'
        assert args['ServerSideEncryption']=='aws:kms'
        head={**self.heads[(source['Bucket'],source['Key'])], 'VersionId':'tenant-'+str(self.copies), 'Metadata':args['Metadata']}
        if self.corrupt:head['ContentLength']+=1
        self.heads[(args['Bucket'],args['Key'])]=head
        return {'VersionId':head['VersionId']}


def configure(owner, record, monkeypatch, corrupt=False):
    entitlements.grant(owner,owner['id'],{'reason':'Owner funded exact reference publication','funding_confirmed':True,'allowance':allowance()})
    with db.tx() as c:
        c.execute('INSERT INTO settings VALUES(?,?)',(owner['id'],db.encode({'s3_bucket':'private-tenant','kms_key_arn':'arn:test:key','research_profile':{'state':'Ready'}})))
    s3=S3(record,corrupt)
    class Session:
        def client(self,name,**kwargs):
            assert name=='s3'
            return s3
    monkeypatch.setattr(refs,'aws_session',lambda:Session())
    return s3


def test_publication_is_dry_by_default_and_never_accepts_arbitrary_sources(client,owner,monkeypatch):
    record=acquisition(monkeypatch);s3=configure(owner,record,monkeypatch)
    prepared=refs.publish(owner,owner['id'],record)
    assert prepared['state']=='planned' and not prepared['aws_mutations'] and s3.copies==0
    assert db.query('SELECT * FROM datasets')==[]
    for changed in [dict(record,completed=False),dict(record,objects=record['objects'][:-1])]:
        with pytest.raises(ValueError):refs.publish(owner,owner['id'],changed,execute=True)
    changed=copy.deepcopy(record);changed['objects'][0]['key']='tenants/foreign/private.fa'
    with pytest.raises(ValueError,match='reviewed platform'):refs.publish(owner,owner['id'],changed,execute=True)
    assert s3.copies==0


def test_exact_copies_commit_complete_companions_and_retry_without_recopying(client,owner,monkeypatch):
    record=acquisition(monkeypatch);s3=configure(owner,record,monkeypatch)
    result=refs.publish(owner,owner['id'],record,execute=True)
    assert result['state']=='Ready' and s3.copies==6
    rows=db.query('SELECT * FROM datasets');assert len(rows)==6 and all(r['state']=='Ready' and r['version_id'].startswith('tenant-') for r in rows)
    fasta=db.query("SELECT * FROM assets WHERE format='FASTA'",one=True);meta=json.loads(fasta['metadata'])
    assert len(meta['secondary_objects'])==2 and meta['index_sha256'] and meta['dictionary_version_id']
    assert all(o['uri'].startswith('s3://private-tenant/tenants/'+owner['id']+'/') for o in meta['secondary_objects'])
    assert refs.publish(owner,owner['id'],record,execute=True)['additional_dataset_bytes']==0
    assert s3.copies==6 and len(db.query('SELECT * FROM cost_reservations'))==1


def test_corrupt_copy_never_becomes_ready_and_holds_are_preserved(client,owner,monkeypatch):
    record=acquisition(monkeypatch);s3=configure(owner,record,monkeypatch,corrupt=True)
    with pytest.raises(ValueError,match='checksum differs'):refs.publish(owner,owner['id'],record,execute=True)
    assert s3.copies==1 and db.query('SELECT * FROM assets')==[]
    assert all(r['state']=='Provisioning' for r in db.query('SELECT * FROM datasets'))
    assert db.query('SELECT state FROM cost_reservations',one=True)['state']=='reserved'


def test_only_funded_workspace_and_active_admin_can_publish(client,owner,monkeypatch):
    record=acquisition(monkeypatch)
    with pytest.raises(ValueError,match='funded Research Pro'):refs.publish(owner,owner['id'],record,execute=True)
    with pytest.raises(ValueError,match='active administrator'):refs.publish({**owner,'role':'user'},owner['id'],record,execute=True)
    with pytest.raises(ValueError,match='active administrator'):refs.publish({**owner,'frozen':True},owner['id'],record,execute=True)
