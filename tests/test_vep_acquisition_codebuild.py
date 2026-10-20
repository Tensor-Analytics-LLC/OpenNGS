"""Synthetic CodeBuild/admission contracts; no external services are contacted."""
import base64
import copy
import hashlib
import io
import json
from pathlib import Path

import pytest

from scripts import acquire_exact_vep_archive as acquisition


@pytest.fixture
def authorized(monkeypatch):
    owner=acquisition.migration.APPROVED_OWNER;digest=acquisition.ARCHIVE_SHA256
    dataset='ds_migration_'+hashlib.sha256((owner+':'+digest).encode()).hexdigest()[:40]
    obj={'name':acquisition.ARCHIVE_NAME,'sha256':digest,'bytes':acquisition.ARCHIVE_BYTES,'dataset_id':dataset,
         'key':f'tenants/{owner}/datasets/{dataset}/{acquisition.ARCHIVE_NAME}',
         'aliases':[{'name':acquisition.ARCHIVE_NAME,'user_id':owner,'bytes':acquisition.ARCHIVE_BYTES}]}
    plan={'plan_id':acquisition.migration.APPROVED_PLAN,'owner_id':owner,'bucket':acquisition.migration.APPROVED_BUCKET,
          'kms':'arn:aws:kms:us-east-1:509505638328:key/synthetic','objects':[obj],'total_bytes':obj['bytes']}
    reservation='cost_'+'a'*32
    monkeypatch.setattr(acquisition,'verify_cloud_runtime',lambda release:{'instance_id':acquisition.ISSUER_HOST,
        'account':acquisition.BUILD_ACCOUNT,'region':acquisition.BUILD_REGION})
    monkeypatch.setattr(acquisition,'verify_admission',lambda reservation,plan:{'reservation':reservation,
        'maximum_reserved_pretax_usd':2.0,'existing_storage_envelope':True})
    buildspec='{"version":"0.2","phases":{"build":{"commands":["synthetic command"]}}}'
    source={'bucket':plan['bucket'],'key':'operations/builds/exact-vep-acquisition/source.zip',
            'version_id':'immutable-sourcezip','bytes':1000,'sha256':'b'*64,
            'entrypoint_sha256':hashlib.sha256(Path(acquisition.__file__).read_bytes()).hexdigest(),
            'buildspec_sha256':hashlib.sha256(buildspec.encode()).hexdigest()}
    receipt=acquisition.make_codebuild_admission(plan,obj,reservation,source)
    build_id=acquisition.BUILD_PROJECT+':12345678-1234-1234-1234-123456789abc'
    build_arn=f'arn:aws:codebuild:{acquisition.BUILD_REGION}:{acquisition.BUILD_ACCOUNT}:build/'+build_id
    build={'arn':build_arn,'id':build_id,'projectName':acquisition.BUILD_PROJECT,'serviceRole':acquisition.BUILD_ROLE,
           'buildStatus':'IN_PROGRESS','timeoutInMinutes':45,
           'environment':{'computeType':'BUILD_GENERAL1_MEDIUM','privilegedMode':False,'type':'LINUX_CONTAINER',
                          'image':'aws/codebuild/standard:7.0'},
           'source':{'type':'S3','location':source['bucket']+'/'+source['key'],'buildspec':buildspec,'insecureSsl':False},
           'sourceVersion':source['version_id']}
    return {'plan':plan,'obj':obj,'receipt':receipt,'build':build,'source':source,
            'env':{'CODEBUILD_BUILD_ID':build_id,'CODEBUILD_BUILD_ARN':build_arn,'AWS_REGION':acquisition.BUILD_REGION}}


class Provider:
    def __init__(self,fixture):
        self.fixture=fixture;self.head_changes={};self.receipt_changes={};self.calls=[]
        self.identity={'Account':acquisition.BUILD_ACCOUNT,
            'Arn':f'arn:aws:sts::{acquisition.BUILD_ACCOUNT}:assumed-role/'+acquisition.BUILD_ROLE.rsplit('/',1)[1]+'/CodeBuild-session'}
    def client(self,name):self.calls.append(name);return self
    def get_caller_identity(self):return self.identity
    def batch_get_builds(self,ids):return {'builds':[self.fixture['build']]}
    def get_object(self,**request):
        return {'VersionId':'immutable-admission','ServerSideEncryption':'aws:kms','SSEKMSKeyId':self.fixture['plan']['kms'],
                'Body':io.BytesIO(acquisition.migration.encode(self.fixture['receipt'])),**self.receipt_changes}
    def head_object(self,**request):
        source=self.fixture['source']
        return {'VersionId':source['version_id'],'ContentLength':source['bytes'],'ChecksumType':'FULL_OBJECT',
                'ChecksumSHA256':base64.b64encode(bytes.fromhex(source['sha256'])).decode(),
                'ServerSideEncryption':'aws:kms','SSEKMSKeyId':self.fixture['plan']['kms'],**self.head_changes}


def verify(fixture,provider=None,**overrides):
    digest=hashlib.sha256(acquisition.migration.encode(fixture['receipt'])).hexdigest()
    args={'uri':'s3://'+fixture['plan']['bucket']+'/operations/checkpoints/migration/'+acquisition.migration.APPROVED_PLAN+
          '/admissions/exact-vep/'+digest+'.json','version':'immutable-admission','digest':digest,
          'environment':fixture['env'],'now':fixture['receipt']['issued_at']+1,**overrides}
    return acquisition.verify_codebuild_admission(provider or Provider(fixture),fixture['plan'],fixture['obj'],**args)


def test_actual_app_issued_receipt_binds_exact_plan_source_reservation_and_current_cloud_build(authorized):
    result=verify(authorized)
    assert result['reservation']==authorized['receipt']['reservation']
    assert result['build_arn']==authorized['build']['arn']
    assert result['expires_at']-authorized['receipt']['issued_at']==3600
    assert authorized['receipt']['maximum_seconds']==2700
    assert authorized['receipt']['maximum_cost_pretax_usd']==.70


@pytest.mark.parametrize('field,value',[
    ('CODEBUILD_BUILD_ARN','arn:aws:codebuild:us-west-2:509505638328:build/openngs-demo-build:12345678-1234-1234-1234-123456789abc'),
    ('CODEBUILD_BUILD_ID','other-project:12345678-1234-1234-1234-123456789abc'),('AWS_REGION','us-west-2')])
def test_environment_spoofing_cannot_authorize_aws_execution(authorized,field,value):
    authorized['env'][field]=value
    with pytest.raises(ValueError,match='project, build ARN and region'):
        verify(authorized)


@pytest.mark.parametrize('identity',[
    {'Account':'000000000000','Arn':'arn:aws:sts::000000000000:assumed-role/other/session'},
    {'Account':acquisition.BUILD_ACCOUNT,'Arn':f'arn:aws:sts::{acquisition.BUILD_ACCOUNT}:assumed-role/other/session'},
    {'Account':acquisition.BUILD_ACCOUNT,'Arn':acquisition.BUILD_ROLE}])
def test_sts_requires_exact_reviewed_assumed_service_role(authorized,identity):
    provider=Provider(authorized);provider.identity=identity
    with pytest.raises(ValueError,match='service role'):
        verify(authorized,provider)
    assert provider.calls==['sts']


@pytest.mark.parametrize('section,key,value',[
    (None,'serviceRole','arn:aws:iam::509505638328:role/other'),(None,'timeoutInMinutes',46),
    (None,'buildStatus','SUCCEEDED'),(None,'projectName','other'),(None,'vpcConfig',{'vpcId':'new-vpc'}),
    ('environment','computeType','BUILD_GENERAL1_LARGE'),('environment','privilegedMode',True),
    ('environment','image','unreviewed/image'),('environment','type','LINUX_GPU_CONTAINER')])
def test_provider_build_record_enforces_identity_and_cost_isolation_bounds(authorized,section,key,value):
    (authorized['build'][section] if section else authorized['build'])[key]=value
    with pytest.raises(ValueError,match='45-minute MEDIUM'):
        verify(authorized)


@pytest.mark.parametrize('key,value',[
    ('owner_id','u_'+'f'*32),('plan_sha256','0'*64),('plan_version_id','other'),
    ('maximum_cost_pretax_usd',2.01),('maximum_seconds',3600),('project_arn','other'),
    ('issuer',{'instance_id':'another-host','account':acquisition.BUILD_ACCOUNT,'region':acquisition.BUILD_REGION})])
def test_receipt_cannot_relax_original_owner_plan_source_or_cost(authorized,key,value):
    authorized['receipt'][key]=value
    with pytest.raises(ValueError):verify(authorized)


def test_expired_or_overlong_admission_is_rejected(authorized):
    with pytest.raises(ValueError,match='expired'):
        verify(authorized,now=authorized['receipt']['expires_at'])
    authorized['receipt']['expires_at']+=1
    with pytest.raises(ValueError,match='expired'):
        verify(authorized)


@pytest.mark.parametrize('section,key,value',[
    (None,'sourceVersion','unversioned'),('source','location','other-bucket/source.zip'),
    ('source','type','GITHUB'),('source','insecureSsl',True),('source','buildspec','different command')])
def test_each_codebuild_source_input_is_bound_to_admission(authorized,section,key,value):
    (authorized['build'][section] if section else authorized['build'])[key]=value
    with pytest.raises(ValueError,match='source version'):
        verify(authorized)


@pytest.mark.parametrize('changes',[
    {'ChecksumType':'COMPOSITE'},{'ChecksumSHA256':'wrong'},{'ContentLength':1001},
    {'VersionId':'mutable'},{'ServerSideEncryption':'AES256'},{'SSEKMSKeyId':'wrong-key'}])
def test_source_zip_requires_s3_full_object_sha_and_exact_version_kms(authorized,changes):
    provider=Provider(authorized);provider.head_changes=changes
    with pytest.raises(ValueError,match='source archive'):
        verify(authorized,provider)


def test_admission_receipt_requires_exact_content_addressed_version_and_private_encryption(authorized):
    for overrides in [{'uri':'s3://elsewhere/receipt.json'},{'version':'null'},{'digest':'0'*64}]:
        with pytest.raises(ValueError):verify(authorized,**overrides)
    provider=Provider(authorized);provider.receipt_changes={'VersionId':'other'}
    with pytest.raises(ValueError,match='receipt must retain'):
        verify(authorized,provider)


def test_app_issuer_cannot_attest_other_host_or_insufficient_shared_hold(authorized,monkeypatch):
    monkeypatch.setattr(acquisition,'verify_cloud_runtime',lambda release:{'instance_id':'other','account':acquisition.BUILD_ACCOUNT,'region':acquisition.BUILD_REGION})
    with pytest.raises(ValueError,match='application host'):
        acquisition.make_codebuild_admission(authorized['plan'],authorized['obj'],authorized['receipt']['reservation'],authorized['source'])
    monkeypatch.setattr(acquisition,'verify_cloud_runtime',lambda release:authorized['receipt']['issuer'])
    monkeypatch.setattr(acquisition,'verify_admission',lambda *args:{'maximum_reserved_pretax_usd':.1})
    with pytest.raises(ValueError,match='cannot cover'):
        acquisition.make_codebuild_admission(authorized['plan'],authorized['obj'],authorized['receipt']['reservation'],authorized['source'])
