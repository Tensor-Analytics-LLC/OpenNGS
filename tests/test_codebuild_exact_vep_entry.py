"""Durability/concurrency tests use synthetic bytes and in-memory AWS doubles."""
import base64
import copy
import hashlib
import io
import json

import pytest

from scripts import acquire_exact_vep_archive as acquisition
from scripts import codebuild_exact_vep_entry as entry
from test_acquire_exact_vep_archive import MemoryProvider, MemoryS3
from test_prioritize_migration_objects import error


def build(number):
    return f'arn:aws:codebuild:us-east-1:509505638328:build/openngs-demo-build:12345678-1234-1234-1234-{number:012x}'


def verified(number=1):
    return {'build_arn':build(number),'reservation':'cost_'+'a'*32,'receipt_sha256':'b'*64,
            'receipt_version_id':'immutable-admission','expires_at':2000000000}


class AWS(MemoryS3):
    def __init__(self,key,kms):
        super().__init__();self.key=key;self.kms=kms;self.versions={};self.latest=None
        self.checkpoint_puts=[];self.builds={};self.reads=[];self.ambiguous_checkpoint=False
        self.missing_checkpoint_version=False;self.before_checkpoint_put=None
    def client(self,name,**kwargs):return self
    def head_object(self,**request):
        if request['Key']!=self.key:return super().head_object(**request)
        version=request.get('VersionId',self.latest)
        if not version:raise error('404','HeadObject')
        return copy.deepcopy(self.versions[version]['head'])
    def get_object(self,**request):
        if request['Key']!=self.key:return super().get_object(**request)
        self.reads.append(request);version=request.get('VersionId',self.latest)
        item=self.versions[version]
        return {**copy.deepcopy(item['head']),'Body':io.BytesIO(item['raw'])}
    def put_object(self,**request):
        if request['Key']!=self.key:return super().put_object(**request)
        self.checkpoint_puts.append(request)
        if self.before_checkpoint_put:self.before_checkpoint_put(self,request)
        if self.latest:
            if request.get('IfNoneMatch')=='*' or request.get('IfMatch')!=self.versions[self.latest]['head']['ETag']:
                raise error('PreconditionFailed','PutObject')
        else:assert request.get('IfNoneMatch')=='*'
        raw=request['Body'];digest=hashlib.sha256(raw).digest();checksum=base64.b64encode(digest).decode()
        assert checksum==request['ChecksumSHA256']
        assert request['ServerSideEncryption']=='aws:kms' and request['SSEKMSKeyId']==self.kms
        assert request['BucketKeyEnabled'] is True
        version='checkpoint-'+str(len(self.versions)+1);etag='"'+hashlib.sha256(raw).hexdigest()+'"'
        head={'VersionId':version,'ETag':etag,'ContentLength':len(raw),'ChecksumSHA256':checksum,'ChecksumType':'FULL_OBJECT',
              'ServerSideEncryption':'aws:kms','SSEKMSKeyId':self.kms,'Metadata':request['Metadata']}
        self.versions[version]={'head':head,'raw':raw};self.latest=version
        if self.ambiguous_checkpoint:
            self.ambiguous_checkpoint=False;raise ConnectionError('Synthetic lost checkpoint response')
        return {} if self.missing_checkpoint_version else {'VersionId':version,'ETag':etag}
    def batch_get_builds(self,ids):return {'builds':[self.builds[key] for key in ids if key in self.builds]}
    def terminal(self,number=1,status='FAILED'):
        arn=build(number);ident=entry.build_id(arn)
        self.builds[ident]={'arn':arn,'id':ident,'projectName':acquisition.BUILD_PROJECT,
                            'serviceRole':acquisition.BUILD_ROLE,'buildStatus':status,'buildComplete':status!='IN_PROGRESS'}


@pytest.fixture
def source(monkeypatch):
    raw=b'x'*563;digest=hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(acquisition,'PART_BYTES',8)
    monkeypatch.setattr(acquisition,'ARCHIVE_BYTES',len(raw));monkeypatch.setattr(acquisition,'ARCHIVE_SHA256',digest)
    owner=acquisition.migration.APPROVED_OWNER
    plan={'plan_id':acquisition.migration.APPROVED_PLAN,'owner_id':owner,'bucket':acquisition.migration.APPROVED_BUCKET,
          'kms':'arn:synthetic:kms'}
    obj={'sha256':digest,'bytes':len(raw),'name':acquisition.ARCHIVE_NAME,'dataset_id':'dataset-original',
         'key':f'tenants/{owner}/datasets/dataset-original/{acquisition.ARCHIVE_NAME}'}
    key=f"operations/checkpoints/migration/{plan['plan_id']}/exact-vep-worker/{digest}.json"
    return plan,obj,raw,AWS(key,plan['kms'])


def wrapped(monkeypatch,aws,number=1):
    original=acquisition.checkpoint
    monkeypatch.setattr(acquisition,'checkpoint',entry.wrapped_checkpoint(original,lambda:aws,{'value':verified(number)}))


def snapshots(aws):
    return [json.loads(value['raw']) for value in aws.versions.values()]


def test_actual_acquisition_persists_each_critical_state_and_every_32_parts(source,tmp_path,monkeypatch):
    plan,obj,raw,aws=source;wrapped(monkeypatch,aws)
    result=acquisition.acquire_archive(aws,plan,obj,MemoryProvider(raw),tmp_path,verified()['reservation'])
    values=snapshots(aws)
    assert result['sha256']==obj['sha256']
    assert [value['state'] for value in values][:3]==['pending','creating','uploading']
    assert [len(value['parts']) for value in values if value['state']=='uploading']==[0,32,64]
    assert [value['state'] for value in values][-2:]==['completing','completed']
    assert len({value['upload_id'] for value in values if value.get('upload_id')})==1
    assert values[-1]['verified_original_sha256']==obj['sha256']
    assert all(value['durable_worker']['build_arn']==build(1) for value in values)
    assert all(request['Key']==aws.key for request in aws.checkpoint_puts)
    assert aws.checkpoint_puts[0]['IfNoneMatch']=='*'
    assert all('IfMatch' in request for request in aws.checkpoint_puts[1:])
    assert all(request.get('VersionId') for request in aws.reads)


def test_new_terminal_successor_restores_private_upload_and_rehashes_without_reuploading_prefix(source,tmp_path,monkeypatch):
    plan,obj,raw,aws=source;original=acquisition.checkpoint
    monkeypatch.setattr(acquisition,'checkpoint',entry.wrapped_checkpoint(original,lambda:aws,{'value':verified(1)}))
    aws.fail_part=35
    with pytest.raises(ConnectionError):
        acquisition.acquire_archive(aws,plan,obj,MemoryProvider(raw),tmp_path/'first-build',verified()['reservation'])
    before=aws.part_calls.count(1);assert before==1
    failed=snapshots(aws)[-1]
    assert failed['state']=='uploading' and failed['durable_failure']['exception']=='ConnectionError'
    assert len(failed['parts'])==34 and failed['upload_id']=='private-upload-1'
    aws.terminal(1)
    successor=verified(2);successor.update(receipt_sha256='c'*64,receipt_version_id='new-source-new-admission')
    monkeypatch.setattr(acquisition,'checkpoint',entry.wrapped_checkpoint(original,lambda:aws,{'value':successor}))
    provider=MemoryProvider(raw)
    acquisition.acquire_archive(aws,plan,obj,provider,tmp_path/'second-ephemeral-build',verified()['reservation'])
    assert aws.create_calls==1 and aws.part_calls.count(1)==before
    assert sorted(provider.ranges)[0]==(0,8) and len(provider.ranges)==71
    assert snapshots(aws)[-1]['durable_worker']['build_arn']==build(2)
    assert snapshots(aws)[-1]['durable_worker']['receipt_sha256']=='c'*64
    assert snapshots(aws)[-1]['durable_worker']['receipt_version_id']=='new-source-new-admission'
    assert len({value['upload_id'] for value in snapshots(aws) if value.get('upload_id')})==1


def test_another_active_or_unknown_build_cannot_restore_or_mutate_existing_checkpoint(source,tmp_path,monkeypatch):
    plan,obj,_,aws=source;original=acquisition.checkpoint
    first=entry.wrapped_checkpoint(original,lambda:aws,{'value':verified(1)})
    with first(tmp_path/'first',plan,obj,verified()['reservation']):pass
    before=len(aws.checkpoint_puts)
    second=entry.wrapped_checkpoint(original,lambda:aws,{'value':verified(2)})
    for known in [False,True]:
        if known:aws.terminal(1,'IN_PROGRESS')
        with pytest.raises(ValueError,match='still owns'):
            with second(tmp_path/'second',plan,obj,verified()['reservation']):pytest.fail('Active owner must block yield')
    assert len(aws.checkpoint_puts)==before and aws.create_calls==0


def test_checkpoint_requires_admission_before_even_reading_s3(source,tmp_path):
    plan,obj,_,aws=source
    def no_session():pytest.fail('Preflight must precede checkpoint access')
    wrapper=entry.wrapped_checkpoint(acquisition.checkpoint,no_session,{})
    with pytest.raises(ValueError,match='admission must pass'):
        with wrapper(tmp_path,plan,obj,verified()['reservation']):pass
    assert not aws.checkpoint_puts and not aws.reads


def test_cas_claim_race_stops_before_multipart_mutation_and_never_retries_put(source,tmp_path):
    plan,obj,_,aws=source
    wrapper=entry.wrapped_checkpoint(acquisition.checkpoint,lambda:aws,{'value':verified()})
    def race(provider,request):
        provider.before_checkpoint_put=None
        provider.latest='foreign-version';provider.versions[provider.latest]={'head':{'ETag':'foreign'}}
    aws.before_checkpoint_put=race
    with pytest.raises(Exception):
        with wrapper(tmp_path,plan,obj,verified()['reservation']):pytest.fail('CAS loser must not yield')
    assert len(aws.checkpoint_puts)==1 and aws.create_calls==0


@pytest.mark.parametrize('failure',['ambiguous','missing-version'])
def test_unknown_checkpoint_put_outcome_has_no_automatic_retry(source,tmp_path,failure):
    plan,obj,_,aws=source
    aws.ambiguous_checkpoint=failure=='ambiguous';aws.missing_checkpoint_version=failure=='missing-version'
    wrapper=entry.wrapped_checkpoint(acquisition.checkpoint,lambda:aws,{'value':verified()})
    with pytest.raises(Exception):
        with wrapper(tmp_path,plan,obj,verified()['reservation']):pytest.fail('Unconfirmed claim must not yield')
    assert len(aws.checkpoint_puts)==1 and aws.create_calls==0


def test_checksum_failure_is_durable_and_never_commits_archive_or_normal_receipt(source,tmp_path,monkeypatch):
    plan,obj,raw,aws=source;wrapped(monkeypatch,aws)
    with pytest.raises(ValueError,match='original inventoried SHA'):
        acquisition.acquire_archive(aws,plan,obj,MemoryProvider(b'y'*len(raw)),tmp_path,verified()['reservation'])
    final=snapshots(aws)[-1]
    assert final['state']=='failed-sha256' and final['durable_failure']['exception']=='ValueError'
    assert aws.complete_calls==0 and aws.puts==[] and obj['key'] not in aws.objects


@pytest.mark.parametrize('kind',['version','checksum','kms','owner','body','binding'])
def test_restore_rejects_changed_immutable_identity_or_original_binding(source,tmp_path,kind):
    plan,obj,_,aws=source
    wrapper=entry.wrapped_checkpoint(acquisition.checkpoint,lambda:aws,{'value':verified()})
    with wrapper(tmp_path/'first',plan,obj,verified()['reservation']):pass
    stored=aws.versions[aws.latest]
    if kind=='version':stored['head']['VersionId']='null'
    if kind=='checksum':stored['head']['ChecksumSHA256']='wrong'
    if kind=='kms':stored['head']['SSEKMSKeyId']='other'
    if kind=='owner':stored['head']['Metadata']['owner-build-arn']='unreviewed-owner'
    if kind=='body':stored['raw']=b'x'*len(stored['raw'])
    if kind=='binding':
        value=json.loads(stored['raw']);value['owner_id']='foreign-owner';stored['raw']=acquisition.migration.encode(value)
        digest=hashlib.sha256(stored['raw']).hexdigest()
        stored['head'].update(ContentLength=len(stored['raw']),ChecksumSHA256=base64.b64encode(bytes.fromhex(digest)).decode())
        stored['head']['Metadata']['sha256']=digest
    before=len(aws.checkpoint_puts)
    with pytest.raises(ValueError):
        with wrapper(tmp_path/'second',plan,obj,verified()['reservation']):pytest.fail('Invalid checkpoint must not yield')
    assert len(aws.checkpoint_puts)==before


def test_entry_wrapper_preserves_original_admission_order_and_restores_globals(monkeypatch):
    calls=[];original_checkpoint=acquisition.checkpoint
    original_session=lambda:object()
    monkeypatch.setattr(acquisition,'aws_session',original_session)
    def verify(*args,**kwargs):calls.append('admission');return verified()
    def main():
        assert acquisition.checkpoint is not original_checkpoint
        assert isinstance(acquisition.aws_session(),entry.ScopedSession)
        assert acquisition.verify_codebuild_admission() == verified()
        calls.append('main-after-admission')
    monkeypatch.setattr(acquisition,'verify_codebuild_admission',verify)
    monkeypatch.setattr(acquisition,'main',main)
    entry.main()
    assert calls==['admission','main-after-admission']
    assert acquisition.verify_codebuild_admission is verify and acquisition.checkpoint is original_checkpoint
    assert acquisition.aws_session is original_session


class DeniedHead:
    def __init__(self,rows=None,denied_listing=False):
        self.rows=[] if rows is None else rows;self.denied_listing=denied_listing;self.list_requests=[]
    def head_object(self,**request):raise error('AccessDenied','HeadObject')
    def list_objects_v2(self,**request):
        self.list_requests.append(request)
        if self.denied_listing:raise error('AccessDenied','ListObjectsV2')
        return {'Contents':self.rows}
    def get_object(self,**request):raise error('AccessDenied','GetObject')


@pytest.mark.parametrize('known_key',sorted(entry.absence_keys()))
@pytest.mark.parametrize('suffix',[None,'.other'])
def test_only_explicit_exact_prefix_listing_can_translate_denied_current_head_to_absence(known_key,suffix):
    rows=[] if suffix is None else [{'Key':known_key+suffix}]
    client=DeniedHead(rows)
    with pytest.raises(Exception) as caught:
        entry.ExactKeyS3(client).head_object(Bucket=acquisition.migration.APPROVED_BUCKET,Key=known_key)
    assert caught.value.response['Error']['Code']=='404'
    assert client.list_requests==[{'Bucket':acquisition.migration.APPROVED_BUCKET,'Prefix':known_key,'MaxKeys':1}]


def test_existing_key_or_denied_listing_preserves_head_denial():
    key=sorted(entry.absence_keys())[0]
    for client in [DeniedHead([{'Key':key}]),DeniedHead(denied_listing=True)]:
        with pytest.raises(Exception) as caught:
            entry.ExactKeyS3(client).head_object(Bucket=acquisition.migration.APPROVED_BUCKET,Key=key)
        assert caught.value.response['Error']['Code']=='AccessDenied'
        assert len(client.list_requests)==1


@pytest.mark.parametrize('head_request',[
    {'Bucket':'other-bucket','Key':sorted(entry.absence_keys())[0]},
    {'Bucket':acquisition.migration.APPROVED_BUCKET,'Key':'operations/private-unrelated-key'},
    {'Bucket':acquisition.migration.APPROVED_BUCKET,'Key':sorted(entry.absence_keys())[0],'VersionId':'exact-older-version'}])
def test_unknown_scope_and_versioned_read_denials_are_never_treated_as_missing(head_request):
    client=DeniedHead()
    with pytest.raises(Exception) as caught:entry.ExactKeyS3(client).head_object(**head_request)
    assert caught.value.response['Error']['Code']=='AccessDenied' and client.list_requests==[]


def test_denied_actual_object_read_never_uses_absence_fallback():
    client=DeniedHead()
    with pytest.raises(Exception) as caught:
        entry.ExactKeyS3(client).get_object(Bucket=acquisition.migration.APPROVED_BUCKET,Key=sorted(entry.absence_keys())[0])
    assert caught.value.response['Error']['Code']=='AccessDenied' and client.list_requests==[]
