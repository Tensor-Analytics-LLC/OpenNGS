"""Exact checkpoint discovery preserves normal verifier leases and authority."""
import hashlib
import io
import json
from collections import OrderedDict

import pytest
from botocore.exceptions import ClientError

from backend import db, migration


def provider_error(code,status=404):
    return ClientError({'Error':{'Code':code},'ResponseMetadata':{'HTTPStatusCode':status}},'GetObject')


@pytest.fixture
def exact_discovery(owner,monkeypatch):
    with db.tx() as c:
        c.execute('DELETE FROM migration_objects')
        c.execute('DELETE FROM migration_assets')
    pid='migration_'+'a'*24;content=b'unchanged original database\n';sha=hashlib.sha256(content).hexdigest()
    obj={'sha256':sha,'bytes':len(content),'dataset_id':'ds_migration_exact',
         'key':'tenants/'+owner['id']+'/datasets/ds_migration_exact/database.txt',
         'aliases':[{'user_id':owner['id'],'bytes':len(content)}]}
    plan={'plan_id':pid,'owner_id':owner['id'],'bucket':'private-test','objects':[obj],'total_bytes':len(content)}
    raw=db.encode(plan).encode();plan_key=f'operations/checkpoints/migration/{pid}/plan.json'
    registration={'plan_id':pid,'owner_id':owner['id'],'bucket':plan['bucket'],'key':plan_key,
                  'version_id':'exact-plan-version','sha256':hashlib.sha256(raw).hexdigest()}
    receipt={'schema':1,'plan_id':pid,'sha256':sha,'bytes':len(content),'key':obj['key'],'version_id':'exact-data-version'}
    class S3:
        def __init__(self):
            self.gets=[];self.heads=[];self.error=None;self.head_error=None
            self.receipt=receipt.copy();self.head={'ContentLength':obj['bytes'],'VersionId':receipt['version_id'],'Metadata':{'sha256':sha}}
            self.on_head=None;self.extra_bytes=0;self.announced_bytes=None
        def get_object(self,**request):
            self.gets.append(request)
            if request['Key']==plan_key:
                assert request=={'Bucket':plan['bucket'],'Key':plan_key,'VersionId':registration['version_id']}
                return {'Body':io.BytesIO(raw),'ContentLength':len(raw)}
            assert request=={'Bucket':plan['bucket'],'Key':f'operations/checkpoints/migration/{pid}/uploaded/{sha}.json'}
            if self.error:raise self.error
            body=db.encode(self.receipt).encode()+b' '*self.extra_bytes
            return {'Body':io.BytesIO(body),'ContentLength':len(body) if self.announced_bytes is None else self.announced_bytes}
        def head_object(self,**request):
            self.heads.append(request)
            assert request=={'Bucket':plan['bucket'],'Key':obj['key'],'VersionId':self.receipt['version_id']}
            if self.on_head:self.on_head()
            if self.head_error:raise self.head_error
            return self.head
    s3=S3();monkeypatch.setenv('NGS_BUCKET',plan['bucket'])
    monkeypatch.setattr(migration,'_PLAN_CACHE',OrderedDict())
    monkeypatch.setattr(migration,'client',lambda:s3)
    migration.register_plan(owner['id'],registration)
    s3.gets.clear()
    return plan,obj,registration,s3


def insert_existing(plan,obj,state='Verifying',lease=0):
    record=(plan['plan_id'],obj['sha256'],plan['owner_id'],state,
            db.encode({'version_id':'original-record-version','_verification_attempts':3,'retained_receipt':'unchanged'}),
            123,'original-record-version',lease,'retained error',1)
    with db.tx() as c:c.execute('INSERT INTO migration_objects VALUES(?,?,?,?,?,?,?,?,?,?)',record)
    return db.query('SELECT * FROM migration_objects WHERE plan_id=? AND sha256=?',record[:2],one=True)


def test_exact_discovery_fetches_only_deterministic_receipt_and_enqueues_verification(exact_discovery):
    plan,obj,registration,s3=exact_discovery
    result=migration.discover_one(plan['plan_id'],obj['sha256'])
    assert result['state']=='Verifying' and result['lease_until']==0 and result['verified_bytes']==0
    assert result['version_id']=='exact-data-version' and json.loads(result['receipt'])==s3.receipt
    assert len(s3.gets)==len(s3.heads)==1
    assert not db.query('SELECT * FROM datasets') and not db.query('SELECT * FROM migration_assets')
    assert not db.query("SELECT * FROM controls WHERE key LIKE 'migration:cursor:%'")
    assert not db.query("SELECT * FROM controls WHERE key LIKE 'migration:summary:%'")


@pytest.mark.parametrize('state,lease',[('Ready',0),('Blocked',0),('Verifying',9999999999)])
def test_existing_ready_blocked_or_leased_rows_are_returned_without_receipt_reads_or_changes(exact_discovery,state,lease):
    plan,obj,_,s3=exact_discovery;original=insert_existing(plan,obj,state,lease)
    s3.error=AssertionError('Existing verifier records must never refetch/replace their receipt')
    assert migration.discover_one(plan['plan_id'],obj['sha256'])==original
    assert db.query('SELECT * FROM migration_objects',one=True)==original
    assert not s3.gets and not s3.heads


def test_concurrent_daemon_insert_wins_without_resetting_lease_attempts_or_receipt(exact_discovery):
    plan,obj,_,s3=exact_discovery;winner=[]
    def race():winner.append(insert_existing(plan,obj,'Blocked',9999999999))
    s3.on_head=race
    result=migration.discover_one(plan['plan_id'],obj['sha256'])
    assert result==winner[0]
    assert json.loads(result['receipt'])['_verification_attempts']==3
    assert db.query('SELECT * FROM migration_objects')==winner


@pytest.mark.parametrize('args',[(None,'b'*64),('migration_'+'a'*24,None),('s3://another/receipt','b'*64),
                                 ('migration_'+'a'*24,'B'*64),('migration_'+'a'*24,'b'*64+'/other')])
def test_exact_discovery_rejects_non_exact_ids_before_cloud_access(client,monkeypatch,args):
    monkeypatch.setattr(migration,'client',lambda:pytest.fail('Invalid identities must never address S3'))
    with pytest.raises(ValueError,match='migration plan ID and SHA-256'):migration.discover_one(*args)


def test_unregistered_plan_or_unknown_object_does_not_address_receipts(exact_discovery):
    plan,obj,_,s3=exact_discovery
    with pytest.raises(ValueError,match='not registered'):migration.discover_one('migration_'+'b'*24,obj['sha256'])
    with pytest.raises(ValueError,match='outside'):migration.discover_one(plan['plan_id'],'f'*64)
    assert not s3.gets and not s3.heads


@pytest.mark.parametrize('field,value',[('key','elsewhere/plan.json'),('bucket','another-bucket'),('plan_id','migration_'+'b'*24),
                                       ('sha256','not-a-sha'),('version_id','null'),('version_id',None)])
def test_forged_registration_cannot_address_an_arbitrary_plan(exact_discovery,field,value):
    plan,obj,registration,s3=exact_discovery;forged={**registration,field:value}
    with db.tx() as c:c.execute('UPDATE controls SET value=? WHERE key=?',(db.encode(forged),'migration:plan:'+plan['plan_id']))
    with pytest.raises(ValueError,match='registered immutable'):migration.discover_one(plan['plan_id'],obj['sha256'])
    assert not s3.gets and not s3.heads


def test_cold_plan_digest_validation_precedes_any_receipt_lookup(exact_discovery):
    plan,obj,registration,s3=exact_discovery;migration._PLAN_CACHE.clear()
    with db.tx() as c:c.execute('UPDATE controls SET value=? WHERE key=?',(db.encode({**registration,'sha256':'f'*64}),'migration:plan:'+plan['plan_id']))
    with pytest.raises(ValueError,match='identity verification'):migration.discover_one(plan['plan_id'],obj['sha256'])
    assert len(s3.gets)==1 and s3.gets[0]['VersionId']==registration['version_id']
    assert not s3.heads and not db.query('SELECT * FROM migration_objects')


@pytest.mark.parametrize('field,value',[('plan_id','migration_'+'b'*24),('key','tenants/other/private'),('sha256','f'*64),('bytes',1),
                                      ('bytes',True),('version_id',None),('version_id',''),('version_id','null'),('version_id',[]),
                                      ('version_id','injected\nheader'),('version_id','x'*1025)])
def test_forged_or_unversioned_receipt_never_enters_verifier_queue(exact_discovery,field,value):
    plan,obj,_,s3=exact_discovery;s3.receipt[field]=value
    with pytest.raises(ValueError,match='checkpoint differs'):migration.discover_one(plan['plan_id'],obj['sha256'])
    assert not s3.heads and not db.query('SELECT * FROM migration_objects')


@pytest.mark.parametrize('field,value',[('VersionId','another-version'),('ContentLength',1),('Metadata',{'sha256':'f'*64})])
def test_destination_head_must_match_exact_receipt_version_and_identity(exact_discovery,field,value):
    plan,obj,_,s3=exact_discovery;s3.head[field]=value
    with pytest.raises(ValueError,match='destination version differs'):migration.discover_one(plan['plan_id'],obj['sha256'])
    assert not db.query('SELECT * FROM migration_objects')


@pytest.mark.parametrize('code',['404','NoSuchKey','NotFound'])
def test_only_missing_receipt_returns_not_transferred(exact_discovery,code):
    plan,obj,_,s3=exact_discovery;s3.error=provider_error(code)
    assert migration.discover_one(plan['plan_id'],obj['sha256']) is None
    assert not s3.heads and not db.query('SELECT * FROM migration_objects')


@pytest.mark.parametrize('code,status',[('403',403),('AccessDenied',403),('NoSuchKey',403),('SlowDown',503),('NoSuchVersion',404)])
def test_permission_and_other_provider_errors_are_not_reported_as_missing(exact_discovery,code,status):
    plan,obj,_,s3=exact_discovery;error=provider_error(code,status);s3.error=error
    with pytest.raises(ClientError) as caught:migration.discover_one(plan['plan_id'],obj['sha256'])
    assert caught.value is error and not db.query('SELECT * FROM migration_objects')


def test_receipt_with_missing_destination_version_is_an_explicit_provider_error(exact_discovery):
    plan,obj,_,s3=exact_discovery;error=provider_error('NoSuchVersion');s3.head_error=error
    with pytest.raises(ClientError) as caught:migration.discover_one(plan['plan_id'],obj['sha256'])
    assert caught.value is error and not db.query('SELECT * FROM migration_objects')


@pytest.mark.parametrize('announce_length',[None,10])
def test_receipt_is_bounded_even_when_provider_understates_content_length(exact_discovery,announce_length):
    plan,obj,_,s3=exact_discovery;s3.extra_bytes=16385;s3.announced_bytes=announce_length
    with pytest.raises(ValueError,match='bounded metadata size'):migration.discover_one(plan['plan_id'],obj['sha256'])
    assert not s3.heads and not db.query('SELECT * FROM migration_objects')
