import concurrent.futures
import glob
import json
import math
import os
import selectors
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
import psutil
from . import compiler,data,db,graph,registry,security,cache,budget,entitlements,asset_rights
from .config import ROOT,DATA,TERMINAL,CLOUD,DEMO
from . import retention
from .migration import source_manifest

POOL=concurrent.futures.ThreadPoolExecutor(max_workers=2,thread_name_prefix='execution')
_CAPS=None;_CAP_TIME=0

def capabilities():
    global _CAPS,_CAP_TIME
    if _CAPS is not None and time.time()-_CAP_TIME<15:return _CAPS
    docker=False
    if shutil.which('docker'):
        try:docker=subprocess.run(['docker','info','--format','{{.ServerVersion}}'],capture_output=True,timeout=3).returncode==0
        except (OSError,subprocess.TimeoutExpired):pass
    _CAPS={'portable':True,'docker':docker,'aws_region':'us-east-1','local_security_profile':'Authenticated desktop research; not a hostile multi-tenant production sandbox','cloud_profiles':'Explicitly published AWS Batch worker contracts','python':sys.version.split()[0]};_CAP_TIME=time.time()
    return _CAPS

def admit(user,g,operation_id):
    graph.validate_shape(g)
    prior=db.query('SELECT * FROM runs WHERE user_id=? AND operation_id=?',(user['id'],operation_id),one=True)
    if prior:
        if graph.graph_digest(json.loads(prior['graph']))!=graph.graph_digest(g):raise ValueError('Idempotency key reused for a different graph')
        return prior,False
    checked=graph.preflight(g,user,capabilities())
    if not checked['ok']:raise ValueError(json.dumps({'code':'PREFLIGHT_FAILED',**checked}))
    with db.tx() as c:
        existing=c.execute('SELECT * FROM runs WHERE user_id=? AND operation_id=?',(user['id'],operation_id)).fetchone()
        if existing:
            if graph.graph_digest(json.loads(existing['graph']))!=checked['graph_digest']:raise ValueError('Idempotency key reused for a different graph')
            return dict(existing),False
        live=c.execute('SELECT * FROM users WHERE id=?',(user['id'],)).fetchone()
        frozen=c.execute("SELECT value FROM controls WHERE key='global_freeze'").fetchone()
        if live['frozen'] or (frozen and frozen['value']=='true'):raise ValueError('Execution admission is frozen')
        if not CLOUD and live['used_seconds']+live['reserved_seconds']+checked['reservation_cpu_seconds']>live['quota_seconds']:raise ValueError('Concurrent admission exhausted the CPU-time quota')
        active=c.execute("SELECT COUNT(*) FROM runs WHERE user_id=? AND state IN ('queued','running','cancelling')",(user['id'],)).fetchone()[0]
        if not DEMO and active>=data.settings(user['id'])['max_concurrency']:raise ValueError('Concurrent run limit reached')
        admitted_state='queued'
        if CLOUD:
            pending=c.execute("SELECT COUNT(*) FROM runs WHERE state IN ('queued','running','cancelling')").fetchone()[0]
            if pending>=100:raise budget.BudgetFull('The shared admitted-work queue is full. Your saved workflow is preserved; retry after capacity becomes available.')
            if not DEMO and pending>=8:raise budget.BudgetFull('Scientific queue is full. Your graph is saved; retry after an existing run finishes.')
            estimate=budget.graph_estimate(g,registry.definitions(user['id']),user['id'])
            budget.reserve(c,user['id'],operation_id,'scientific',estimate['pretax_usd'],estimate)
        rid=db.ident('run_')
        entitlements.hold(c,user,rid,checked['reservation_cpu_seconds'])
        frozen_graph=json.loads(db.encode(g));s=data.settings(user['id'])
        all_specs=registry.definitions(user['id']);needed={n['tool'] for n in g['nodes']}|{n.get('params',{}).get('published_tool') for n in g['nodes']}
        snapshot={'images':s['image_approvals'],'backend':s['backend'],'batch_queue':s['batch_queue'],'batch_job_definition':s['batch_job_definition'],'catalog':{k:v for k,v in all_specs.items() if k in needed}}
        snapshot['access']=entitlements.limits(user['id'])['access']
        snapshot['assets']={}
        if CLOUD:
            for node in g['nodes']:
                spec=all_specs[node['tool']]
                if node['tool'] in ['asset.file','reference.asset','database.asset'] or spec.get('asset_family'):
                    asset_id=node.get('params',{}).get('asset_id')
                    snapshot['assets'][asset_id]=input_asset(user['id'],asset_id)
                    retention.ensure_available(user['id'],asset_id,c)
                    retention.ensure_outputs(user['id'],{'source':[snapshot['assets'][asset_id]]},c)
        snapshot['migration_sources']=list({(entry['Bucket'],entry['Key']):entry for item in snapshot['assets'].values() for entry in source_manifest(user['id'],item)}.values())
        snapshot['allowance']=entitlements.limits(user['id'])
        if CLOUD:snapshot['allowance']['max_output_bytes']=entitlements.reserve_output(c,user,rid,g.get('maximum_output_bytes'))
        snapshot['cloud_settings']={k:s[k] for k in ['s3_bucket','kms_key_arn','batch_queue','batch_job_definition','max_upload_gib']}
        snapshot['runtime_profiles']=s.get('research_profile',{}).get('runtime_profiles',{})
        from . import vm
        snapshot['vm_configuration']=vm.configuration();snapshot['capacity']=s.get('research_profile',{})
        if entitlements.restricted(user['id']):snapshot.update(demo_image=os.getenv('NGS_DEMO_IMAGE',''),sample_manifest=os.getenv('NGS_SAMPLE_MANIFEST_SHA',''))
        frozen_graph['_execution']=snapshot
        frozen_graph['_execution_digest']='sha256:'+__import__('hashlib').sha256((checked['graph_digest']+db.encode(snapshot)).encode()).hexdigest()
        c.execute('INSERT INTO runs(id,user_id,operation_id,state,graph,created,reserved) VALUES(?,?,?,?,?,?,?)',(rid,user['id'],operation_id,admitted_state,db.encode(frozen_graph),time.time(),checked['reservation_cpu_seconds']))
        c.execute('UPDATE users SET reserved_seconds=reserved_seconds+? WHERE id=?',(checked['reservation_cpu_seconds'],user['id']))
        db.audit(c,user['id'],'run.admitted',rid,{'graph_digest':checked['graph_digest'],'cpu_seconds_reserved':checked['reservation_cpu_seconds']})
    db.event(rid,'run.state',state=admitted_state,graph_digest=checked['graph_digest'])
    if not CLOUD:POOL.submit(run,rid,checked['order'])
    return db.query('SELECT * FROM runs WHERE id=?',(rid,),one=True),True

def cancelled(rid):
    r=db.query('SELECT r.cancel_requested,u.frozen FROM runs r JOIN users u ON r.user_id=u.id WHERE r.id=?',(rid,),one=True)
    return not r or bool(r['cancel_requested'] or r['frozen'])

def stop(rid,actor):
    with db.tx() as c:
        r=c.execute('SELECT state FROM runs WHERE id=?',(rid,)).fetchone()
        if not r:raise ValueError('Run not found')
        if r['state'] not in TERMINAL:c.execute("UPDATE runs SET cancel_requested=1,state='cancelling' WHERE id=?",(rid,))
        db.audit(c,actor,'run.cancel.requested',rid)
    db.event(rid,'run.state',state='cancelling')

def terminate(proc):
    if proc.poll() is not None:return
    try:os.killpg(proc.pid,signal.SIGTERM)
    except ProcessLookupError:return
    try:proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:os.killpg(proc.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        proc.wait(timeout=3)

def run_process(argv,rid,nid,aid,work,timeout,stdout_file=None,container_name=None,resources=None,input_secret=None):
    env={k:os.environ[k] for k in ['PATH','HOME','TMPDIR','SSL_CERT_FILE'] if k in os.environ}
    env.update(PYTHONUNBUFFERED='1',PYTHONPATH=str(ROOT),OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1',NGS_ATTEMPT_ID=aid)
    handle=open(stdout_file,'wb') if stdout_file else None
    proc=subprocess.Popen(argv,cwd=work,env=env,stdin=subprocess.PIPE if input_secret is not None else subprocess.DEVNULL,stdout=handle or subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True,bufsize=0)
    if input_secret is not None:
        try:proc.stdin.write(input_secret);proc.stdin.close()
        except BrokenPipeError:pass
    with db.tx() as c:c.execute('UPDATE attempts SET pid=? WHERE id=?',(proc.pid,aid))
    selector=selectors.DefaultSelector()
    for stream in ([proc.stderr] if handle else [proc.stdout,proc.stderr]):
        os.set_blocking(stream.fileno(),False);selector.register(stream,selectors.EVENT_READ)
    started=time.monotonic();last=0;log_bytes=0;pending=b''
    try:
        while proc.poll() is None or selector.get_map():
            if cancelled(rid):raise InterruptedError('Cancellation requested')
            if time.monotonic()-started>timeout:raise TimeoutError(f'Node exceeded {timeout}s wall-time limit')
            for key,_ in selector.select(timeout=.15):
                block=os.read(key.fileobj.fileno(),4096)
                if not block:selector.unregister(key.fileobj);continue
                if log_bytes<128*1024 and input_secret is None:
                    log_bytes+=len(block);pending+=block
                    while b'\n' in pending:
                        line,pending=pending.split(b'\n',1)
                        db.event(rid,'log',nid,text=line[:2000].decode('utf8','replace').replace('\x1b',''),source='tool')
            if time.monotonic()-last>=1:
                last=time.monotonic();rss=cpu=0
                try:
                    parent=psutil.Process(proc.pid)
                    for child in [parent,*parent.children(recursive=True)]:
                        try:rss+=child.memory_info().rss;cpu+=sum(child.cpu_times()[:2])
                        except psutil.Error:pass
                except psutil.Error:pass
                if resources and rss>resources.get('memory_mb',1024)*1024**2:raise MemoryError('Process memory watermark exceeded')
                if resources:
                    scratch=files=0
                    for parent,dirs,names in os.walk(work):
                        for name in names:
                            p=Path(parent)/name
                            if p.is_symlink():continue
                            scratch+=p.stat().st_size;files+=1
                            if scratch>resources.get('scratch_mb',2048)*1024**2 or files>10000:raise OSError('Scratch size or file-count watermark exceeded')
                metrics={'rss_bytes':rss,'process_cpu_seconds':round(cpu,3),'elapsed_seconds':round(time.monotonic()-started,2),'source':'host-process' if not container_name else 'docker-client'}
                if container_name:
                    try:
                        s=subprocess.run(['docker','stats','--no-stream','--format','{{json .}}',container_name],capture_output=True,text=True,timeout=2)
                        if s.returncode==0:metrics['container']=json.loads(s.stdout)
                    except (OSError,ValueError,subprocess.TimeoutExpired):pass
                with db.tx() as c:c.execute('UPDATE attempts SET metrics=? WHERE id=?',(db.encode(metrics),aid))
                db.event(rid,'task.metrics',nid,**metrics)
        if pending and log_bytes<128*1024 and input_secret is None:db.event(rid,'log',nid,text=pending[:2000].decode('utf8','replace'),source='tool')
        code=proc.wait()
        if code:raise RuntimeError(f'Tool exited with code {code}; inspect this node’s diagnostic log')
    finally:
        if proc.poll() is None:terminate(proc)
        if container_name:
            subprocess.run(['docker','rm','-f',container_name],capture_output=True,timeout=10)
        selector.close()
        for stream in [proc.stdout,proc.stderr]:
            if stream:stream.close()
        if handle:handle.close()
        with db.tx() as c:c.execute('UPDATE attempts SET pid=NULL WHERE id=?',(aid,))

def input_asset(uid,asset_id,expected=None):
    from .asset_rights import attach as attach_data_rights
    # Admission calls this inside its existing transaction. The atomic retention
    # fence is checked by admit; runtime reads also check the frozen output URI.
    row=db.query('SELECT * FROM assets WHERE id=? AND user_id=?',(asset_id,uid),one=True)
    if row:
        from .migration import resolve_asset
        row=resolve_asset(uid,row)
        row=attach_data_rights(uid,row);row['metadata']=db.encode(row['metadata'])
        if row['path'].startswith('s3://'):
            meta=json.loads(row['metadata']);ready=db.query("SELECT state FROM datasets WHERE id=? AND user_id=?",(meta.get('dataset_id',asset_id),uid),one=True)
            if not ready or ready['state']!='Ready' or not meta.get('sha256'):raise ValueError('Cloud dataset is not verified and Ready')
            bucket,key=row['path'][5:].split('/',1)
            from .migration import authorize_source
            migrated=authorize_source(uid,row['path'],meta.get('version_id'),meta['sha256'])
            if not migrated and (bucket!=data.settings(uid)['s3_bucket'] or not key.startswith('tenants/'+uid+'/')):raise ValueError('Cloud dataset escaped its tenant storage')
            return {'path':row['path'],'name':row['name'],'format':row['format'],'bytes':row['bytes'],'digest':'sha256:'+meta['sha256'],'metadata':meta}
        p=data.permitted_path(uid,row['path']);meta=json.loads(row['metadata']);meta['assembly']=row['assembly']
        return {'path':str(p),'name':row['name'],'format':row['format'],'bytes':p.stat().st_size,'metadata':meta}
    row=db.query('SELECT * FROM artifacts WHERE id=? AND user_id=?',(asset_id,uid),one=True)
    if row:return attach_data_rights(uid,row)
    raise ValueError('Asset binding no longer exists or belongs to another workspace')

def register_artifacts(uid,rid,nid,outputs,work):
    result={}
    for port,items in outputs.items():
        result[port]=[]
        for item in items:
            if item['path'].startswith('s3://'):
                # Cloud worker manifests are admitted only from the expected
                # run-prefix by the cloud adapter before reaching this layer.
                pass
            else:
                resolved=Path(item['path']).resolve(strict=True)
                if not resolved.is_relative_to(DATA):raise ValueError('Output path escaped the application-managed working directory')
                if not resolved.is_file():raise ValueError('Output must be a regular file')
                item['path']=str(resolved)
            aid=db.ident('artifact_');meta=item.get('metadata',{})
            with db.tx() as c:
                c.execute('INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?,?,?,?)',(aid,uid,rid,nid,item['name'],item['path'],item['format'],item['bytes'],item.get('digest'),db.encode(meta),time.time()))
                db.audit(c,uid,'artifact.published',aid,{'run':rid,'node':nid,'format':item['format'],'bytes':item['bytes'],'digest':item.get('digest')})
            result[port].append({**item,'id':aid})
    return result

def native_docker(spec,node,inputs,work,rid,aid):
    s=data.settings(db.query('SELECT user_id FROM runs WHERE id=?',(rid,),one=True)['user_id'])
    image=node.get('_approved_image')
    if not image or '@sha256:' not in image:raise ValueError('Approved immutable OCI image required')
    plan=compiler.compile_tool(spec,node,inputs,work)
    budget=node.get('resources',{});started=time.monotonic()
    # Pull has an independent bounded timeout and is visible in diagnostics.
    db.event(rid,'log',node['id'],text='Resolving immutable container image '+image)
    pull=subprocess.run(['docker','pull',image],capture_output=True,text=True,timeout=min(120,budget.get('timeout_seconds',120)))
    if pull.returncode:raise ValueError('Container pull failed: '+pull.stderr[-500:])
    for i,step in enumerate(plan['steps']):
        remaining=budget.get('timeout_seconds',120)-(time.monotonic()-started)
        if remaining<=0:raise TimeoutError('Container initialization exhausted the node time budget')
        name='ngs-'+aid[-16:]+'-'+str(i)
        argv=['docker','run','--rm','--name',name,'--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--pids-limit','256','--cpus',str(budget.get('threads',2)),'--memory',str(budget.get('memory_mb',4096))+'m','--memory-swap',str(budget.get('memory_mb',4096))+'m','--user',str(os.getuid())+':'+str(os.getgid()),'--tmpfs','/tmp:rw,nosuid,nodev,size=256m','--mount',f'type=bind,source={work},target=/work','--workdir','/work']
        for source,target in plan['bindings']:argv+=['--mount',f'type=bind,source={source},target={target},readonly']
        secret=None
        if spec.get('secret_delivery'):
            provider=spec['secret_delivery']['provider'];binding=node.get('_credential_node_id',spec['id']);row=security.credential_for(db.query('SELECT user_id FROM runs WHERE id=?',(rid,),one=True)['user_id'],provider,binding)
            if not row:raise ValueError('No active licence credential bound to this execution node')
            secret=security.unwrap(row['user_id'],row['id'],binding).encode()
            argv+=['--tmpfs','/run/ngs-secrets:rw,nosuid,nodev,noexec,size=1m','-i','--entrypoint','/bin/sh',image,'-c','umask 077; cat > /run/ngs-secrets/license; chmod 400 /run/ngs-secrets/license; exec "$@"','ngs-license-wrapper',*step['argv']]
            db.event(rid,'log',node['id'],text='Scoped licence file injected through stdin into container tmpfs. Raw tool diagnostics are withheld for this profile.')
        else:argv+=['--entrypoint',step['argv'][0],image,*step['argv'][1:]]
        db.event(rid,'log',node['id'],text='Executing '+Path(step['argv'][0]).name+' · step '+str(i+1)+'/'+str(len(plan['steps'])))
        run_process(argv,rid,node['id'],aid,work,remaining,work/step['stdout'] if step['stdout'] else None,name,input_secret=secret)
        secret=None
    from .portable import artifact
    outputs={}
    for port,items in plan['outputs'].items():
        outputs[port]=[]
        for desc in items:
            files=sorted(work.glob(desc['file']))
            if not files:raise ValueError('Tool did not produce required output: '+desc['file'])
            for p in files:
                if not p.resolve().is_relative_to(work.resolve()) or p.is_symlink():raise ValueError('Unsafe output symlink')
                outputs[port].append(artifact(p,desc['format'],**desc.get('metadata',{})))
    return outputs

def run(rid,order):
    row=db.query('SELECT * FROM runs WHERE id=?',(rid,),one=True);uid=row['user_id'];g=json.loads(row['graph']);nodes={n['id']:n for n in g['nodes']};snapshot=g.get('_execution',{});specs=snapshot.get('catalog') or registry.definitions(uid)
    completed={};used=0;state='succeeded';error=None;began=time.time()
    with db.tx() as c:c.execute("UPDATE runs SET state='running',started=COALESCE(started,?) WHERE id=?",(began,rid))
    db.event(rid,'run.state',state='running')
    try:
        for nid in order:
            if cancelled(rid):raise InterruptedError('Run cancelled')
            allowance=snapshot.get('allowance',{})
            if allowance.get('max_job_seconds') and time.time()-(row.get('started') or began)>allowance['max_job_seconds']:raise TimeoutError('Accepted job wall-time allowance exhausted; completed outputs are retained')
            n=nodes[nid];spec=specs[n['tool']];aid=db.ident('attempt_');started=time.monotonic();work=DATA/'runs'/uid/rid/nid;work.mkdir(parents=True,exist_ok=True)
            prior=db.query('SELECT * FROM attempts WHERE run_id=? AND node_id=? ORDER BY started DESC LIMIT 1',(rid,nid),one=True) if CLOUD else None
            if prior and prior['state']=='succeeded':
                outputs=json.loads(prior['outputs'])
                from .asset_rights import require_inputs
                require_inputs(uid,outputs)
                for items in outputs.values():
                    for item in items:
                        if cache.file_identity(uid,item['path'],snapshot.get('cloud_settings'))['digest']!=item.get('digest','').removeprefix('sha256:'):raise ValueError('Committed upstream output identity changed; resume refused')
                completed[nid]=outputs;used+=int(json.loads(prior['metrics']).get('allocated_cpu_seconds',0));continue
            if prior and (prior.get('provider_id') or json.loads(prior['metrics']).get('submission_intent')) and prior['state'] in ['running','interrupted','failed']:aid=prior['id']
            inputs={}
            for e in g['edges']:
                if e['target']==nid:inputs.setdefault(e['targetPort'],[]).extend(completed.get(e['source'],{}).get(e['sourcePort'],[]))
            absent=[p['key'] for p in spec['inputs'] if p['required'] and not inputs.get(p['key'])]
            if absent:
                completed[nid]={};db.event(rid,'task.state',nid,state='skipped',reason='Upstream conditional branch emitted no artifact');continue
            params={**registry.defaults(spec),**n.get('params',{})};resources={**spec['resources'],**n.get('resources',{})};n={**n,'params':params,'resources':resources}
            if params.get('published_tool') in specs and specs[params['published_tool']].get('approved'):spec=specs[params['published_tool']]
            n['_approved_image']=snapshot.get('images',{}).get(spec['id']) or (spec.get('image') if spec.get('approved') else None)
            n['_batch_queue']=snapshot.get('batch_queue');n['_batch_job_definition']=snapshot.get('batch_job_definition')
            runtime_profile=snapshot.get('runtime_profiles',{}).get(spec['id'])
            if CLOUD and (n.get('execution_profile')=='exclusive-vm' or resources.get('scratch_mb',2048)>184320):
                from . import vm
                runtime_profile=vm.runtime_route(snapshot.get('capacity',{}),runtime_profile,bool(snapshot.get('migration_sources')),snapshot.get('vm_configuration',{}))
            if runtime_profile:
                n['_approved_image']=runtime_profile['image']
                n['_batch_job_definition']=runtime_profile['definition']
            n['_cloud_settings']=snapshot.get('cloud_settings')
            n['_migration_sources']=snapshot.get('migration_sources',[])
            n['_source_grants_version']=(runtime_profile or {}).get('source_grants_version')
            n['_capacity']=snapshot.get('capacity',{});n['_vm_configuration']=snapshot.get('vm_configuration',{})
            n['_max_output_bytes']=allowance.get('max_output_bytes',64*1024**2)
            if CLOUD:
                committed={}
                for ports in completed.values():
                    for items in ports.values():
                        for item in items:
                            for obj in [{'uri':item['path'],'bytes':item['bytes']}]+item.get('metadata',{}).get('secondary_objects',[]):
                                if obj['uri'].startswith('s3://') and '/runs/'+rid+'/' in obj['uri']:committed[obj['uri']]=obj['bytes']
                already=sum(committed.values())
                n['_max_output_bytes']=max(0,n['_max_output_bytes']-already)
            n['_credential_node_id']=n['tool']
            with db.tx() as c:c.execute("INSERT INTO attempts(id,run_id,node_id,state,started) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET state='running',error=NULL",(aid,rid,nid,'running',time.time()))
            db.event(rid,'task.state',nid,state='running',name=spec['name'])
            reused=False
            try:
                if spec.get('quarantined') or db.query('SELECT * FROM quarantine WHERE tool_id=?',(spec['id'],),one=True):raise ValueError('Node release was quarantined')
                live_settings=data.settings(uid)
                original=specs[n['tool']]
                if original['license'] in ['Academic','Commercial BYOL','Review required'] and original['id'] not in live_settings['rights_approved']:raise ValueError('Node rights approval was revoked')
                from .asset_rights import require_inputs,attach as attach_data_rights
                data_obligations=require_inputs(uid,inputs)
                source_item=None
                if n['tool'] in ['asset.file','reference.asset','database.asset'] or spec.get('asset_family'):
                    source_item=json.loads(db.encode(snapshot['assets'][params['asset_id']])) if params.get('asset_id') in snapshot.get('assets',{}) else input_asset(uid,params['asset_id'])
                    source_item=attach_data_rights(uid,source_item)
                    data_obligations=source_item['metadata']['data_rights']
                from .semantics import runtime as check_runtime
                check_runtime(spec,inputs)
                cache_key=cache.key(uid,spec,n,inputs,snapshot.get('backend'),cloud_settings=snapshot.get('cloud_settings'))
                cached=cache.load(uid,cache_key,snapshot.get('cloud_settings')) if not g.get('disable_cache') else None
                if cached is not None:
                    outputs=cached;reused=True
                    db.event(rid,'log',nid,text='Resume: verified content-addressed upstream outputs reused; no scientific tool launched.')
                elif n['tool'] in ['utility.export','utility.gather','utility.condition']:
                    # These operations route authenticated artifact records.
                    # They parse no genomic bytes and need no scientific worker.
                    from .portable import execute as route_metadata
                    outputs=route_metadata({'tool':n['tool'],'params':params,'inputs':inputs,'workdir':str(work)})
                    db.event(rid,'log',nid,text='Verified artifact references published without copying or recomputing scientific data.')
                elif spec.get('api'):
                    from .onboarding import execute_api
                    outputs=execute_api(uid,spec,n,inputs,work,rid)
                elif n['tool'] in ['asset.file','reference.asset','database.asset'] or spec.get('asset_family'):
                    item=source_item;key=spec['outputs'][0]['key']
                    if params.get('assembly') and not item.get('metadata',{}).get('assembly'):item.setdefault('metadata',{})['assembly']=params['assembly']
                    if spec['outputs'][0]['type']=='DATABASE':item={**item,'format':'DATABASE','metadata':{**item.get('metadata',{}),'underlying_format':item['format']}}
                    outputs={key:[item]}
                elif snapshot.get('backend')=='aws-batch' and spec['builtin']:
                    from .cloud import execute_batch
                    outputs=execute_batch(uid,rid,aid,spec,n,inputs,work,cancelled)
                elif spec['builtin']:
                    payload={'tool':n['tool'],'params':params,'inputs':inputs,'workdir':str(work),'threads':resources['threads'],'memory_mb':resources['memory_mb'],'max_file_bytes':resources.get('scratch_mb',2048)*1024**2}
                    jobfile=work/'job.json';jobfile.write_text(db.encode(payload))
                    run_process([sys.executable,'-m','backend.portable',str(jobfile)],rid,nid,aid,work,resources['timeout_seconds'],resources=resources)
                    manifest=work/'result.json'
                    if manifest.stat().st_size>8*1024*1024:raise ValueError('Output manifest is too large')
                    outputs=json.loads(manifest.read_text())
                elif n['tool']=='oncokb':
                    from .providers import oncokb
                    outputs=oncokb(uid,n,inputs,work,rid)
                elif snapshot.get('backend')=='aws-batch':
                    from .cloud import execute_batch
                    outputs=execute_batch(uid,rid,aid,spec,n,inputs,work,cancelled)
                else:outputs=native_docker(spec,n,inputs,work,rid,aid)
                for items in outputs.values():
                    for item in items:
                        item.setdefault('metadata',{})['data_rights']=data_obligations
                        for field in ['assembly','contig_style','reference_digest']:
                            values={i.get('metadata',{}).get(field) for rows in inputs.values() for i in rows}-{None}
                            if len(values)==1:item.setdefault('metadata',{}).setdefault(field,next(iter(values)))
                if not reused:cache.store(uid,cache_key,outputs,snapshot.get('cloud_settings'))
                completed[nid]=register_artifacts(uid,rid,nid,outputs,work)
                with db.tx() as c:c.execute("UPDATE attempts SET state='succeeded',finished=?,outputs=? WHERE id=?",(time.time(),db.encode(completed[nid]),aid))
                db.event(rid,'task.state',nid,state='succeeded',artifacts=sum(len(x) for x in completed[nid].values()),cached=reused,cache_key=cache_key)
                if reused:
                    with db.tx() as c:c.execute('UPDATE attempts SET metrics=? WHERE id=?',(db.encode({'cached':True,'cache_key':cache_key,'elapsed_seconds':round(time.monotonic()-started,3)}),aid))
            except BaseException as e:
                status='cancelled' if isinstance(e,InterruptedError) else 'interrupted' if type(e).__name__=='UncertainCloudState' else 'failed'
                with db.tx() as c:c.execute('UPDATE attempts SET state=?,finished=?,error=? WHERE id=?',(status,time.time(),str(e)[:1000],aid))
                db.event(rid,'task.state',nid,state=status,error=str(e)[:1000]);raise
            finally:
                allocation=0 if reused else min(resources['threads']*resources['timeout_seconds'],math.ceil((time.monotonic()-started)*resources['threads']))
                if CLOUD and (n.get('execution_profile')=='exclusive-vm' or resources.get('scratch_mb',2048)>184320):
                    from .vm import select
                    machine=select(resources);allocation=min(machine['vcpus']*(resources['timeout_seconds']+900),math.ceil((time.monotonic()-started)*machine['vcpus'])) if not reused else 0
                used+=allocation
                with db.tx() as c:
                    previous=c.execute('SELECT metrics FROM attempts WHERE id=?',(aid,)).fetchone()
                    if previous:
                        metrics=json.loads(previous['metrics']);metrics['allocated_cpu_seconds']=allocation;c.execute('UPDATE attempts SET metrics=? WHERE id=?',(db.encode(metrics),aid))
    except InterruptedError as e:state='cancelled';error=str(e)
    except BaseException as e:state='interrupted' if type(e).__name__=='UncertainCloudState' else 'failed';error=str(e)[:1000]
    finally:
        try:
            from .workbench import run_summary
            from .portable import report
            summary_folder=DATA/'runs'/uid/rid/'summary';summary_folder.mkdir(parents=True,exist_ok=True)
            summary=run_summary(uid,rid);summary['state']=state;summary['allocated_cpu_seconds']=used
            register_artifacts(uid,rid,'summary',{'report':[report(summary_folder,'OpenNGS-run-quality-summary.json',summary)]},summary_folder)
        except Exception as summary_error:
            db.event(rid,'log',text='Run summary could not be published: '+str(summary_error)[:300])
        with db.tx() as c:
            c.execute('UPDATE runs SET state=?,finished=?,used=?,error=? WHERE id=?',(state,time.time(),used,error,rid))
            if state!='interrupted':c.execute('UPDATE users SET reserved_seconds=MAX(0,reserved_seconds-?),used_seconds=used_seconds+? WHERE id=?',(row['reserved'],used,uid))
            if state!='interrupted':entitlements.settle(c,rid,used)
            db.audit(c,uid,'run.'+state,rid,{'allocated_cpu_seconds':used,'elapsed_seconds':round(time.time()-began,3)})
        db.event(rid,'run.state',state=state,error=error,used_cpu_seconds=used)
        if CLOUD and state!='interrupted':
            from . import metering
            budget.reconcile(row['operation_id'],uid,'scientific',metering.scientific_cost(rid))
