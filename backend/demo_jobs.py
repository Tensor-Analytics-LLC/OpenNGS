"""Durable fair demo scheduler. API restarts never discard accepted work.

One pinned Fargate task executes a whole curated DAG. The task has no IAM role;
it receives only a short-lived manifest GET and a size-limited output POST.
"""
import hashlib
import io
import json
import os
import tarfile
import time
from pathlib import Path,PurePosixPath
from botocore.config import Config
from botocore.exceptions import ClientError
from . import budget,data,db,graph,registry,entitlements
from .config import DATA,TERMINAL,aws_session

def initialize():
    with db.tx() as c:
        c.execute('CREATE TABLE IF NOT EXISTS demo_tasks(run_id TEXT PRIMARY KEY,state TEXT NOT NULL,provider_id TEXT,manifest_key TEXT NOT NULL,output_key TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,log_cursor TEXT)')
        c.execute('CREATE TABLE IF NOT EXISTS demo_cache(user_id TEXT NOT NULL,digest TEXT NOT NULL,run_id TEXT NOT NULL,PRIMARY KEY(user_id,digest))')

def clients():
    s=aws_session()
    return s.client('batch',config=Config(connect_timeout=5,read_timeout=20,retries={'total_max_attempts':1})),s.client('s3',config=Config(signature_version='s3v4',connect_timeout=5,read_timeout=30))

def runtime():
    queue=os.getenv('NGS_DEMO_QUEUE','');definition=os.getenv('NGS_DEMO_JOB_DEFINITION','')
    if not queue or not definition:return None
    return queue,definition

def extract_outputs(body,folder):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(body),mode='r:gz') as archive:
        members=archive.getmembers();by_name={m.name:m for m in members}
        if len(members)>512 or len(by_name)!=len(members):raise ValueError('Sandbox archive has too many or duplicate members')
        expanded=0
        for m in members:
            name=PurePosixPath(m.name)
            if name.is_absolute() or '..' in name.parts or (m.name!='manifest.json' and not m.name.startswith('files/')):raise ValueError('Unsafe sandbox output path')
            if m.islnk():
                # Older reviewed workers emitted TAR hardlinks for duplicate
                # inodes. Copy bytes from one verified regular archive member;
                # never create a filesystem link or follow a chain of links.
                target=by_name.get(m.linkname)
                if not target or not target.isfile() or not target.name.startswith('files/') or '..' in PurePosixPath(target.name).parts or PurePosixPath(target.name).is_absolute():raise ValueError('Unsafe sandbox hardlink target')
                expanded+=target.size
            elif m.isfile():expanded+=m.size
            else:raise ValueError('Unsafe sandbox output member')
        if expanded>64*1024**2:raise ValueError('Sandbox output expansion exceeds its ceiling')
        for m in members:
            target=folder/m.name
            if not target.resolve().is_relative_to(folder.resolve()):raise ValueError('Output escaped publication root')
            target.parent.mkdir(parents=True,exist_ok=True)
            with archive.extractfile(m) as src,target.open('wb') as dest:
                for chunk in iter(lambda:src.read(1024*1024),b''):dest.write(chunk)

def cache_digest(run):
    return hashlib.sha256((graph.graph_digest(json.loads(run['graph']))+os.getenv('NGS_DEMO_IMAGE','')+os.getenv('NGS_SAMPLE_MANIFEST_SHA','')).encode()).hexdigest()

def finish(row,state,used,error=None):
    with db.tx() as c:
        live=c.execute('SELECT state FROM runs WHERE id=?',(row['id'],)).fetchone()
        if live['state'] in TERMINAL:return
        c.execute('UPDATE runs SET state=?,finished=?,used=?,error=? WHERE id=?',(state,time.time(),used,error,row['id']))
        c.execute('UPDATE users SET reserved_seconds=MAX(0,reserved_seconds-?),used_seconds=used_seconds+? WHERE id=?',(row['reserved'],used,row['user_id']))
        entitlements.settle(c,row['id'],used)
        db.audit(c,row['user_id'],'run.'+state,row['id'],{'allocated_cpu_seconds':used,'provider':'Fargate','sample_only':True})
    from . import metering
    task=db.query('SELECT provider_id FROM demo_tasks WHERE run_id=?',(row['id'],),one=True)
    actual=None
    if task and task['provider_id']:
        try:
            job=aws_session().client('batch').describe_jobs(jobs=[task['provider_id']])['jobs'][0]
            usage=metering.fargate(job,2,4096)
            if usage:
                actual=usage['upper_pretax_usd']+.03
                with db.tx() as c:db.audit(c,'metering-service','usage.observed',row['id'],usage)
        except Exception:pass  # Preserve the full reservation when provider usage is unavailable.
    budget.reconcile(row['operation_id'],row['user_id'],'scientific',actual)
    db.event(row['id'],'run.state',state=state,error=error,used_cpu_seconds=used)

def drain_logs(row,task,job):
    stream=job.get('container',{}).get('logStreamName')
    if not stream:return
    client=aws_session().client('logs',config=Config(connect_timeout=3,read_timeout=5,retries={'total_max_attempts':1}))
    kwargs={'logGroupName':'/openngs/jobs','logStreamName':stream,'startFromHead':True,'limit':100}
    if task['log_cursor']:kwargs['nextToken']=task['log_cursor']
    try:page=client.get_log_events(**kwargs)
    except Exception:return
    for event in page.get('events',[]):
        try:p=json.loads(event['message'])
        except (ValueError,TypeError):continue
        nid=p.get('node');kind=p.get('kind')
        if kind=='node.started':db.event(row['id'],'task.state',nid,state='running',name=p.get('tool'))
        elif kind=='node.completed':
            db.event(row['id'],'task.state',nid,state='succeeded')
            db.event(row['id'],'task.metrics',nid,elapsed_seconds=p.get('seconds'),source='measured-worker')
        elif kind=='run.failed':db.event(row['id'],'log',nid,text=str(p.get('message','Sandbox failure'))[:1800],source='worker')
        else:continue
        if kind!='run.failed':db.event(row['id'],'log',nid,text=f"{kind}: {p.get('tool') or nid}",source='worker')
    token=page.get('nextForwardToken')
    if token:
        with db.tx() as c:c.execute('UPDATE demo_tasks SET log_cursor=? WHERE run_id=?',(token,row['id']))

def submit(row):
    config=runtime()
    if not config:return
    batch,s3=clients();bucket=os.environ['NGS_BUCKET'];kms=os.environ['NGS_KMS_KEY'];uid=row['user_id'];rid=row['id']
    prefix=f'tenants/{uid}/runs/{rid}/sandbox/';manifest_key=prefix+'manifest.json';output_key=prefix+'result.tar.gz'
    state=db.query('SELECT * FROM demo_tasks WHERE run_id=?',(rid,),one=True)
    if state:return
    output=s3.generate_presigned_post(bucket,output_key,Fields={'x-amz-server-side-encryption':'aws:kms','x-amz-server-side-encryption-aws-kms-key-id':kms},Conditions=[['content-length-range',1,64*1024**2],{'x-amz-server-side-encryption':'aws:kms'},{'x-amz-server-side-encryption-aws-kms-key-id':kms}],ExpiresIn=7200)
    g=json.loads(row['graph']);checked=graph.preflight(g,db.query('SELECT * FROM users WHERE id=?',(uid,),one=True))
    # The quota is already held by this admitted run. Structural ordering is stable.
    order=[];pending={n['id'] for n in g['nodes']}
    while pending:
        ready=sorted(n for n in pending if all(e['source'] in order for e in g['edges'] if e['target']==n))
        if not ready:raise ValueError('Invalid persisted DAG')
        order.extend(ready);pending.difference_update(ready)
    manifest={'run':rid,'graph':g,'order':order,'output':output}
    s3.put_object(Bucket=bucket,Key=manifest_key,Body=json.dumps(manifest).encode(),ServerSideEncryption='aws:kms',SSEKMSKeyId=kms)
    url=s3.generate_presigned_url('get_object',Params={'Bucket':bucket,'Key':manifest_key},ExpiresIn=7200)
    # Record intent before the non-idempotent provider call. Unknown outcomes are
    # reconciled by deterministic job name, never blindly submitted again.
    with db.tx() as c:c.execute("INSERT INTO demo_tasks VALUES(?,'submitting',NULL,?,?,?,?,NULL)",(rid,manifest_key,output_key,time.time(),time.time()))
    try:
        response=batch.submit_job(jobName='demo-'+rid,jobQueue=config[0],jobDefinition=config[1],containerOverrides={'command':[url]},retryStrategy={'attempts':1},tags={'Project':'OpenNGS','RunId':rid})
    except ClientError as error:
        if error.response.get('ResponseMetadata',{}).get('HTTPStatusCode',500)<500:
            code=error.response.get('Error',{}).get('Code','provider-denied')
            with db.tx() as c:
                c.execute("UPDATE demo_tasks SET state='dispatch-blocked',updated=? WHERE run_id=?",(time.time(),rid))
                db.audit(c,uid,'cloud.dispatch.denied',rid,{'provider_code':code,'operation':'SubmitJob'})
            db.event(rid,'run.state',state='queued',reason='Cloud dispatch configuration needs correction; accepted work is preserved',provider_code=code)
            return
        with db.tx() as c:c.execute("UPDATE demo_tasks SET state='reconciling',updated=? WHERE run_id=?",(time.time(),rid))
        return
    except Exception:
        with db.tx() as c:c.execute("UPDATE demo_tasks SET state='reconciling',updated=? WHERE run_id=?",(time.time(),rid))
        db.event(rid,'run.state',state='queued',reason='Confirming cloud submission; work and quota reservation are preserved')
        return
    with db.tx() as c:c.execute("UPDATE demo_tasks SET state='submitted',provider_id=?,updated=? WHERE run_id=?",(response['jobId'],time.time(),rid))
    db.event(rid,'run.state',state='queued',provider_id=response['jobId'])

def publish(row,task):
    if task['state']=='published':
        used=sum(max(1,int(json.loads(x['metrics']).get('seconds',0)*2+1)) for x in db.query('SELECT metrics FROM attempts WHERE run_id=?',(row['id'],)))
        finish(row,'succeeded',min(row['reserved'],used));return
    _,s3=clients();bucket=os.environ['NGS_BUCKET'];kms=os.environ['NGS_KMS_KEY'];uid=row['user_id'];rid=row['id']
    obj=s3.get_object(Bucket=bucket,Key=task['output_key'])
    if obj['ContentLength']>64*1024**2:raise ValueError('Sandbox archive exceeds its size ceiling')
    body=obj['Body'].read(64*1024**2+1);obj['Body'].close()
    if len(body)>64*1024**2:raise ValueError('Sandbox archive exceeds its size ceiling')
    folder=DATA/'published'/uid/rid;folder.mkdir(parents=True,exist_ok=True)
    extract_outputs(body,folder)
    result=json.loads((folder/'manifest.json').read_text())
    if result.get('schema')!=1 or result.get('run')!=rid:raise ValueError('Sandbox output identity does not match the accepted run')
    expected=json.loads(row['graph']).get('_execution',{}).get('sample_manifest')
    if expected and result.get('sample_manifest_sha256')!=expected:raise ValueError('Sample release differs from the frozen admitted manifest')
    files={};records=[];attempts={}
    def publish_file(relative):
        if relative in files:return files[relative]
        if not isinstance(relative,str) or not relative.startswith('files/'):raise ValueError('Invalid published file identity')
        p=(folder/relative).resolve()
        if not p.is_relative_to(folder.resolve()) or not p.is_file() or p.is_symlink():raise ValueError('Output file escaped the verified archive')
        digest=data.hash_file(p);key=f'tenants/{uid}/runs/{rid}/outputs/{relative}'
        try:
            uploaded=s3.head_object(Bucket=bucket,Key=key)
            if uploaded.get('Metadata',{}).get('sha256')!=digest:raise ValueError('Published object identity changed')
        except s3.exceptions.ClientError as error:
            if error.response['Error']['Code'] not in ['404','NoSuchKey','NotFound']:raise
            uploaded=s3.put_object(Bucket=bucket,Key=key,Body=p.read_bytes(),ServerSideEncryption='aws:kms',SSEKMSKeyId=kms,Metadata={'sha256':digest},IfNoneMatch='*')
        files[relative]={'uri':'s3://'+bucket+'/'+key,'sha256':digest,'version':uploaded.get('VersionId'),'bytes':p.stat().st_size}
        return files[relative]
    for nid,ports in result['outputs'].items():
        if nid not in {n['id'] for n in json.loads(row['graph'])['nodes']}:raise ValueError('Unknown sandbox node')
        attempts[nid]={}
        for port,items in ports.items():
            attempts[nid][port]=[]
            for i,item in enumerate(items):
                file=publish_file(item['path'])
                if item.get('digest','').removeprefix('sha256:')!=file['sha256']:raise ValueError('Scientific artifact digest failed verification')
                metadata=dict(item.get('metadata',{}));metadata['version_id']=file['version'];metadata['sample_only']=True
                for field in ['mate','index','reference']:
                    if isinstance(metadata.get(field),str) and metadata[field].startswith('files/'):
                        secondary=publish_file(metadata[field]);metadata[field]=secondary['uri'];metadata[field+'_sha256']=secondary['sha256']
                if metadata.get('index_paths'):metadata['index_paths']=[publish_file(x)['uri'] for x in metadata['index_paths']]
                aid='artifact_'+hashlib.sha256(f'{rid}:{nid}:{port}:{i}'.encode()).hexdigest()[:32]
                published={**item,'id':aid,'path':file['uri'],'bytes':file['bytes'],'metadata':metadata}
                records.append((aid,uid,rid,nid,item['name'],file['uri'],item['format'],file['bytes'],'sha256:'+file['sha256'],db.encode(metadata),time.time()))
                attempts[nid][port].append(published)
    metrics={x['node']:x for x in result.get('metrics',{}).get('nodes',[])}
    with db.tx() as c:
        if c.execute('SELECT state FROM runs WHERE id=?',(rid,)).fetchone()['state'] in TERMINAL:return
        for rec in records:
            c.execute('INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING',rec)
        for nid,outputs in attempts.items():
            aid='attempt_'+hashlib.sha256((rid+nid).encode()).hexdigest()[:32]
            c.execute("INSERT INTO attempts(id,run_id,node_id,state,started,finished,outputs,metrics,provider_id) VALUES(?,?,?,'succeeded',?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,finished=excluded.finished,outputs=excluded.outputs,metrics=excluded.metrics",(aid,rid,nid,row.get('started') or row['created'],time.time(),db.encode(outputs),db.encode(metrics.get(nid,{})),task['provider_id']))
        c.execute("UPDATE demo_tasks SET state='published',updated=? WHERE run_id=?",(time.time(),rid))
        db.audit(c,uid,'sandbox.outputs.verified',rid,{'artifacts':len(records),'image':os.getenv('NGS_DEMO_IMAGE'),'source_manifest':result.get('sample_manifest_sha256')})
        manifest={'state':'Ready','reason':'Cloud execution and output hashes verified','image':os.getenv('NGS_DEMO_IMAGE'),'manifest_sha256':result.get('sample_manifest_sha256'),'scientific_tool_versions':result.get('scientific_tool_versions'),'verified_run':rid}
        c.execute("INSERT INTO controls VALUES('reference-bundle:curated-samples-v1',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(db.encode(manifest),))
    for nid in attempts:db.event(rid,'task.state',nid,state='succeeded',artifacts=sum(len(x) for x in attempts[nid].values()))
    used=min(row['reserved'],sum(max(1,int(x.get('seconds',0)*2+1)) for x in metrics.values()))
    finish(row,'succeeded',used)

def tick():
    if not runtime():return
    # Exactly one orchestrator owns dispatch. The Fargate environment independently
    # limits all submitted work to two vCPUs even if a dispatcher is interrupted.
    row=db.query("SELECT r.* FROM runs r JOIN demo_tasks t ON t.run_id=r.id WHERE r.state NOT IN ('succeeded','failed','cancelled','interrupted') ORDER BY r.created LIMIT 1",one=True)
    batch,_=clients()
    if row:
        task=db.query('SELECT * FROM demo_tasks WHERE run_id=?',(row['id'],),one=True)
        if not task['provider_id']:
            found=batch.list_jobs(jobQueue=runtime()[0],filters=[{'name':'JOB_NAME','values':['demo-'+row['id']]}],maxResults=100)['jobSummaryList']
            if found:
                found.sort(key=lambda x:x['createdAt']);provider=found[0]['jobId']
                with db.tx() as c:c.execute("UPDATE demo_tasks SET provider_id=?,state='submitted' WHERE run_id=?",(provider,row['id']))
            return
        job=batch.describe_jobs(jobs=[task['provider_id']])['jobs'][0]
        drain_logs(row,task,job)
        if row['cancel_requested'] or db.query('SELECT frozen FROM users WHERE id=?',(row['user_id'],),one=True)['frozen']:
            if job['status'] not in ['SUCCEEDED','FAILED']:batch.terminate_job(jobId=task['provider_id'],reason='User cancellation or workspace freeze');return
            finish(row,'cancelled',min(row['reserved'],int((time.time()-(row['started'] or time.time()))*2)));return
        if job['status']=='SUCCEEDED':publish(row,task)
        elif job['status']=='FAILED':finish(row,'failed',min(row['reserved'],max(1,int((job.get('stoppedAt',0)-job.get('startedAt',0))/1000*2))),'Scientific sandbox failed; inspect the run diagnostics')
        elif job['status']=='RUNNING' and row['state']!='running':
            with db.tx() as c:c.execute("UPDATE runs SET state='running',started=? WHERE id=?",(time.time(),row['id']))
            db.event(row['id'],'run.state',state='running',provider_id=task['provider_id'])
        return
    # Promote affordable waiting work without rejecting or discarding it.
    for waiting in db.query("SELECT * FROM runs WHERE state='waiting-budget' ORDER BY created LIMIT 50"):
        try:
            with db.tx() as c:
                estimate=budget.graph_estimate(json.loads(waiting['graph']),registry.definitions(waiting['user_id']),waiting['user_id'])
                budget.reserve(c,waiting['user_id'],waiting['operation_id'],'scientific',estimate['pretax_usd'],estimate)
                c.execute("UPDATE runs SET state='queued' WHERE id=?",(waiting['id'],))
        except budget.BudgetFull:continue
    candidates=db.query("SELECT r.* FROM runs r WHERE r.state IN ('queued','cancelling') ORDER BY COALESCE((SELECT MAX(x.started) FROM runs x WHERE x.user_id=r.user_id),0),r.created LIMIT 100")
    row=next((r for r in candidates if json.loads(r['graph']).get('_execution',{}).get('access','explore')=='explore'),None)
    if row:
        if row['cancel_requested']:finish(row,'cancelled',0)
        else:submit(row)

def main():
    initialize()
    while True:
        try:tick()
        except Exception as e:
            # Retain work and reservations on operational failures. Do not log
            # provider URLs, encrypted payloads, DB errors or credential values.
            print(db.encode({'at':time.time(),'severity':'error','event':'scheduler.retry','exception':type(e).__name__}),flush=True)
        time.sleep(3)

initialize()
if __name__=='__main__':main()
