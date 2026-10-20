"""Durable private checkpoint wrapper for the reviewed exact-Exomiser CodeBuild route.

The original acquisition entrypoint verifies its immutable app-issued admission
before this wrapper restores, claims or writes a checkpoint. Only one exact
private checkpoint object is mirrored; dataset registration stays with the
ordinary migration verifier. No database or runtime publication occurs here.
"""
import base64
import contextlib
import copy
import hashlib
import json
import re
import time
import uuid

from botocore.config import Config
from botocore.exceptions import ClientError
from scripts import acquire_exact_exomiser_archives as acquisition


MAX_CHECKPOINT_BYTES = 2 * 1024**2
BINDING_KEYS = {'schema','plan_id','plan_sha256','owner_id','bucket','key','sha256','bytes',
                'source','part_bytes','reservation'}
TERMINAL_BUILDS = {'SUCCEEDED','FAILED','FAULT','STOPPED','TIMED_OUT'}
IMMEDIATE_STATES = {'creating','uploading','completing','completed','failed-sha256'}


def absence_keys():
    owner=acquisition.migration.APPROVED_OWNER;prefix='operations/checkpoints/migration/'+acquisition.migration.APPROVED_PLAN+'/'
    keys=set()
    for name,profile in acquisition.ARCHIVES.items():
        digest=profile['sha256'];dataset='ds_migration_'+hashlib.sha256((owner+':'+digest).encode()).hexdigest()[:40]
        keys.update({f'tenants/{owner}/datasets/{dataset}/{name}',prefix+'exact-exomiser-worker/'+digest+'.json',prefix+'uploaded/'+digest+'.json'})
    return keys


class ExactKeyS3:
    """Resolve only known current-key absence without a bucket-wide list grant."""
    def __init__(self,client):self.client=client
    def __getattr__(self,name):return getattr(self.client,name)
    def head_object(self,**request):
        try:return self.client.head_object(**request)
        except ClientError as denied:
            code=denied.response.get('Error',{}).get('Code')
            status=denied.response.get('ResponseMetadata',{}).get('HTTPStatusCode')
            if (code not in ['403','AccessDenied','Forbidden'] and status!=403
                    or request.get('Bucket')!=acquisition.migration.APPROVED_BUCKET
                    or request.get('Key') not in absence_keys() or request.get('VersionId')):
                raise
            # A missing current object can produce 403 when ListBucket is
            # restricted by prefix. Never infer absence from that denial alone.
            # A versioned read cannot be resolved from a current-object listing.
            try:
                listed=self.client.list_objects_v2(Bucket=request['Bucket'],Prefix=request['Key'],MaxKeys=1)
            except Exception:
                raise denied from None
            rows=listed.get('Contents',[])
            if not isinstance(rows,list) or rows and (not isinstance(rows[0],dict) or not isinstance(rows[0].get('Key'),str)):
                raise denied from None
            if not rows or rows[0].get('Key')!=request['Key']:
                raise ClientError({'Error':{'Code':'404','Message':'Exact current key absent in authorized prefix listing'},
                                   'ResponseMetadata':{'HTTPStatusCode':404}},'HeadObject') from denied
            raise denied from None


class ScopedSession:
    def __init__(self,session):self.session=session
    def client(self,name,**kwargs):
        client=self.session.client(name,**kwargs)
        return ExactKeyS3(client) if name=='s3' else client


def build_id(arn):
    prefix=f'arn:aws:codebuild:{acquisition.BUILD_REGION}:{acquisition.BUILD_ACCOUNT}:build/'
    if not re.fullmatch(re.escape(prefix+acquisition.BUILD_PROJECT+':')+r'[a-f0-9-]{36}',arn or ''):
        raise ValueError('Durable checkpoint requires an exact reviewed CodeBuild owner ARN')
    return arn[len(prefix):]


def claim_admission(session,plan,obj,verified):
    """One permanent attempt claim per funded hold, before any dataset request.

    It prevents another build or copied receipt reusing that hold. StartBuild is
    operator-only: this guard cannot refund the minimum charge of an externally
    submitted invalid build. An unknown claim write is never retried here.
    """
    build_id(verified.get('build_arn'));reservation=verified.get('reservation')
    if (not re.fullmatch(r'cost_[a-f0-9]{32}',reservation or '')
            or verified.get('archive_sha256')!=obj['sha256']
            or any(not re.fullmatch('[a-f0-9]{64}',verified.get(k,'')) for k in ['source_zip_sha256','receipt_sha256'])
            or verified.get('receipt_version_id') in [None,'','null']):
        raise ValueError('An exact authenticated single-attempt admission is required before any claim')
    key=f"operations/checkpoints/migration/{plan['plan_id']}/exact-exomiser-claims/{reservation}.json"
    value={'schema':1,'purpose':acquisition.PURPOSE,'plan_sha256':acquisition.migration.APPROVED_PLAN_SHA256,
           'owner_id':plan['owner_id'],'archive_sha256':obj['sha256'],'target_key':obj['key'],
           **{k:verified[k] for k in ['reservation','build_arn','source_zip_sha256','receipt_sha256','receipt_version_id']}}
    body=acquisition.migration.encode(value);digest=hashlib.sha256(body).hexdigest()
    metadata={'sha256':digest,'tenant':plan['owner_id'],'claim':'exact-exomiser-attempt-v1'}
    client=session.client('s3',config=Config(connect_timeout=5,read_timeout=30,retries={'total_max_attempts':1}))
    try:
        result=client.put_object(Bucket=plan['bucket'],Key=key,Body=body,IfNoneMatch='*',
            ServerSideEncryption='aws:kms',SSEKMSKeyId=plan['kms'],BucketKeyEnabled=True,
            ChecksumSHA256=base64.b64encode(bytes.fromhex(digest)).decode(),Metadata=metadata)
        version=result.get('VersionId')
        if version in [None,'','null']:raise ValueError('Attempt claim returned no immutable version')
        response=client.get_object(Bucket=plan['bucket'],Key=key,VersionId=version,ChecksumMode='ENABLED')
    except ClientError as error:
        if error.response['Error']['Code']!='PreconditionFailed':raise
        response=client.get_object(Bucket=plan['bucket'],Key=key,ChecksumMode='ENABLED')
        version=response.get('VersionId')
    try:
        if (version in [None,'','null'] or response.get('VersionId')!=version
                or response.get('ContentLength')!=len(body) or response.get('Metadata')!=metadata
                or response.get('ServerSideEncryption')!='aws:kms' or response.get('SSEKMSKeyId')!=plan['kms']
                or response.get('ChecksumType','FULL_OBJECT')!='FULL_OBJECT'
                or response.get('ChecksumSHA256')!=base64.b64encode(bytes.fromhex(digest)).decode()
                or response['Body'].read(16385)!=body):
            raise ValueError('This funded hold is already claimed by another build, source, receipt or archive')
    finally:response['Body'].close()
    return {'key':key,'version_id':version,'sha256':digest}


class Mirror:
    def __init__(self,session,plan,obj,verified,binding):
        if not verified or verified.get('reservation')!=binding['reservation']:
            raise ValueError('Immutable CodeBuild admission must pass before any checkpoint access')
        claim=verified.get('attempt_claim',{})
        if (claim.get('key')!=f"operations/checkpoints/migration/{plan['plan_id']}/exact-exomiser-claims/{binding['reservation']}.json"
                or claim.get('version_id') in [None,'','null'] or not re.fullmatch('[a-f0-9]{64}',claim.get('sha256',''))):
            raise ValueError('The fresh single-attempt funding claim must pass before checkpoint access')
        build_id(verified.get('build_arn'))
        if (binding.get('plan_id'),binding.get('owner_id'),binding.get('sha256'),binding.get('key'))!=(
                plan['plan_id'],plan['owner_id'],obj['sha256'],obj['key']):
            raise ValueError('Private checkpoint binding differs from the admitted original source')
        self.client=session.client('s3',config=Config(connect_timeout=5,read_timeout=30,retries={'total_max_attempts':1}))
        self.codebuild=session.client('codebuild')
        self.plan=plan;self.obj=obj;self.verified=verified;self.binding=binding
        self.key=f"operations/checkpoints/migration/{plan['plan_id']}/exact-exomiser-worker/{obj['sha256']}.json"
        self.head=None;self.uncertain=False;self.sequence=0;self.last_parts=0;self.last_content=None

    def _metadata(self,digest,owner):
        return {'sha256':digest,'tenant':self.plan['owner_id'],'migration':self.plan['plan_id'],
                'archive-sha256':self.obj['sha256'],'owner-build-arn':owner,'checkpoint':'exact-exomiser-worker-v1'}

    def _head(self):
        try:return self.client.head_object(Bucket=self.plan['bucket'],Key=self.key,ChecksumMode='ENABLED')
        except ClientError as error:
            if error.response['Error']['Code'] in acquisition.migration.MISSING:return None
            raise

    def _validate_head(self,head,digest,owner):
        if (head.get('VersionId') in [None,'','null'] or not head.get('ETag')
                or type(head.get('ContentLength')) is not int or not 0<head['ContentLength']<=MAX_CHECKPOINT_BYTES
                or head.get('ServerSideEncryption')!='aws:kms' or head.get('SSEKMSKeyId')!=self.plan['kms']
                or head.get('ChecksumType','FULL_OBJECT')!='FULL_OBJECT'
                or not re.fullmatch('[a-f0-9]{64}',digest or '')
                or head.get('ChecksumSHA256')!=base64.b64encode(bytes.fromhex(digest)).decode()
                or head.get('Metadata')!=self._metadata(digest,owner)):
            raise ValueError('Durable checkpoint lacks its exact immutable version, KMS encryption or full checksum identity')

    def restore(self):
        head=self._head()
        if head is None:return None
        metadata=head.get('Metadata',{});digest=metadata.get('sha256');owner=metadata.get('owner-build-arn')
        build_id(owner);self._validate_head(head,digest,owner)
        response=self.client.get_object(Bucket=self.plan['bucket'],Key=self.key,VersionId=head['VersionId'],ChecksumMode='ENABLED')
        try:
            if response.get('VersionId')!=head['VersionId'] or response.get('ContentLength')!=head['ContentLength']:
                raise ValueError('Restored checkpoint differs from its exact immutable HEAD version')
            raw=response['Body'].read(MAX_CHECKPOINT_BYTES+1)
        finally:response['Body'].close()
        if len(raw)!=head['ContentLength'] or hashlib.sha256(raw).hexdigest()!=digest:
            raise ValueError('Restored checkpoint bytes differ from the confirmed full SHA-256')
        value=json.loads(raw)
        if not isinstance(value,dict) or any(value.get(key)!=expected for key,expected in self.binding.items() if key!='reservation'):
            raise ValueError('Restored checkpoint belongs to another original plan, owner, source, reservation or upload layout')
        if not re.fullmatch(r'cost_[a-f0-9]{32}',value.get('reservation','')):
            raise ValueError('Restored checkpoint lacks its original funded admission')
        worker=value.get('durable_worker',{})
        if (worker.get('build_arn')!=owner or type(worker.get('sequence')) is not int or worker['sequence']<1
                or not re.fullmatch('[a-f0-9]{64}',worker.get('receipt_sha256',''))
                or worker.get('receipt_version_id') in [None,'','null']):
            raise ValueError('Durable checkpoint worker ownership or admission provenance is incomplete')
        if owner!=self.verified['build_arn']:
            records=self.codebuild.batch_get_builds(ids=[build_id(owner)]).get('builds',[])
            if (len(records)!=1 or records[0].get('arn')!=owner or records[0].get('id')!=build_id(owner)
                    or records[0].get('projectName')!=acquisition.BUILD_PROJECT or records[0].get('serviceRole')!=acquisition.BUILD_ROLE
                    or records[0].get('buildStatus') not in TERMINAL_BUILDS or records[0].get('buildComplete') is not True):
                raise ValueError('Another or unreconciled CodeBuild worker still owns this private checkpoint')
        self.head=head;self.sequence=worker['sequence'];self.last_parts=len(value.get('parts',{}))
        self.last_content=self._content(value)
        if value['reservation']!=self.binding['reservation']:
            history=value.get('prior_admissions',[])
            if not isinstance(history,list) or len(history)>=128:raise ValueError('Prior admission history requires operator review')
            value['prior_admissions']=[*history,{'reservation':value['reservation'],
                **{key:worker[key] for key in ['build_arn','receipt_sha256','receipt_version_id']}}]
            value['reservation']=self.binding['reservation']
        return value

    @staticmethod
    def _content(value):
        return acquisition.migration.encode({key:item for key,item in value.items() if key!='durable_worker'})

    def persist(self,value,*,force=False):
        if self.uncertain:raise ValueError('A checkpoint mutation has an unknown outcome; no automatic write retry is allowed')
        content=self._content(value)
        if not force and content==self.last_content:return
        current=self._head()
        if self.head is None:
            if current is not None:raise ValueError('Another worker claimed this checkpoint before the conditional write')
            condition={'IfNoneMatch':'*'}
        else:
            if current is None or (current.get('VersionId'),current.get('ETag'))!=(self.head['VersionId'],self.head['ETag']):
                raise ValueError('Durable checkpoint ownership changed; this worker stopped before another mutation')
            condition={'IfMatch':self.head['ETag']}
        worker={'build_arn':self.verified['build_arn'],'sequence':self.sequence+1,'write_id':uuid.uuid4().hex,
                'previous_version_id':self.head['VersionId'] if self.head else None,
                'receipt_sha256':self.verified['receipt_sha256'],'receipt_version_id':self.verified['receipt_version_id'],
                'admission_expires_at':self.verified['expires_at'],'updated_at':time.time()}
        body=acquisition.migration.encode({**value,'durable_worker':worker})
        if len(body)>MAX_CHECKPOINT_BYTES:raise ValueError('Private durable checkpoint exceeds its metadata bound')
        digest=hashlib.sha256(body).hexdigest()
        try:
            response=self.client.put_object(Bucket=self.plan['bucket'],Key=self.key,Body=body,
                ServerSideEncryption='aws:kms',SSEKMSKeyId=self.plan['kms'],BucketKeyEnabled=True,
                ChecksumSHA256=base64.b64encode(bytes.fromhex(digest)).decode(),
                Metadata=self._metadata(digest,self.verified['build_arn']),**condition)
            version=response.get('VersionId')
            if version in [None,'','null'] or not response.get('ETag'):
                raise ValueError('Durable checkpoint write returned no immutable version and ETag')
            pinned=self.client.head_object(Bucket=self.plan['bucket'],Key=self.key,VersionId=version,ChecksumMode='ENABLED')
            self._validate_head(pinned,digest,self.verified['build_arn'])
            if pinned['VersionId']!=version or pinned['ETag']!=response['ETag']:
                raise ValueError('Durable checkpoint write did not preserve its exact confirmed version')
        except BaseException:
            # Even an apparent network failure may have committed a new version.
            # A later build reads that version; this process never retries it.
            self.uncertain=True
            raise
        self.head=pinned;self.sequence=worker['sequence'];self.last_parts=len(value.get('parts',{}));self.last_content=content
        value['durable_worker']=worker


def wrapped_checkpoint(original,session,verified):
    @contextlib.contextmanager
    def durable(directory,plan,obj,reservation):
        if not verified.get('value'):
            raise ValueError('The original immutable CodeBuild admission must pass before checkpoint bootstrap')
        with original(directory,plan,obj,reservation) as (value,save):
            binding={key:value.get(key) for key in BINDING_KEYS}
            mirror=Mirror(session(),plan,obj,verified['value'],binding)
            restored=mirror.restore()
            if restored is not None:value.clear();value.update(restored);save()
            # Claim with a CAS before yielding to any original multipart mutation.
            mirror.persist(value,force=True);save(durable_worker=value['durable_worker'])
            def durable_save(**changes):
                save(**changes)
                immediate=changes.get('state') in IMMEDIATE_STATES or 'upload_id' in changes
                if immediate or len(value.get('parts',{}))-mirror.last_parts>=32:
                    mirror.persist(value);save(durable_worker=value['durable_worker'])
            try:yield value,durable_save
            except BaseException as error:
                if not mirror.uncertain:
                    # Evidence contains an exception class only; no credential,
                    # local source path or provider request text is copied.
                    save(durable_failure={'exception':type(error).__name__,'at':time.time()})
                    mirror.persist(value);save(durable_worker=value['durable_worker'])
                raise
            else:
                mirror.persist(value);save(durable_worker=value['durable_worker'])
    return durable


def main():
    original_verify=acquisition.verify_codebuild_admission;original_checkpoint=acquisition.checkpoint
    original_session=acquisition.aws_session
    verified={}
    def verify(*args,**kwargs):
        value=original_verify(*args,**kwargs)
        value['attempt_claim']=claim_admission(args[0],args[1],args[2],value)
        verified['value']=value
        return value
    acquisition.verify_codebuild_admission=verify
    acquisition.aws_session=lambda:ScopedSession(original_session())
    acquisition.checkpoint=wrapped_checkpoint(original_checkpoint,acquisition.aws_session,verified)
    try:acquisition.main()
    finally:
        acquisition.verify_codebuild_admission=original_verify
        acquisition.checkpoint=original_checkpoint
        acquisition.aws_session=original_session


if __name__=='__main__':
    try:main()
    except Exception as error:
        raise SystemExit('Durable exact Exomiser acquisition stopped ('+type(error).__name__+'). Original objects and normal migration state were preserved.')
