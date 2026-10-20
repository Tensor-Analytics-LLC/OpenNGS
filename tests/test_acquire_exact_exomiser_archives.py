"""Exact original-byte, funding and concurrency proofs using synthetic services."""
import base64
from collections import Counter
import copy
from datetime import datetime,timezone
import gc
import hashlib
import io
import json
from pathlib import Path
import threading
import time
import subprocess
import sys
from zipfile import ZipFile

import pytest

from scripts import acquire_exact_exomiser_archives as acquisition
from scripts import codebuild_exact_exomiser_entry as entry
from test_acquire_exact_vep_archive import MemoryProvider,MemoryS3,HTTPResponse,existing
from test_codebuild_exact_vep_entry import AWS,build
from test_prioritize_migration_objects import error

RESERVATION='cost_'+'a'*32


@pytest.fixture(params=['2410_hg38.zip','2410_phenotype.zip'])
def source(request,monkeypatch):
    raw=bytes(range(251))*3;name=request.param;digest=hashlib.sha256(raw).hexdigest()
    monkeypatch.setitem(acquisition.ARCHIVES,name,{'bytes':len(raw),'sha256':digest})
    monkeypatch.setattr(acquisition,'PART_BYTES',8)
    owner=acquisition.migration.APPROVED_OWNER;dataset='ds_migration_'+hashlib.sha256((owner+':'+digest).encode()).hexdigest()[:40]
    obj={'name':name,'sha256':digest,'bytes':len(raw),'dataset_id':dataset,
         'key':f'tenants/{owner}/datasets/{dataset}/{name}','aliases':[{'name':name,'user_id':owner,'bytes':len(raw)}]}
    plan={'plan_id':acquisition.migration.APPROVED_PLAN,'owner_id':owner,'bucket':acquisition.migration.APPROVED_BUCKET,
          'kms':'arn:synthetic:kms','objects':[obj],'total_bytes':len(raw)}
    return plan,obj,raw


def saved(directory,obj):return json.loads((directory/('exact-exomiser-'+obj['sha256']+'.json')).read_bytes())


def test_exact_two_official_original_identities_only():
    assert acquisition.ARCHIVES=={
        '2410_hg38.zip':{'bytes':23510939317,'sha256':'606030ad8d9f7038444d3cf58482c59ca20a8ec2deb4d1dd64349b98c767b325'},
        '2410_phenotype.zip':{'bytes':8166015076,'sha256':'057021dfdb18f7a9dd7e5de3023c9e42f738a64d8f8c41ae6728edc595d726ec'}}
    assert acquisition.PART_BYTES==8*1024**2 and acquisition.PREFETCH_PARTS==8 and acquisition.UPLOAD_PARTS==2
    assert acquisition.MAX_SECONDS==2700 and acquisition.CODEBUILD_MAX_COST==.8


@pytest.mark.parametrize('field,value',[('bytes',1),('sha256','0'*64),('dataset_id','other'),('key','other'),('cloud_source',True)])
def test_original_target_binding_rejects_any_substitution(source,field,value):
    plan,obj,raw=source;assert acquisition.target_object(plan,obj['name'])==obj
    obj[field]=value
    with pytest.raises(ValueError):acquisition.target_object(plan,obj['name'])


def test_public_head_and_each_range_require_exact_size_range_and_identity_encoding(source):
    plan,obj,raw=source;provider=acquisition.Provider(obj);requests=[]
    class Opener:
        def open(self,request,timeout):
            requests.append(request)
            headers={'Content-Length':str(len(raw)),'Accept-Ranges':'bytes'}
            return HTTPResponse(headers,b'',status=200) if request.method=='HEAD' else HTTPResponse(
                {'Content-Length':'8','Content-Range':f'bytes 0-7/{len(raw)}'},raw[:8])
    provider.opener=Opener();assert provider.head()['bytes']==len(raw) and provider.read_range(0,8)==raw[:8]
    for request in requests:
        headers={k.lower():v for k,v in request.headers.items()}
        assert 'if-match' not in headers and 'if-unmodified-since' not in headers
        assert headers['accept-encoding']=='identity'
    assert requests[1].get_header('Range')=='bytes=0-7'
    assert 'etag' not in acquisition.profile(obj) and 'last_modified' not in acquisition.profile(obj)


@pytest.mark.parametrize('changes,body',[
    ({'Content-Range':'bytes 1-8/753'},b'12345678'),({'Content-Range':'bytes 0-7/754'},b'12345678'),
    ({'Content-Length':'9'},b'12345678'),({'Content-Encoding':'gzip'},b'12345678'),({},b'short'),({},b'too-long!')])
def test_missing_or_wrong_source_validators_never_become_valid_ranges(source,changes,body):
    plan,obj,raw=source;provider=acquisition.Provider(obj)
    headers={'Content-Length':'8','Content-Range':f'bytes 0-7/{len(raw)}',**changes}
    provider._open=lambda *args:HTTPResponse(headers,body)
    with pytest.raises(ValueError):provider.read_range(0,8)


def test_no_redirect_or_non_partial_response_is_accepted(source):
    plan,obj,raw=source;provider=acquisition.Provider(obj)
    provider._open=lambda *args:HTTPResponse({'Content-Length':'8','Content-Range':f'bytes 0-7/{len(raw)}'},raw[:8],status=200)
    with pytest.raises(ValueError):provider.read_range(0,8)
    with pytest.raises(ValueError):acquisition.NoRedirect().redirect_request(None,None,302,'',{},'https://other.test/')


def test_real_whole_hash_precedes_conditional_commit_and_safe_progress_covers_rehashed_prefix(source,tmp_path,monkeypatch):
    plan,obj,raw=source;client=MemoryS3();provider=MemoryProvider(raw);events=[]
    monkeypatch.setattr(acquisition,'PROGRESS_INTERVAL',0)
    def before_complete(request):
        value=saved(tmp_path,obj)
        assert value['verified_original_sha256']==hashlib.sha256(raw).hexdigest()
        assert value['provider_bytes']==len(raw) and request['IfNoneMatch']=='*'
        assert [p['PartNumber'] for p in request['MultipartUpload']['Parts']]==list(range(1,96))
    client.complete_hook=before_complete
    acquisition.acquire_archive(client,plan,obj,provider,tmp_path,RESERVATION,progress=events.append)
    assert client.bodies[obj['key']]==raw and client.create_calls==client.complete_calls==1
    assert events[-1]['source_bytes_rehashed']==len(raw) and events[-1]['new_uploaded_bytes']==len(raw)
    assert all(set(event)=={'stage','source_bytes_rehashed','source_total_bytes','confirmed_parts','new_uploaded_parts','new_uploaded_bytes','elapsed_seconds'} for event in events)
    assert all('https:' not in json.dumps(event) and 's3:' not in json.dumps(event) and 'upload' not in str(event.get('stage')) for event in events)


def test_full_sha_mismatch_never_commits_even_without_etag(source,tmp_path):
    plan,obj,raw=source;client=MemoryS3()
    with pytest.raises(ValueError,match='inventoried SHA-256'):
        acquisition.acquire_archive(client,plan,obj,MemoryProvider(b'X'+raw[1:]),tmp_path,RESERVATION)
    assert saved(tmp_path,obj)['state']=='failed-sha256' and client.complete_calls==0 and not client.puts


def test_resume_rereads_all_bytes_and_reuses_only_identical_private_parts(source,tmp_path,monkeypatch):
    plan,obj,raw=source;client=MemoryS3();client.fail_part=35
    with pytest.raises(ConnectionError):acquisition.acquire_archive(client,plan,obj,MemoryProvider(raw),tmp_path,RESERVATION)
    upload=saved(tmp_path,obj)['upload_id'];known=set(client.uploads[upload]['parts']);before=Counter(client.part_calls);events=[]
    monkeypatch.setattr(acquisition,'PROGRESS_INTERVAL',0);provider=MemoryProvider(raw)
    acquisition.acquire_archive(client,plan,obj,provider,tmp_path,RESERVATION,progress=events.append)
    assert all(Counter(client.part_calls)[number]==before[number] for number in known)
    assert sorted(provider.ranges)==[(offset,min(8,len(raw)-offset)) for offset in range(0,len(raw),8)]
    assert events and any(e['confirmed_parts']>0 and e['new_uploaded_bytes']==0 for e in events)
    assert client.create_calls==1 and client.completions[0]['UploadId']==upload and client.bodies[obj['key']]==raw


@pytest.mark.parametrize('composite',[False,True])
def test_existing_immutable_object_wins_without_source_or_upload(source,tmp_path,composite):
    plan,obj,raw=source;client=MemoryS3();existing(client,plan,obj,raw,composite=composite);before=copy.deepcopy(client.objects[obj['key']])
    provider=MemoryProvider(b'');result=acquisition.acquire_archive(client,plan,obj,provider,tmp_path,RESERVATION)
    assert result['version_id']=='original-version' and client.objects[obj['key']]==before
    assert provider.head_calls==0 and not provider.ranges and client.create_calls==client.complete_calls==0


def test_unknown_create_and_complete_never_repeat(source,tmp_path):
    plan,obj,raw=source;client=MemoryS3();client.ambiguous_create=True
    with pytest.raises(ConnectionError):acquisition.acquire_archive(client,plan,obj,MemoryProvider(raw),tmp_path,RESERVATION)
    with pytest.raises(ValueError,match='operator reconciliation'):acquisition.acquire_archive(client,plan,obj,MemoryProvider(raw),tmp_path,RESERVATION)
    assert client.create_calls==1 and client.complete_calls==0
    second=MemoryS3();second.ambiguous_complete=True;folder=tmp_path/'second'
    with pytest.raises(ConnectionError):acquisition.acquire_archive(second,plan,obj,MemoryProvider(raw),folder,RESERVATION)
    result=acquisition.acquire_archive(second,plan,obj,MemoryProvider(b''),folder,RESERVATION)
    assert result['provider_bytes']==0 and second.create_calls==second.complete_calls==1


def test_shared_eight_buffer_window_counts_retained_executor_results_and_upload_callables(source,tmp_path,monkeypatch):
    plan,obj,raw=source;counts={'live':0,'peak':0};mutex=threading.Lock();returned=[];tasks=[]
    original=acquisition.concurrent.futures.ThreadPoolExecutor
    class Retaining(original):
        def submit(self,function,*args,**kwargs):
            if getattr(function,'__name__','')=='read':
                def retain():
                    value=function(*args,**kwargs);returned.append(value);return value
                return super().submit(retain)
            tasks.append(function);return super().submit(function,*args,**kwargs)
    class Tracked(bytes):
        def __new__(cls,value):
            result=super().__new__(cls,value)
            with mutex:counts['live']+=1;counts['peak']=max(counts['peak'],counts['live'])
            return result
        def __del__(self):
            with mutex:counts['live']-=1
    class Provider(MemoryProvider):
        def read_range(self,offset,length):return Tracked(self.raw[offset:offset+length])
    class Client(MemoryS3):
        def upload_part(self,**request):
            time.sleep(.001);request={**request,'Body':memoryview(request['Body']).tobytes()}
            return super().upload_part(**request)
    monkeypatch.setattr(acquisition.concurrent.futures,'ThreadPoolExecutor',Retaining)
    acquisition.acquire_archive(Client(),plan,obj,Provider(raw),tmp_path,RESERVATION);gc.collect()
    assert counts['peak']<=8 and counts['live']==0
    assert all(r.raw is None for r in returned) and all(task.raw is None for task in tasks)


def funded_details(obj,source):
    return {'purpose':acquisition.PURPOSE,'archive_sha256':obj['sha256'],'plan_sha256':acquisition.migration.APPROVED_PLAN_SHA256,
        'source_zip_sha256':source['sha256'],'maximum_seconds':2700,'maximum_attempts':1}


@pytest.mark.parametrize('change',[
    {'user_id':'other'},{'charged_micro':1},{'reserved_micro':799999},{'state':'reconciled'},
    {'account':'research-pro'},{'role':'user'},{'period':'2000-01'},{'frozen':1},{'details':'{}'}])
def test_only_fresh_exact_owner_target_source_shared_hold_can_issue(source,monkeypatch,change):
    from backend import budget,db
    plan,obj,raw=source;package={'sha256':'b'*64}
    row={'user_id':plan['owner_id'],'state':'reserved','reserved_micro':800000,'charged_micro':0,'period':budget.period(),
         'details':json.dumps(funded_details(obj,package)),'account':'shared-platform','role':'admin','frozen':0}
    storage={'user_id':plan['owner_id'],'plan_sha256':acquisition.migration.APPROVED_PLAN_SHA256,'bytes':plan['total_bytes']}
    monkeypatch.setattr(db,'query',lambda sql,*args,**kwargs:storage if 'baseline_storage_allocations' in sql else row)
    assert acquisition.verify_admission(RESERVATION,plan,obj,package)['uncharged_pretax_usd']==.8
    row.update(change)
    with pytest.raises(ValueError):acquisition.verify_admission(RESERVATION,plan,obj,package)


def authorized(source,monkeypatch):
    plan,obj,raw=source;buildspec='exact reviewed wrapper --wall-seconds 2700'
    package={'bucket':plan['bucket'],'key':'operations/builds/exact-exomiser/source.zip','version_id':'source-version','bytes':100,
             'sha256':'b'*64,'entrypoint_sha256':hashlib.sha256(Path(acquisition.__file__).read_bytes()).hexdigest(),
             'buildspec_sha256':hashlib.sha256(buildspec.encode()).hexdigest()}
    monkeypatch.setattr(acquisition,'verify_cloud_runtime',lambda release:{'instance_id':acquisition.ISSUER_HOST,'account':acquisition.BUILD_ACCOUNT,'region':acquisition.BUILD_REGION})
    monkeypatch.setattr(acquisition,'verify_admission',lambda reservation,plan,obj,source:{'reservation':reservation,
        'period':datetime.fromtimestamp(time.time(),timezone.utc).strftime('%Y-%m'),
        'maximum_reserved_pretax_usd':.8,'uncharged_pretax_usd':.8,'existing_storage_envelope':True,**funded_details(obj,source)})
    receipt=acquisition.make_codebuild_admission(plan,obj,RESERVATION,package);now=receipt['issued_at']
    record={'arn':build(1),'id':entry.build_id(build(1)),'projectName':acquisition.BUILD_PROJECT,'serviceRole':acquisition.BUILD_ROLE,
        'buildStatus':'IN_PROGRESS','timeoutInMinutes':45,'startTime':datetime.fromtimestamp(now-30,timezone.utc),
        'environment':{'computeType':'BUILD_GENERAL1_MEDIUM','privilegedMode':False,'type':'LINUX_CONTAINER','image':'aws/codebuild/standard:7.0'},
        'source':{'type':'S3','location':package['bucket']+'/'+package['key'],'buildspec':buildspec},'sourceVersion':package['version_id']}
    class Services:
        def client(self,name,**kwargs):return self
        def get_caller_identity(self):return {'Account':acquisition.BUILD_ACCOUNT,'Arn':f'arn:aws:sts::{acquisition.BUILD_ACCOUNT}:assumed-role/'+acquisition.BUILD_ROLE.rsplit('/',1)[1]+'/session'}
        def batch_get_builds(self,ids):return {'builds':[record]}
        def get_bucket_lifecycle_configuration(self,**kwargs):return {'Rules':[{'Status':'Enabled','AbortIncompleteMultipartUpload':{'DaysAfterInitiation':1}}]}
        def get_object(self,**kwargs):return {'VersionId':'receipt-version','ServerSideEncryption':'aws:kms','SSEKMSKeyId':plan['kms'],'Body':io.BytesIO(acquisition.migration.encode(receipt))}
        def head_object(self,**kwargs):return {'VersionId':package['version_id'],'ContentLength':package['bytes'],'ChecksumType':'FULL_OBJECT',
            'ChecksumSHA256':base64.b64encode(bytes.fromhex(package['sha256'])).decode(),'ServerSideEncryption':'aws:kms','SSEKMSKeyId':plan['kms']}
    def verify():
        digest=hashlib.sha256(acquisition.migration.encode(receipt)).hexdigest()
        return acquisition.verify_codebuild_admission(Services(),plan,obj,
            's3://'+plan['bucket']+'/operations/checkpoints/migration/'+plan['plan_id']+'/admissions/exact-exomiser/'+digest+'.json',
            'receipt-version',digest,environment={'CODEBUILD_BUILD_ID':record['id'],'CODEBUILD_BUILD_ARN':record['arn'],'AWS_REGION':acquisition.BUILD_REGION},now=now+1)
    return receipt,record,package,verify


@pytest.mark.parametrize('section,key,value',[
    ('build','timeoutInMinutes',46),('environment','privilegedMode',True),('environment','computeType','BUILD_GENERAL1_LARGE'),
    ('receipt','maximum_cost_pretax_usd',.7),('provider','etag','invented'),('provider','url','https://other.test/file'),
    ('target','sha256','0'*64),('admission','archive_sha256','0'*64),('admission','source_zip_sha256','0'*64),
    ('admission','uncharged_pretax_usd',.79),('admission','maximum_attempts',2),('source','sourceVersion','other')])
def test_exact_immutable_admission_cannot_relax_provider_target_funding_or_build(source,monkeypatch,section,key,value):
    receipt,record,package,verify=authorized(source,monkeypatch)
    valid=verify();assert valid['expires_at']==record['startTime'].timestamp()+2700
    target=record if section in ['build','source'] else record['environment'] if section=='environment' else receipt if section=='receipt' else receipt[section]
    target[key]=value
    with pytest.raises(ValueError):verify()


class Cloud(AWS):
    def __init__(self,plan,obj):
        super().__init__(f"operations/checkpoints/migration/{plan['plan_id']}/exact-exomiser-worker/{obj['sha256']}.json",plan['kms'])
        self.claims={};self.claim_puts=0;self.lost_claim=False
    def put_object(self,**request):
        if '/exact-exomiser-claims/' not in request['Key']:return super().put_object(**request)
        self.claim_puts+=1
        if request['Key'] in self.claims:raise error('PreconditionFailed','PutObject')
        raw=request['Body'];head={'VersionId':'claim-version','ContentLength':len(raw),'Metadata':request['Metadata'],
            'ChecksumSHA256':request['ChecksumSHA256'],'ChecksumType':'FULL_OBJECT','ServerSideEncryption':'aws:kms','SSEKMSKeyId':request['SSEKMSKeyId']}
        self.claims[request['Key']]={'raw':raw,'head':head}
        if self.lost_claim:raise ConnectionError('claim response lost')
        return {'VersionId':head['VersionId']}
    def get_object(self,**request):
        if '/exact-exomiser-claims/' not in request['Key']:return super().get_object(**request)
        row=self.claims[request['Key']];return {**row['head'],'Body':io.BytesIO(row['raw'])}


def verified(obj,number=1,reservation=RESERVATION):
    return {'build_arn':build(number),'reservation':reservation,'receipt_sha256':str(number)*64,
        'receipt_version_id':'receipt-'+str(number),'source_zip_sha256':'b'*64,'archive_sha256':obj['sha256'],'expires_at':time.time()+2700}


def test_permanent_attempt_claim_cannot_fund_another_build_receipt_or_archive(source):
    plan,obj,raw=source;cloud=Cloud(plan,obj);first=verified(obj)
    claim=entry.claim_admission(cloud,plan,obj,first)
    assert entry.claim_admission(cloud,plan,obj,first)==claim
    for changed in [verified(obj,2),{**first,'receipt_sha256':'f'*64},{**first,'source_zip_sha256':'f'*64}]:
        with pytest.raises(ValueError,match='already claimed'):entry.claim_admission(cloud,plan,obj,changed)
    assert len(cloud.claims)==1 and cloud.create_calls==0 and not cloud.checkpoint_puts


def test_unknown_claim_is_not_retried_and_fails_before_dataset_access(source):
    plan,obj,raw=source;cloud=Cloud(plan,obj);cloud.lost_claim=True
    with pytest.raises(ConnectionError):entry.claim_admission(cloud,plan,obj,verified(obj))
    assert cloud.claim_puts==1 and cloud.create_calls==0 and not cloud.checkpoint_puts
    cloud.lost_claim=False
    with pytest.raises(ValueError,match='already claimed'):entry.claim_admission(cloud,plan,obj,verified(obj,2))


def test_fresh_funded_terminal_successor_preserves_old_parts_and_admission_history(source,tmp_path,monkeypatch):
    plan,obj,raw=source;cloud=Cloud(plan,obj);original=acquisition.checkpoint;first=verified(obj)
    first['attempt_claim']=entry.claim_admission(cloud,plan,obj,first)
    monkeypatch.setattr(acquisition,'checkpoint',entry.wrapped_checkpoint(original,lambda:cloud,{'value':first}));cloud.fail_part=35
    with pytest.raises(ConnectionError):acquisition.acquire_archive(cloud,plan,obj,MemoryProvider(raw),tmp_path/'one',RESERVATION)
    before=Counter(cloud.part_calls);second=verified(obj,2,'cost_'+'c'*32);second['attempt_claim']=entry.claim_admission(cloud,plan,obj,second)
    monkeypatch.setattr(acquisition,'checkpoint',entry.wrapped_checkpoint(original,lambda:cloud,{'value':second}))
    with pytest.raises(ValueError,match='still owns'):acquisition.acquire_archive(cloud,plan,obj,MemoryProvider(raw),tmp_path/'not-terminal',second['reservation'])
    cloud.terminal(1)
    result=acquisition.acquire_archive(cloud,plan,obj,MemoryProvider(raw),tmp_path/'two',second['reservation'])
    value=saved(tmp_path/'two',obj)
    assert value['reservation']==second['reservation'] and value['prior_admissions'][0]['reservation']==RESERVATION
    assert cloud.create_calls==1 and cloud.part_calls.count(1)==before[1] and cloud.bodies[obj['key']]==raw
    assert result['sha256']==obj['sha256'] and value['durable_worker']['build_arn']==build(2)


def test_direct_cli_cannot_execute_without_the_durable_wrapper(source,tmp_path,monkeypatch):
    import sys
    plan,obj,raw=source
    monkeypatch.setattr(acquisition,'load_local_plan',lambda *args:(plan,obj,{},1))
    monkeypatch.setattr(acquisition,'aws_session',lambda:object())
    monkeypatch.setattr(acquisition,'verify_codebuild_admission',lambda *args:verified(obj))
    monkeypatch.setattr(sys,'argv',['acquirer','--archive',obj['name'],'--execute','--codebuild-admission','synthetic'])
    with pytest.raises(ValueError,match='durable entrypoint'):acquisition.main()


def test_deadline_forbids_commit_and_new_part_attempts(source,tmp_path):
    plan,obj,raw=source
    class Slow(MemoryS3):
        def upload_part(self,**request):time.sleep(.04);return super().upload_part(**request)
    client=Slow()
    with pytest.raises(TimeoutError):
        acquisition.acquire_archive(client,plan,obj,MemoryProvider(raw),tmp_path,RESERVATION,expires_at=time.time()+.01)
    assert client.create_calls==1 and client.complete_calls==0 and len(client.part_calls)<=2 and not client.puts


def test_only_three_identical_private_part_attempts_are_retryable(source,tmp_path):
    plan,obj,raw=source
    class Lost(MemoryS3):
        def __init__(self):super().__init__();self.attempts=[]
        def upload_part(self,**request):
            if request['PartNumber']==1:
                self.attempts.append((request['UploadId'],bytes(request['Body']),request['ChecksumSHA256']))
                raise acquisition.ConnectionClosedError(endpoint_url='https://synthetic.invalid')
            return super().upload_part(**request)
    client=Lost()
    with pytest.raises(acquisition.ConnectionClosedError):acquisition.acquire_archive(client,plan,obj,MemoryProvider(raw),tmp_path,RESERVATION)
    assert len(client.attempts)==3 and len(set(client.attempts))==1
    assert client.create_calls==1 and client.complete_calls==0 and not client.puts


def test_conditional_complete_preserves_an_exact_other_migration_version(source,tmp_path):
    plan,obj,raw=source;client=MemoryS3()
    def winner(request):
        existing(client,plan,obj,raw)
        raise error('PreconditionFailed','CompleteMultipartUpload')
    client.complete_hook=winner
    result=acquisition.acquire_archive(client,plan,obj,MemoryProvider(raw),tmp_path,RESERVATION)
    assert result['version_id']=='original-version' and client.objects[obj['key']]['VersionId']=='original-version'
    assert len(client.aborts)==1 and not client.uploads


def test_checkpoint_cas_race_stops_before_private_archive_create(source,tmp_path,monkeypatch):
    plan,obj,raw=source;cloud=Cloud(plan,obj);current=verified(obj)
    current['attempt_claim']=entry.claim_admission(cloud,plan,obj,current)
    def race(client,request):raise error('PreconditionFailed','PutObject')
    cloud.before_checkpoint_put=race
    monkeypatch.setattr(acquisition,'checkpoint',entry.wrapped_checkpoint(acquisition.checkpoint,lambda:cloud,{'value':current}))
    with pytest.raises(acquisition.ClientError):acquisition.acquire_archive(cloud,plan,obj,MemoryProvider(raw),tmp_path,RESERVATION)
    assert len(cloud.checkpoint_puts)==1 and cloud.create_calls==cloud.complete_calls==0


def test_copied_hold_cannot_claim_the_second_archive(source):
    plan,obj,raw=source;cloud=Cloud(plan,obj);first=verified(obj)
    entry.claim_admission(cloud,plan,obj,first)
    other={**obj,'sha256':'e'*64,'key':'another-exact-original'}
    with pytest.raises(ValueError,match='already claimed'):
        entry.claim_admission(cloud,plan,other,{**first,'archive_sha256':other['sha256']})
    assert len(cloud.claims)==1 and cloud.create_calls==0


def test_expired_whole_build_window_is_rejected_even_with_unexpired_receipt(source,monkeypatch):
    receipt,record,package,verify=authorized(source,monkeypatch)
    record['startTime']=datetime.fromtimestamp(receipt['issued_at']-2700,timezone.utc)
    with pytest.raises(ValueError,match='entire submitted build'):verify()


def test_funded_month_is_bound_and_expiration_clamped_at_utc_boundary(source,monkeypatch):
    late=datetime(2026,10,31,23,59,40,tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(acquisition.time,'time',lambda:late)
    receipt,record,package,verify=authorized(source,monkeypatch)
    assert receipt['admission']['period']=='2026-10'
    assert receipt['expires_at']==datetime(2026,11,1,tzinfo=timezone.utc).timestamp()
    assert verify()['expires_at']==receipt['expires_at']
    receipt['admission']['period']='2026-11'
    with pytest.raises(ValueError):verify()


@pytest.mark.parametrize('rule',[
    {'Status':'Disabled','AbortIncompleteMultipartUpload':{'DaysAfterInitiation':1}},
    {'Status':'Enabled','AbortIncompleteMultipartUpload':{'DaysAfterInitiation':8}},
    {'Status':'Enabled','Prefix':'different/','AbortIncompleteMultipartUpload':{'DaysAfterInitiation':1}},
    {'Status':'Enabled','Filter':{'Tag':{'Key':'not-present','Value':'x'}},'AbortIncompleteMultipartUpload':{'DaysAfterInitiation':1}},
    {'Status':'Enabled'}])
def test_unbounded_or_nonmatching_temporary_storage_cannot_use_the_cost_estimate(source,rule):
    plan,obj,raw=source
    class Client:
        def get_bucket_lifecycle_configuration(self,**kwargs):return {'Rules':[rule]}
    with pytest.raises(ValueError,match='multipart-abort lifecycle'):acquisition.verify_temporary_storage(Client(),plan,obj)


def test_local_package_is_self_verifying_exact_and_contains_no_activation(source,tmp_path,monkeypatch):
    from scripts import prepare_exomiser_acquisition as preparation
    plan,obj,raw=source;plan=copy.deepcopy(plan)
    other_name=next(name for name in acquisition.ARCHIVES if name!=obj['name']);other_raw=b'other synthetic original'
    other_sha=hashlib.sha256(other_raw).hexdigest();monkeypatch.setitem(acquisition.ARCHIVES,other_name,{'bytes':len(other_raw),'sha256':other_sha})
    dataset='ds_migration_'+hashlib.sha256((plan['owner_id']+':'+other_sha).encode()).hexdigest()[:40]
    plan['objects'].append({'name':other_name,'bytes':len(other_raw),'sha256':other_sha,'dataset_id':dataset,
        'key':f"tenants/{plan['owner_id']}/datasets/{dataset}/{other_name}",
        'aliases':[{'name':other_name,'bytes':len(other_raw),'user_id':plan['owner_id']}]})
    plan['total_bytes']+=len(other_raw);plan_raw=acquisition.migration.encode(plan)
    monkeypatch.setattr(acquisition.migration,'APPROVED_PLAN_SHA256',hashlib.sha256(plan_raw).hexdigest())
    folder=tmp_path/'metadata';folder.mkdir();(folder/'plan.json').write_bytes(plan_raw)
    registration={'plan_id':plan['plan_id'],'owner_id':plan['owner_id'],'bucket':plan['bucket'],
        'sha256':acquisition.migration.APPROVED_PLAN_SHA256,'version_id':acquisition.migration.APPROVED_PLAN_VERSION,
        'key':'operations/checkpoints/migration/'+plan['plan_id']+'/plan.json'}
    registration_raw=acquisition.migration.encode(registration);(folder/'registration.json').write_bytes(registration_raw)
    # The package-format unit control has synthetic dependency bodies. The real
    # private package separately requires the exact frozen dependency ZIP SHA.
    members={name:b'# synthetic metadata\n' for name in ['backend/__init__.py','backend/config.py','scripts/prioritize_migration_objects.py','infra/vep-requirements.lock']}
    members.update({'wheels/control'+str(i)+'.whl':b'synthetic wheel body '+bytes([i]) for i in range(7)})
    members['source-file-manifest.json']=json.dumps({'files':[{'path':n,'bytes':len(b),'sha256':hashlib.sha256(b).hexdigest()} for n,b in members.items()]}).encode()
    base=tmp_path/'base.zip'
    with ZipFile(base,'w') as zipped:
        for name,body in members.items():zipped.writestr(name,body)
    monkeypatch.setattr(preparation,'BASE_SHA256',hashlib.sha256(base.read_bytes()).hexdigest())
    output=tmp_path/'prepared';package=preparation.prepare(folder,output,base)
    assert package['maximum_attempt_pretax_usd']==.8 and package['maximum_build_minutes']==45
    assert package['new_shared_hold_required_per_target_per_attempt'] is True and package['ready_or_rights_grants'] is False
    assert set(p.name for p in output.iterdir())=={'source.zip','package.json','source-file-manifest.json','buildspec.json','permission-needs.json'}
    with ZipFile(output/'source.zip') as zipped:
        assert len(zipped.namelist())==18 and not any('admission' in n or 'identity' in n or 'launch' in n for n in zipped.namelist())
        assert zipped.read('metadata/plan.json')==plan_raw and zipped.read('metadata/registration.json')==registration_raw
        assert zipped.read('buildspec.yml')==(output/'buildspec.json').read_bytes()
        spec=json.loads(zipped.read('buildspec.yml'));command=spec['phases']['build']['commands'][0]
        assert '-m scripts.codebuild_exact_exomiser_entry ' in command and command.endswith('--wall-seconds 2700')
        assert '--archive "$EXOMISER_ARCHIVE"' in command
        extracted=tmp_path/'verify';zipped.extractall(extracted)
    result=subprocess.run([sys.executable,'verify_package.py'],cwd=extracted,check=True,capture_output=True,text=True)
    assert json.loads(result.stdout)=={'verified_source_files':17,'verified_offline_wheels':7}
    assert package['source_zip_sha256']==hashlib.sha256((output/'source.zip').read_bytes()).hexdigest()
    assert all(t['new_shared_hold_details']['source_zip_sha256']==package['source_zip_sha256'] for t in package['targets'].values())
    assert all(p.stat().st_mode&0o777==0o600 for p in output.iterdir())
