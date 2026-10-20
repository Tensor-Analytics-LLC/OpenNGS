"""Adapter for an explicitly published Batch worker contract, never arbitrary jobs."""
import json
import time
from pathlib import Path
from botocore.config import Config
from botocore.exceptions import ClientError
from . import compiler,data,db,security
from .config import aws_session

class UncertainCloudState(RuntimeError):pass

def execute_batch(uid,rid,aid,spec,node,inputs,work,cancelled):
    session=aws_session();s=data.settings(uid);batch=session.client('batch',config=Config(connect_timeout=5,read_timeout=15));s3=session.client('s3')
    image=node.get('_approved_image')
    definition=batch.describe_job_definitions(jobDefinitions=[node.get('_batch_job_definition') or s['batch_job_definition']]).get('jobDefinitions',[])
    if len(definition)!=1:raise ValueError('Select a published, revision-pinned Batch job-definition ARN')
    definition=definition[0];props=definition.get('containerProperties',{})
    if spec['builtin']:image=props.get('image')
    if definition.get('status')!='ACTIVE' or props.get('image')!=image or not image or '@sha256:' not in image or definition.get('tags',{}).get('NGSWorkerContract')!='1':raise ValueError('Job-definition image/worker contract does not match the approved node release')
    if props.get('privileged') or not props.get('readonlyRootFilesystem'):raise ValueError('Published Batch profile must disable privilege and use a read-only root')
    if spec.get('resource')=='gpu':
        if definition.get('tags',{}).get('NGSIsolationProfile')!='exclusive-vm-reviewed':raise ValueError('GPU publication requires the reviewed exclusive-VM profile')
    elif 'FARGATE' not in definition.get('platformCapabilities',[]):raise ValueError('The CPU lane requires a published Fargate isolation profile')
    if not s['s3_bucket'] or not s['kms_key_arn']:raise ValueError('Configure the private encrypted us-east-1 cache first')
    if spec['builtin']:
        import copy
        mapped=copy.deepcopy(inputs);bindings=[]
        for port,rows in mapped.items():
            for i,item in enumerate(rows):
                source=item['path'];target=f"/inputs/{port}/{i}/{source.rsplit('/',1)[-1]}";bindings.append((source,target));item['path']=target
                for field in ['mate','index']:
                    if item.get('metadata',{}).get(field):
                        source=item['metadata'][field];target=f"/inputs/{port}/{i}/{source.rsplit('/',1)[-1]}";bindings.append((source,target));item['metadata'][field]=target
        plan={'bindings':bindings,'portable_job':{'tool':node['tool'],'params':node['params'],'inputs':mapped,'workdir':'/work','threads':node['resources']['threads']},'steps':[{'argv':['python','-m','backend.portable','/work/portable-job.json'],'stdout':None}],'outputs':{}}
    else:plan=compiler.compile_tool(spec,node,inputs,work)
    prefix=f'tenants/{uid}/runs/{rid}/{aid}/';objects=[];expected_digests={}
    for items in inputs.values():
        for item in items:
            if item.get('digest'):expected_digests[item['path']]=item['digest'].removeprefix('sha256:')
            for field in ['mate','index']:
                meta=item.get('metadata',{})
                if meta.get(field) and meta.get(field+'_sha256'):expected_digests[meta[field]]=meta[field+'_sha256']
    # Stage exact files, not entire SSD directories or all tenant databases.
    for source,target in plan.pop('bindings'):
        if source.startswith('s3://'):
            source_bucket,source_key=source[5:].split('/',1)
            if source_bucket!=s['s3_bucket'] or not source_key.startswith('tenants/'+uid+'/'):raise ValueError('Cloud input is outside the admitted tenant cache')
            head=s3.head_object(Bucket=source_bucket,Key=source_key);digest=head.get('Metadata',{}).get('sha256')
            if not digest:raise ValueError('Cloud input lacks an admitted SHA-256 identity')
            if source in expected_digests and digest!=expected_digests[source]:raise ValueError('Cloud artifact identity changed after publication')
            objects.append({'bucket':source_bucket,'key':source_key,'target':target,'sha256':digest,'bytes':head['ContentLength']});continue
        source=Path(source)
        if source.stat().st_size>s['max_upload_gib']*1024**3:raise ValueError('Input exceeds this workspace staging ceiling')
        digest=data.hash_file(source);key=f'tenants/{uid}/cache/sha256/{digest}/blob'
        try:h=s3.head_object(Bucket=s['s3_bucket'],Key=key);exists=h.get('Metadata',{}).get('sha256')==digest
        except s3.exceptions.ClientError as e:
            if e.response['Error']['Code'] not in ['404','NoSuchKey','NotFound']:raise
            exists=False
        if not exists:
            from boto3.s3.transfer import TransferConfig
            tid=db.ident('transfer_')
            with db.tx() as c:
                if data.storage_exposure(c,uid)+source.stat().st_size>s['max_upload_gib']*1024**3:raise ValueError('Cumulative retained-cache quota exhausted')
                c.execute('INSERT INTO transfers(id,user_id,asset_id,state,total,digest,started) VALUES(?,?,?,?,?,?,?)',(tid,uid,aid,'staging',source.stat().st_size,digest,time.time()))
            try:
                s3.upload_file(str(source),s['s3_bucket'],key,ExtraArgs={'ServerSideEncryption':'aws:kms','SSEKMSKeyId':s['kms_key_arn'],'Metadata':{'sha256':digest}},Config=TransferConfig(multipart_threshold=64*1024*1024,multipart_chunksize=64*1024*1024,max_concurrency=4))
                with db.tx() as c:c.execute("UPDATE transfers SET state='complete',bytes=total,s3_uri=?,finished=? WHERE id=?",('s3://'+s['s3_bucket']+'/'+key,time.time(),tid))
            except BaseException:
                with db.tx() as c:c.execute("UPDATE transfers SET state='failed',finished=? WHERE id=?",(time.time(),tid))
                raise
        objects.append({'bucket':s['s3_bucket'],'key':key,'target':target,'sha256':digest,'bytes':source.stat().st_size})
    manifest={'schema_version':1,'tenant':uid,'run':rid,'attempt':aid,'bucket':s['s3_bucket'],'prefix':prefix,'kms_key_arn':s['kms_key_arn'],'inputs':objects,'plan':plan,'wall_seconds':node['resources']['timeout_seconds']}
    if spec.get('secret_delivery'):
        if definition.get('tags',{}).get('NGSSecretMemfdProfile')!='1':raise ValueError('A tested memfd-compatible licence profile is required; no plaintext licence file is staged in S3 or Fargate scratch')
        binding=node.get('_credential_node_id',spec['id']);row=security.credential_for(uid,spec['secret_delivery']['provider'],binding)
        if not row or row['mode']!='aws-kms':raise ValueError('Cloud licence delivery requires a KMS-envelope credential bound to this node')
        manifest['secret']={'encrypted':row['encrypted'],'key_envelope':row['key_envelope'],'context':{'tenant':uid,'secret':row['id'],'provider':row['provider'],'version':str(row['version'])}}
        with db.tx() as c:db.audit(c,uid,'vault.cloud-envelope.granted',row['id'],{'attempt':aid,'version':row['version']})
    key=prefix+'manifest.json';s3.put_object(Bucket=s['s3_bucket'],Key=key,Body=db.encode(manifest).encode(),ServerSideEncryption='aws:kms',SSEKMSKeyId=s['kms_key_arn'])
    db.event(rid,'log',node['id'],text='Submitting the approved Batch worker in us-east-1; exact input hashes staged.')
    # SubmitJob has no provider idempotency token. Never retry an ambiguous call.
    try:
        response=batch.submit_job(jobName='ngs-'+aid,jobQueue=node.get('_batch_queue') or s['batch_queue'],jobDefinition=definition['jobDefinitionArn'],containerOverrides={'command':['python','/opt/studio/cloud_worker.py',s['s3_bucket'],key],'resourceRequirements':[{'type':'VCPU','value':str(node['resources']['threads'])},{'type':'MEMORY','value':str(node['resources']['memory_mb'])}]},timeout={'attemptDurationSeconds':max(60,node['resources']['timeout_seconds'])},retryStrategy={'attempts':1},tags={'Project':'ngs-studio','TenantId':uid,'RunId':rid,'AttemptId':aid})
    except ClientError as e:
        if e.response.get('ResponseMetadata',{}).get('HTTPStatusCode',500)<500:raise ValueError('AWS rejected the launch: '+e.response['Error'].get('Message','Request denied')) from e
        raise UncertainCloudState('Batch submission outcome is unknown; reservation retained. Reconcile job name ngs-'+aid+' before retrying.') from e
    except Exception as e:raise UncertainCloudState('Batch submission outcome is unknown; reservation retained. Reconcile job name ngs-'+aid+' before retrying.') from e
    job_id=response['jobId']
    with db.tx() as c:c.execute('UPDATE attempts SET provider_id=? WHERE id=?',(job_id,aid))
    started=time.monotonic();termination=False;termination_at=None
    while True:
        try:jobs=batch.describe_jobs(jobs=[job_id])['jobs']
        except Exception as e:raise UncertainCloudState('Cannot reconcile Batch resource '+job_id+'; reservation retained.') from e
        if not jobs:raise UncertainCloudState('Batch job disappeared from inventory; reservation retained: '+job_id)
        job=jobs[0];status=job['status'];db.event(rid,'task.metrics',node['id'],provider='AWS Batch',provider_status=status,elapsed_seconds=round(time.monotonic()-started,1),source='provider')
        if status in ['SUCCEEDED','FAILED']:break
        if (cancelled(rid) or time.monotonic()-started>node['resources']['timeout_seconds']+120) and not termination:
            batch.terminate_job(jobId=job_id,reason='NGS Studio cancellation or wall-time circuit breaker');termination=True;termination_at=time.monotonic()
        if termination and time.monotonic()-termination_at>60:raise UncertainCloudState('Termination unconfirmed for '+job_id+'; quota reservation remains held.')
        time.sleep(2)
    if termination:raise InterruptedError('AWS Batch termination confirmed')
    if status!='SUCCEEDED':raise ValueError('AWS Batch failed: '+job.get('statusReason','Worker failure'))
    response=s3.get_object(Bucket=s['s3_bucket'],Key=prefix+'result.json')
    if response['ContentLength']>8*1024*1024:raise ValueError('Cloud output manifest exceeds its size ceiling')
    result=json.loads(response['Body'].read())
    if not isinstance(result,dict):raise ValueError('Invalid cloud result manifest')
    for items in result.values():
        for item in items:
            if not item['path'].startswith('s3://'+s['s3_bucket']+'/'+prefix+'outputs/'):raise ValueError('Cloud output escaped its admitted run prefix')
    return result
