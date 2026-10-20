"""Adapter for an explicitly published Batch worker contract, never arbitrary jobs."""
import json
import time
from pathlib import Path
from botocore.config import Config
from botocore.exceptions import ClientError
from . import compiler,data,db,security
from .config import aws_session

class UncertainCloudState(RuntimeError):pass

def require_migration_runtime(profile, migrated):
    version=(profile or {}).get('source_grants_version')
    if migrated and (type(version) is not int or version != 1):
        raise ValueError('This tool has no qualified worker route for migrated AWS inputs. Its source data is preserved; publish the reviewed migration-compatible runtime before running this node.')

def execute_batch(uid,rid,aid,spec,node,inputs,work,cancelled):
    require_migration_runtime({'source_grants_version':node.get('_source_grants_version')},bool(node.get('_migration_sources')))
    session=aws_session();s={**data.settings(uid),**(node.get('_cloud_settings') or {})};batch=session.client('batch',config=Config(connect_timeout=5,read_timeout=15,retries={'total_max_attempts':1}));s3=session.client('s3')
    image=node.get('_approved_image')
    definition=batch.describe_job_definitions(jobDefinitions=[node.get('_batch_job_definition') or s['batch_job_definition']]).get('jobDefinitions',[])
    if len(definition)!=1:raise ValueError('Select a published, revision-pinned Batch job-definition ARN')
    definition=definition[0];props=definition.get('containerProperties',{})
    if spec['builtin'] and not image:image=props.get('image')
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
                for field in ['mate','index','dictionary']:
                    if item.get('metadata',{}).get(field):
                        source=item['metadata'][field];target=f"/inputs/{port}/{i}/{source.rsplit('/',1)[-1]}";bindings.append((source,target));item['metadata'][field]=target
                if item.get('metadata',{}).get('index_paths'):
                    updated=[]
                    for source in item['metadata']['index_paths']:
                        target=f"/inputs/{port}/{i}/{source.rsplit('/',1)[-1]}";bindings.append((source,target));updated.append(target)
                    item['metadata']['index_paths']=updated
        plan={'bindings':bindings,'portable_job':{'tool':node['tool'],'params':node['params'],'inputs':mapped,'workdir':'/work','threads':node['resources']['threads'],'max_output_bytes':node.get('_max_output_bytes',64*1024**2)},'steps':[{'argv':['python','-m','backend.portable','/work/portable-job.json'],'stdout':None}],'outputs':{}}
    else:plan=compiler.compile_tool(spec,node,inputs,work)
    prefix=f'tenants/{uid}/runs/{rid}/{aid}/';objects=[];expected_digests={};versions={}
    for items in inputs.values():
        for item in items:
            if item.get('digest'):expected_digests[item['path']]=item['digest'].removeprefix('sha256:')
            if item.get('metadata',{}).get('version_id'):versions[item['path']]=item['metadata']['version_id']
            for secondary in item.get('metadata',{}).get('secondary_objects',[]):
                if secondary.get('version_id'):versions[secondary['uri']]=secondary['version_id']
            for field in ['mate','index']:
                meta=item.get('metadata',{})
                if meta.get(field) and meta.get(field+'_sha256'):expected_digests[meta[field]]=meta[field+'_sha256']
    # Read only exact admitted cloud objects.
    for source,target in plan.pop('bindings'):
        if source.startswith('s3://'):
            source_bucket,source_key=source[5:].split('/',1)
            migration_proof=next((p for p in node.get('_migration_sources',[]) if p['Bucket']==source_bucket and p['Key']==source_key),None)
            if not source_key.startswith('tenants/'+uid+'/') or (source_bucket!=s['s3_bucket'] and not migration_proof):raise ValueError('Cloud input is outside the admitted tenant cache')
            if migration_proof:
                versions[source]=migration_proof['VersionId']
                expected_digests[source]=migration_proof['sha256']
            from . import retention
            retention.lease(uid,source,seconds=3600)
            args={'Bucket':source_bucket,'Key':source_key}
            if versions.get(source):args['VersionId']=versions[source]
            head=s3.head_object(**args);digest=head.get('Metadata',{}).get('sha256')
            if not digest:raise ValueError('Cloud input lacks an admitted SHA-256 identity')
            if source in expected_digests and digest!=expected_digests[source]:raise ValueError('Cloud artifact identity changed after publication')
            objects.append({'bucket':source_bucket,'key':source_key,'version_id':head.get('VersionId'),'target':target,'sha256':digest,'bytes':head['ContentLength']});continue
        raise ValueError('Pipeline inputs must be verified cloud objects; upload through the cloud dataset manager first')
    manifest={'schema_version':1,'tenant':uid,'run':rid,'attempt':aid,'bucket':s['s3_bucket'],'prefix':prefix,'kms_key_arn':s['kms_key_arn'],'inputs':objects,'plan':plan,'wall_seconds':node['resources']['timeout_seconds'],'max_output_bytes':node.get('_max_output_bytes',64*1024**2),'max_scratch_bytes':node['resources'].get('scratch_mb',2048)*1024**2}
    manifest['source_grants']=node.get('_migration_sources',[])
    if spec.get('secret_delivery'):
        if definition.get('tags',{}).get('NGSSecretMemfdProfile')!='1':raise ValueError('A tested memfd-compatible licence profile is required; no plaintext licence file is staged in S3 or Fargate scratch')
        binding=node.get('_credential_node_id',spec['id']);row=security.credential_for(uid,spec['secret_delivery']['provider'],binding)
        if not row or row['mode']!='aws-kms':raise ValueError('Cloud licence delivery requires a KMS-envelope credential bound to this node')
        manifest['secret']={'encrypted':row['encrypted'],'key_envelope':row['key_envelope'],'context':{'tenant':uid,'secret':row['id'],'provider':row['provider'],'version':str(row['version'])}}
        with db.tx() as c:db.audit(c,uid,'vault.cloud-envelope.granted',row['id'],{'attempt':aid,'version':row['version']})
    key=prefix+'manifest.json';s3.put_object(Bucket=s['s3_bucket'],Key=key,Body=db.encode(manifest).encode(),ServerSideEncryption='aws:kms',SSEKMSKeyId=s['kms_key_arn'])
    if node.get('execution_profile')=='exclusive-vm' or node['resources'].get('scratch_mb',2048)>184320:
        from .vm import execute as execute_vm
        execute_vm(uid,rid,aid,{**node,'_approved_image':image},s,key,cancelled)
    else:
        db.event(rid,'log',node['id'],text='Submitting the approved Batch worker in us-east-1; exact input hashes staged.')
        # SubmitJob has no provider idempotency token. Never retry an ambiguous call.
        existing=db.query('SELECT provider_id,metrics FROM attempts WHERE id=?',(aid,),one=True);job_id=existing['provider_id'] if existing else None
        if not job_id and existing and json.loads(existing['metrics']).get('submission_intent'):
            jobs=batch.list_jobs(jobQueue=node.get('_batch_queue') or s['batch_queue'],filters=[{'name':'JOB_NAME','values':['ngs-'+aid]}]).get('jobSummaryList',[])
            if len(jobs)!=1:raise UncertainCloudState('Provider submission intent has no unique matching job; reservation retained for operator reconciliation')
            job_id=jobs[0]['jobId']
        try:
            if not job_id:
                with db.tx() as c:c.execute('UPDATE attempts SET metrics=? WHERE id=?',(db.encode({'submission_intent':True}),aid))
                response=batch.submit_job(jobName='ngs-'+aid,jobQueue=node.get('_batch_queue') or s['batch_queue'],jobDefinition=definition['jobDefinitionArn'],containerOverrides={'command':['python','/opt/studio/cloud_worker.py',s['s3_bucket'],key],'resourceRequirements':[{'type':'VCPU','value':str(node['resources']['threads'])},{'type':'MEMORY','value':str(node['resources']['memory_mb'])}]},timeout={'attemptDurationSeconds':max(60,node['resources']['timeout_seconds'])},retryStrategy={'attempts':1},tags={'Project':'OpenNGS-Pro','TenantId':uid,'RunId':rid,'AttemptId':aid});job_id=response['jobId']
        except ClientError as e:
            if e.response.get('ResponseMetadata',{}).get('HTTPStatusCode',500)<500:raise ValueError('AWS rejected the launch: '+e.response['Error'].get('Message','Request denied')) from e
            raise UncertainCloudState('Batch submission outcome is unknown; reservation retained. Reconcile job name ngs-'+aid+' before retrying.') from e
        except Exception as e:raise UncertainCloudState('Batch submission outcome is unknown; reservation retained. Reconcile job name ngs-'+aid+' before retrying.') from e
        with db.tx() as c:c.execute('UPDATE attempts SET provider_id=? WHERE id=?',(job_id,aid))
        started=time.monotonic();termination=False;termination_at=None
        while True:
            try:jobs=batch.describe_jobs(jobs=[job_id])['jobs']
            except Exception as e:raise UncertainCloudState('Cannot reconcile Batch resource '+job_id+'; reservation retained.') from e
            if not jobs:raise UncertainCloudState('Batch job disappeared from inventory; reservation retained: '+job_id)
            job=jobs[0];status=job['status'];db.event(rid,'task.metrics',node['id'],provider='AWS Batch',provider_status=status,elapsed_seconds=round(time.monotonic()-started,1),source='provider')
            from . import cloud_logs
            cloud_logs.drain(uid,rid,aid,node['id'],job,sensitive=bool(spec.get('secret_delivery')))
            if status in ['SUCCEEDED','FAILED']:break
            elapsed=(time.time()-job['startedAt']/1000) if job.get('startedAt') else 0
            if (cancelled(rid) or elapsed>node['resources']['timeout_seconds']+120) and not termination:
                batch.terminate_job(jobId=job_id,reason='NGS Studio cancellation or wall-time circuit breaker');termination=True;termination_at=time.monotonic()
            if termination and time.monotonic()-termination_at>60:raise UncertainCloudState('Termination unconfirmed for '+job_id+'; quota reservation remains held.')
            time.sleep(2)
        from . import metering
        metering.record(aid,metering.fargate(job,node['resources']['threads'],node['resources']['memory_mb']))
        if termination:raise InterruptedError('AWS Batch termination confirmed')
        if status!='SUCCEEDED':raise ValueError('AWS Batch failed: '+job.get('statusReason','Worker failure'))
    response=s3.get_object(Bucket=s['s3_bucket'],Key=prefix+'result.json')
    if response['ContentLength']>8*1024*1024:raise ValueError('Cloud output manifest exceeds its size ceiling')
    result=json.loads(response['Body'].read())
    if not isinstance(result,dict):raise ValueError('Invalid cloud result manifest')
    for items in result.values():
        for item in items:
            if not item['path'].startswith('s3://'+s['s3_bucket']+'/'+prefix+'outputs/'):raise ValueError('Cloud output escaped its admitted run prefix')
            if not isinstance(item.get('bytes'),int) or item['bytes']<0 or not __import__('re').fullmatch(r'sha256:[a-f0-9]{64}',item.get('digest','')):raise ValueError('Cloud output identity is incomplete')
            meta=item.setdefault('metadata',{});meta['sample_only']=False;meta['untrusted_research']=True;secondary_objects=[]
            companions=[meta.get(field) for field in ['mate','index','reference','dictionary']]+meta.get('index_paths',[])
            for secondary in companions:
                if secondary and str(secondary).startswith('s3://') and not secondary.startswith('s3://'+s['s3_bucket']+'/'+prefix+'outputs/'):raise ValueError('Cloud companion escaped its run prefix')
                if secondary and str(secondary).startswith('s3://'):
                    head=s3.head_object(Bucket=s['s3_bucket'],Key=secondary[5:].split('/',1)[1]);secondary_objects.append({'uri':secondary,'bytes':head['ContentLength'],'sha256':head.get('Metadata',{}).get('sha256'),'version_id':head.get('VersionId')})
            meta['secondary_objects']=secondary_objects
            path=item['path'][5:].split('/',1)[1];head=s3.head_object(Bucket=s['s3_bucket'],Key=path)
            if head['ContentLength']!=item['bytes'] or head.get('Metadata',{}).get('sha256')!=item['digest'][7:]:raise ValueError('Cloud output does not match its committed object identity')
            meta['version_id']=head.get('VersionId')
    sizes={i['path']:i['bytes'] for rows in result.values() for i in rows}
    for rows in result.values():
        for item in rows:sizes.update({x['uri']:x['bytes'] for x in item['metadata']['secondary_objects']})
    if sum(sizes.values())>manifest['max_output_bytes']:raise ValueError('Cloud output exceeds its accepted byte allowance')
    return result
