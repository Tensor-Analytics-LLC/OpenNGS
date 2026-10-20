import asyncio
import collections
import hashlib
import json
import re
import secrets
import shlex
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
import httpx
from fastapi import FastAPI,Request,Response,HTTPException,Depends,Body
from fastapi.responses import FileResponse,StreamingResponse,RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware
from . import compiler,data,db,graph,registry,runner,security,budget,mfa,demo,entitlements
from .config import DATA,FRONTEND,ROOT,TERMINAL,TRIAL_SECONDS,aws_session,CLOUD,ALLOWED_HOSTS,SSD

@asynccontextmanager
async def lifespan(app):
    if not CLOUD:db.recover()
    try:yield
    finally:
        if CLOUD:
            from .postgres import close
            close()

app=FastAPI(title='OpenNGS by Tensor Analytics',version='1.0.0',lifespan=lifespan,docs_url='/api/docs',openapi_url='/api/openapi.json')
app.add_middleware(TrustedHostMiddleware,allowed_hosts=ALLOWED_HOSTS)
from .admission import Admission
AUTH_ATTEMPTS=collections.OrderedDict()

@app.middleware('http')
async def boundary(request,call_next):
    if CLOUD:
        expected=__import__('os').environ.get('NGS_ORIGIN_TOKEN','')
        if not expected or not secrets.compare_digest(request.headers.get('x-ngs-origin',''),expected):return Response('Origin access denied',403)
        request.scope['scheme']='https'
    if demo.DEMO and request.method in ['POST','PATCH','PUT','DELETE']:
        blocked=request.url.path=='/api/uploads' or request.url.path.startswith(('/api/operations/transfers','/api/assets/scan','/api/public/','/api/onboarding/','/api/tools','/api/cloud/')) or request.url.path.endswith('/sync')
        if blocked:
            current=await run_in_threadpool(security.session_for,request.cookies.get('ngs_session'))
            limited=not current or await run_in_threadpool(entitlements.restricted,current['id'])
            # Filesystem scans and the legacy in-memory upload route remain local-only.
            local_only=request.url.path=='/api/uploads' or request.url.path.startswith('/api/assets/scan') or request.url.path.endswith('/sync')
            if limited or local_only:return __import__('fastapi.responses',fromlist=['JSONResponse']).JSONResponse({'detail':'Explore accepts curated samples only. Funded Research Pro uses the durable upload manager and reviewed cloud capabilities.'},status_code=403)
    if request.method in ['POST','PUT','PATCH','DELETE']:
        origin=request.headers.get('origin')
        expected=str(request.base_url).rstrip('/')
        if origin!=expected:return Response('Origin verification failed',403)
        if request.headers.get('x-ngs-client')!='studio':return Response('Application request header required',403)
        limit=64*1024*1024 if request.url.path=='/api/uploads' else 2*1024*1024
        try:length=int(request.headers.get('content-length','0'))
        except ValueError:return Response('Invalid content length',400)
        if length>limit:return Response('Request exceeds its size limit',413)
        if request.url.path!='/api/uploads':
            pieces=[];received=0
            async for piece in request.stream():
                received+=len(piece)
                if received>limit:return Response('Request exceeds its size limit',413)
                pieces.append(piece)
            request._body=b''.join(pieces)
    response=await call_next(request)
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Referrer-Policy']='no-referrer'
    response.headers['Cache-Control']='no-store' if request.url.path.startswith('/api') else 'no-cache'
    bucket=__import__('os').environ.get('NGS_BUCKET','')
    if 'text/html' in response.headers.get('content-type','') and request.cookies.get('ngs_session'):
        current=await run_in_threadpool(security.session_for,request.cookies.get('ngs_session'))
        if current:
            profile=await run_in_threadpool(data.settings,current['id']);bucket=profile.get('s3_bucket') or bucket
    storage_origins=(' https://'+bucket+'.s3.us-east-1.amazonaws.com https://'+bucket+'.s3.dualstack.us-east-1.amazonaws.com') if re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]',bucket) else ''
    response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'"+storage_origins+"; worker-src 'self'; object-src 'none'; frame-ancestors 'self'; base-uri 'none'"
    return response

@app.exception_handler(ValueError)
async def validation_error(request,exc):
    if isinstance(exc,budget.BudgetFull):
        from fastapi.responses import JSONResponse
        return JSONResponse({'detail':str(exc),'reason':'budget'},status_code=429,headers={'Retry-After':'3600'})
    text=str(exc)
    try:detail=json.loads(text)
    except ValueError:detail={'message':text[:1500]}
    from fastapi.responses import JSONResponse
    return JSONResponse({'detail':detail},status_code=422)

def user(request:Request):
    current=security.session_for(request.cookies.get('ngs_session'))
    if not current:raise HTTPException(401,'Sign in to this workspace')
    if request.method in ['POST','PUT','PATCH','DELETE'] and not secrets.compare_digest(request.headers.get('x-csrf-token',''),current['csrf']):raise HTTPException(403,'Session CSRF verification failed')
    return current

def admin(current=Depends(user)):
    if current['role']!='admin':raise HTTPException(403,'Administrator permission required')
    return current

def owned_run(rid,current):
    row=db.query('SELECT * FROM runs WHERE id=? AND user_id=?',(rid,current['id']),one=True)
    if not row:raise HTTPException(404,'Run not found')
    return row

def safe_run(row):
    return {**{k:v for k,v in row.items() if k!='graph'},'graph':json.loads(row['graph'])}

@app.get('/api/bootstrap')
def bootstrap(request:Request):
    current=security.session_for(request.cookies.get('ngs_session'))
    return {'setup_required':not bool(db.query('SELECT id FROM users LIMIT 1')),'user':security.public_user(current) if current else None,'csrf':current['csrf'] if current else None,'deployment':'cloud-research' if CLOUD else 'desktop-research','region':'us-east-1','demo':demo.public_info(current['id'] if current else None)}

@app.post('/api/auth/{operation}')
def auth(operation:str,request:Request,response:Response,payload:dict=Body(...)):
    if operation not in ['signup','login']:raise HTTPException(404)
    email=str(payload.get('email','')).strip().lower();password=payload.get('password','')
    if not re.fullmatch(r'[^\s@]{1,100}@[^\s@]{1,100}',email) or not isinstance(password,str) or not 12<=len(password)<=256:raise ValueError('Use a valid email and a password of 12–256 characters')
    key=(request.client.host,email);now=time.time();AUTH_ATTEMPTS[key]=[x for x in AUTH_ATTEMPTS.get(key,[]) if now-x<60]
    AUTH_ATTEMPTS.move_to_end(key)
    if len(AUTH_ATTEMPTS)>4096:AUTH_ATTEMPTS.popitem(last=False)
    if len(AUTH_ATTEMPTS[key])>=8:raise HTTPException(429,'Too many attempts; wait one minute')
    AUTH_ATTEMPTS[key].append(now)
    # Password derivation is intentionally outside the serialized write transaction.
    candidate=db.query('SELECT * FROM users WHERE email=?',(email,),one=True)
    valid=security.verify_password(password,candidate['password']) if candidate else False
    if valid and operation=='login' and not mfa.check(candidate['id'],payload.get('otp','')):raise HTTPException(401,'Authenticator or recovery code required')
    hashed=security.password_hash(password) if operation=='signup' else None
    with db.tx() as c:
        row=c.execute('SELECT * FROM users WHERE email=?',(email,)).fetchone()
        if operation=='signup':
            if row:raise ValueError('An account with this email already exists')
            first=not c.execute('SELECT id FROM users LIMIT 1').fetchone();uid=db.ident('u_')
            c.execute('INSERT INTO users(id,email,password,role,tier,quota_seconds,created) VALUES(?,?,?,?,?,?,?)',(uid,email,hashed,'admin' if first else 'user','explore' if CLOUD else 'trial',TRIAL_SECONDS,now))
            db.audit(c,uid,'user.signup',uid,{'tier':'trial','cpu_seconds':TRIAL_SECONDS,'first_owner':first})
        else:
            if not row or not valid or row['password']!=candidate['password']:raise HTTPException(401,'Email or password is incorrect')
            uid=row['id'];db.audit(c,uid,'user.login',uid)
        token,csrf=security.issue_session(c,uid)
    response.set_cookie('ngs_session',token,httponly=True,samesite='strict',secure=request.url.scheme=='https',max_age=43200,path='/')
    return {'user':security.public_user(db.query('SELECT * FROM users WHERE id=?',(uid,),one=True)),'csrf':csrf}

@app.post('/api/logout')
def logout(request:Request,response:Response,current=Depends(user)):
    with db.tx() as c:
        c.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(request.cookies['ngs_session'].encode()).hexdigest(),));db.audit(c,current['id'],'user.logout')
    response.delete_cookie('ngs_session');return {'ok':True}

@app.get('/api/session')
def session(current=Depends(user)):return {'user':security.public_user(current),'csrf':current['csrf']}

@app.get('/api/catalog')
def catalog(current=Depends(user)):
    from . import catalogue_view
    limited=entitlements.restricted(current['id'])
    templates=[{**t,'availability':{'note':'Explore runnable' if all(n['tool'] in demo.TOOLS for n in t['graph']['nodes']) else 'Research Pro execution required; design and self-hosting remain available'}} for t in registry.templates()]
    return {**catalogue_view.view(current['id']),'templates':templates,'capabilities':runner.capabilities(),'demo':demo.public_info(current['id'])}

@app.get('/api/settings')
def settings(current=Depends(user)):return data.settings(current['id'])

@app.patch('/api/settings')
def update_settings(patch:dict=Body(...),current=Depends(user)):return data.update_settings(current,patch)

@app.get('/api/assets')
def assets(q:str='',current=Depends(user)):
    if entitlements.restricted(current['id']) and current['role']!='admin':return {'assets':[],'scans':[],'public_sources':[],'transfers':[]}
    from .asset_listing import list_assets
    rows=list_assets(current['id'],query=q,limit=300,cloud=CLOUD)
    return {'assets':rows,'scans':db.query('SELECT * FROM scans WHERE user_id=? ORDER BY started DESC LIMIT 20',(current['id'],)),'public_sources':data.PUBLIC,'transfers':db.query('SELECT * FROM transfers WHERE user_id=? ORDER BY started DESC LIMIT 30',(current['id'],))}

@app.get('/api/assets/resolve')
def resolve(family:str,assembly:str|None=None,source_id:str|None=None,asset_id:str|None=None,current=Depends(user)):return {'candidates':data.resolve(current['id'],family,assembly,data.settings(current['id'])['backend'],source_id,asset_id)}

@app.get('/api/assets/{aid}/rights')
def asset_rights_details(aid:str,current=Depends(user)):
    from . import asset_rights
    return asset_rights.disclosure(current['id'],asset_rights.owned(current['id'],aid))

@app.post('/api/admin/data-rights/{uid}/{aid}')
def acknowledge_data_rights(uid:str,aid:str,payload:dict=Body(...),current=Depends(admin)):
    from . import asset_rights
    return asset_rights.acknowledge(current,uid,aid,payload)

@app.post('/api/admin/data-rights/{uid}/{identity}/revoke')
def revoke_data_rights(uid:str,identity:str,payload:dict=Body(...),current=Depends(admin)):
    from . import asset_rights
    return asset_rights.revoke(current,uid,identity,str(payload.get('reason','')))

@app.get('/api/assets/{aid}')
def get_asset(aid:str,current=Depends(user)):
    row=db.query('SELECT * FROM assets WHERE id=? AND user_id=?',(aid,current['id']),one=True)
    if not row:raise HTTPException(404)
    row['metadata']=json.loads(row['metadata']);return row

@app.get('/api/directories')
def directories(path:str='',current=Depends(admin)):
    if demo.DEMO:raise HTTPException(403,'Local directories are available in the self-hosted edition')
    root=SSD;p=Path(path or root).expanduser().resolve(strict=True)
    if not p.is_relative_to(root) or not p.is_dir():raise ValueError('Browse is restricted to the connected external SSD')
    children=[]
    for x in sorted(p.iterdir(),key=lambda x:x.name.lower()):
        try:
            if not x.name.startswith('.') and x.is_dir() and not x.is_symlink():children.append({'name':x.name,'path':str(x)})
        except OSError:continue
    return {'path':str(p),'parent':str(p.parent) if p!=root else None,'directories':children[:300]}

@app.post('/api/tier-requests')
def tier_request(payload:dict=Body(...),current=Depends(user)):
    if payload.get('tier') not in ['academic','pro','enterprise'] or not 10<=len(str(payload.get('reason','')))<=2000:raise ValueError('Select a tier and explain intended usage in at least ten characters')
    key='tier-request:'+current['id']
    with db.tx() as c:
        c.execute('INSERT INTO controls VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,db.encode({'tier':payload['tier'],'reason':payload['reason'],'at':time.time(),'state':'pending'})))
        db.audit(c,current['id'],'entitlement.review.requested',detail={'tier':payload['tier']})
    return {'state':'pending'}

@app.post('/api/assets/scan')
def scan(payload:dict=Body(...),current=Depends(user)):return {'id':data.scan(current['id'],payload.get('root',''))}

@app.post('/api/public/{source_id}/probe')
def probe(source_id:str,current=Depends(user)):return data.probe_public(source_id)

@app.post('/api/assets/{aid}/sync')
def sync(aid:str,current=Depends(user)):return {'id':data.start_transfer(current,aid)}

@app.post('/api/transfers/{tid}/cancel')
def cancel_transfer(tid:str,current=Depends(user)):
    with db.tx() as c:
        result=c.execute('UPDATE transfers SET cancel_requested=1 WHERE id=? AND user_id=?',(tid,current['id']))
        if not result.rowcount:raise HTTPException(404)
        db.audit(c,current['id'],'transfer.cancel.requested',tid)
    return {'requested':True}

@app.post('/api/uploads')
async def upload(request:Request,current=Depends(user)):
    if current['frozen']:raise ValueError('Workspace is frozen')
    name=Path(request.headers.get('x-file-name','upload.dat')).name
    if not re.fullmatch(r'[A-Za-z0-9._ -]{1,160}',name):raise ValueError('Choose a simple file name without path characters')
    folder=DATA/'uploads'/current['id']/db.ident();folder.mkdir(parents=True,exist_ok=True)
    path=folder/name;total=0;reservation=int(request.headers.get('content-length',64*1024*1024));tid=db.ident('transfer_');aid=db.ident('upload_')
    with db.tx() as c:
        if data.storage_exposure(c,current['id'])+reservation>data.settings(current['id'])['max_upload_gib']*1024**3:raise ValueError('Retained data and concurrent upload quota exhausted')
        c.execute('INSERT INTO transfers(id,user_id,asset_id,state,total,started) VALUES(?,?,?,?,?,?)',(tid,current['id'],aid,'browser-upload',reservation,time.time()))
    try:
        with path.open('wb') as f:
            async for chunk in request.stream():
                total+=len(chunk)
                if total>64*1024*1024:raise ValueError('Browser upload exceeds 64 MiB; register its SSD directory or use S3 sync')
                f.write(chunk)
        fmt=data.file_type(path)
        if not fmt:raise ValueError('Unrecognized genomic file extension')
        with db.tx() as c:
            c.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?,?)',(aid,current['id'],str(path),name,fmt,total,path.stat().st_mtime,'upload',None,db.encode({'root':str(folder),'uploaded':True})))
            db.audit(c,current['id'],'asset.uploaded',aid,{'bytes':total,'format':fmt})
            c.execute("UPDATE transfers SET state='browser-complete',bytes=?,total=?,finished=? WHERE id=?",(total,total,time.time(),tid))
        return {'id':aid,'name':name,'bytes':total,'format':fmt}
    except BaseException:
        path.unlink(missing_ok=True)
        with db.tx() as c:c.execute("UPDATE transfers SET state='failed',finished=? WHERE id=?",(time.time(),tid))
        raise

@app.get('/api/graphs')
def graphs(current=Depends(user)):return db.query('SELECT id,name,revision,updated FROM graphs WHERE user_id=? ORDER BY updated DESC',(current['id'],))

@app.get('/api/graphs/{gid}')
def get_graph(gid:str,current=Depends(user)):
    row=db.query('SELECT * FROM graphs WHERE id=? AND user_id=?',(gid,current['id']),one=True)
    if not row:raise HTTPException(404)
    return {**row,'body':json.loads(row['body'])}

@app.put('/api/graphs/{gid}')
def save_graph(gid:str,payload:dict=Body(...),current=Depends(user)):
    g=graph.validate_shape(payload['graph']);revision=payload.get('revision',0);name=str(g.get('name','Untitled pipeline'))[:160]
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',gid):raise ValueError('Invalid graph ID')
    with db.tx() as c:
        old=c.execute('SELECT * FROM graphs WHERE id=?',(gid,)).fetchone()
        if old and old['user_id']!=current['id']:raise HTTPException(404)
        if old and old['revision']!=revision:raise HTTPException(409,'Graph changed in another window. Reload before saving.')
        next_revision=(old['revision'] if old else 0)+1
        c.execute('INSERT INTO graphs VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,revision=excluded.revision,body=excluded.body,updated=excluded.updated',(gid,current['id'],name,next_revision,db.encode(g),time.time()))
        db.audit(c,current['id'],'graph.saved',gid,{'revision':next_revision,'digest':graph.graph_digest(g)})
    return {'id':gid,'revision':next_revision}

@app.post('/api/graph/preflight')
def preflight(payload:dict=Body(...),current=Depends(user)):return graph.preflight(payload['graph'],current,runner.capabilities())

@app.post('/api/graph/fix')
def fix(payload:dict=Body(...),current=Depends(user)):
    checked=graph.preflight(payload['graph'],current)
    fixes=[x['fix'] for x in checked['issues'] if x.get('fix')]
    fixed=graph.apply_fixes(payload['graph'],fixes)
    with db.tx() as c:db.audit(c,current['id'],'graph.autofix',detail={'fixes':len(fixes),'before':graph.graph_digest(payload['graph']),'after':graph.graph_digest(fixed)})
    return {'graph':fixed,'applied':len(fixes),'preflight':graph.preflight(fixed,current,runner.capabilities())}

@app.post('/api/runs')
def submit(payload:dict=Body(...),request:Request=None,current=Depends(user)):
    operation=request.headers.get('idempotency-key','')
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,100}',operation):raise ValueError('Idempotency-Key header required')
    row,created=runner.admit(current,payload['graph'],operation)
    return {'run':safe_run(row),'created':created}

@app.get('/api/runs')
def runs(current=Depends(user)):return [safe_run(r) for r in db.query('SELECT * FROM runs WHERE user_id=? ORDER BY created DESC LIMIT 100',(current['id'],))]

@app.get('/api/runs/{rid}')
def get_run(rid:str,current=Depends(user)):
    row=owned_run(rid,current);attempts=db.query('SELECT * FROM attempts WHERE run_id=? ORDER BY started',(rid,))
    for a in attempts:a['outputs']=json.loads(a['outputs']);a['metrics']=json.loads(a['metrics'])
    return {'run':safe_run(row),'attempts':attempts,'artifacts':db.query('SELECT id,node_id,name,format,bytes,digest FROM artifacts WHERE run_id=? AND user_id=?',(rid,current['id']))}

@app.post('/api/runs/{rid}/cancel')
def cancel(rid:str,current=Depends(user)):
    owned_run(rid,current);runner.stop(rid,current['id']);return {'requested':True}

@app.get('/api/runs/{rid}/events')
async def events(rid:str,request:Request,after:int=0,current=Depends(user)):
    owned_run(rid,current)
    try:cursor=max(after,int(request.headers.get('last-event-id','0')))
    except ValueError:raise HTTPException(400)
    async def stream():
        nonlocal cursor
        idle=0;started=time.monotonic()
        while not await request.is_disconnected() and time.monotonic()-started<55:
            live=await run_in_threadpool(security.session_for,request.cookies.get('ngs_session'))
            if not live or live['id']!=current['id']:return
            rows=await run_in_threadpool(db.query,'SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT 200',(rid,cursor))
            for row in rows:
                cursor=row['seq'];row['payload']=json.loads(row['payload'])
                yield f"id: {cursor}\ndata: {db.encode(row)}\n\n"
            if not rows:
                idle+=1
                if idle%10==0:yield ': heartbeat\n\n'
                r=await run_in_threadpool(db.query,'SELECT state FROM runs WHERE id=?',(rid,),one=True)
                if r and r['state'] in TERMINAL:
                    yield 'event: complete\ndata: {}\n\n';return
            await asyncio.sleep(1)
    return StreamingResponse(stream(),media_type='text/event-stream',headers={'X-Accel-Buffering':'no'})

@app.get('/api/artifacts/{aid}')
def artifact(aid:str,request:Request,current=Depends(user)):
    from . import retention
    retention.lease(current['id'],aid,seconds=120)
    r=db.query('SELECT * FROM artifacts WHERE id=? AND user_id=?',(aid,current['id']),one=True)
    if not r:raise HTTPException(404)
    meta=json.loads(r['metadata'])
    with db.tx() as c:db.audit(c,current['id'],'artifact.accessed',aid)
    if request.query_params.get('metadata')=='1':return {**{k:v for k,v in r.items() if k!='path'},'metadata':meta}
    entitlements.reserve_download(current,r['bytes'])
    if r['path'].startswith('s3://'):
        bucket,key=r['path'][5:].split('/',1)
        if not key.startswith(f"tenants/{current['id']}/"):raise HTTPException(403)
        name=re.sub(r'[^A-Za-z0-9_. -]','_',r['name'])[:160] or 'OpenNGS-artifact.bin'
        args={'Bucket':bucket,'Key':key,'ResponseContentType':'application/octet-stream','ResponseContentDisposition':'attachment; filename="'+name+'"','ResponseCacheControl':'private, no-store'}
        if meta.get('version_id'):args['VersionId']=meta['version_id']
        return RedirectResponse(aws_session().client('s3',config=__import__('botocore.config',fromlist=['Config']).Config(signature_version='s3v4')).generate_presigned_url('get_object',Params=args,ExpiresIn=60))
    p=data.permitted_path(current['id'],r['path'])
    return FileResponse(p,filename=r['name'],headers={'Content-Security-Policy':"sandbox; default-src 'none'"},media_type='application/octet-stream')

@app.get('/api/vault')
def vault(current=Depends(user)):return security.secret_metadata(current['id'])

@app.post('/api/vault')
def put_vault(payload:dict=Body(...),current=Depends(user)):return {'id':security.put_secret(current,payload)}

@app.post('/api/vault/{sid}/revoke')
def revoke(sid:str,current=Depends(user)):
    with db.tx() as c:
        result=c.execute('UPDATE secrets SET revoked=1 WHERE id=? AND user_id=?',(sid,current['id']))
        if not result.rowcount:raise HTTPException(404)
        db.audit(c,current['id'],'vault.revoked',sid)
    return {'revoked':True,'provider_revocation':'Revoke or rotate the underlying token with its provider as well.'}

@app.post('/api/vault/{sid}/check')
def check_secret(sid:str,current=Depends(user)):
    from .providers import health
    return health(current['id'],sid)

@app.get('/api/admin')
def admin_status(current=Depends(admin)):
    users=[security.public_user(u) for u in db.query('SELECT * FROM users ORDER BY created')]
    runs=db.query('SELECT id,user_id,state,created,started,finished,reserved,used,error FROM runs ORDER BY created DESC LIMIT 100')
    records=db.query('SELECT seq,body,hash,previous_hash FROM audit ORDER BY seq DESC LIMIT 300')
    for row in records:row['event']=json.loads(row.pop('body'))
    return {'users':users,'runs':runs,'audit':records,'audit_verification':security.verify_audit(),'quarantine':db.query('SELECT * FROM quarantine'),'controls':db.query('SELECT * FROM controls'),'capabilities':runner.capabilities(),'storage_bytes':sum(r['bytes'] for r in db.query('SELECT bytes FROM artifacts'))}

@app.post('/api/admin/users/{uid}')
def admin_user(uid:str,payload:dict=Body(...),current=Depends(admin)):
    reason=str(payload.get('reason','')).strip()
    if len(reason)<5:raise ValueError('An audit reason is required')
    allowed={'quota_seconds','tier','frozen'}
    patch={k:v for k,v in payload.items() if k in allowed}
    if 'quota_seconds'in patch and (type(patch['quota_seconds'])!=int or not 0<=patch['quota_seconds']<=100000000):raise ValueError('Invalid CPU quota')
    if 'tier'in patch and patch['tier'] not in ['trial','academic','pro','enterprise','explore','research-pro']:raise ValueError('Invalid tier')
    if CLOUD and patch.get('tier') in ['pro','enterprise','research-pro'] and not entitlements.research(uid):raise ValueError('Use the funded Research Pro allowance control; a tier label does not authorize paid execution')
    if 'frozen'in patch and type(patch['frozen'])!=bool:raise ValueError('Invalid freeze state')
    with db.tx() as c:
        if not c.execute('SELECT id FROM users WHERE id=?',(uid,)).fetchone():raise HTTPException(404)
        for key,value in patch.items():c.execute(f'UPDATE users SET {key}=? WHERE id=?',(value,uid))
        db.audit(c,current['id'],'admin.user.override',uid,{'reason':reason,'changes':patch})
        if patch.get('frozen'):c.execute("UPDATE runs SET cancel_requested=1,state='cancelling' WHERE user_id=? AND state IN ('queued','running')",(uid,))
    return {'ok':True}

@app.post('/api/admin/runs/{rid}/kill')
def admin_kill(rid:str,payload:dict=Body(...),current=Depends(admin)):
    if len(str(payload.get('reason','')))<5:raise ValueError('An audit reason is required')
    runner.stop(rid,current['id']);return {'requested':True}

@app.post('/api/admin/freeze')
def freeze(payload:dict=Body(...),current=Depends(admin)):
    if type(payload.get('frozen'))!=bool or len(str(payload.get('reason','')))<5:raise ValueError('Choose a freeze state and supply an audit reason')
    with db.tx() as c:
        c.execute("INSERT INTO controls VALUES('global_freeze',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(json.dumps(payload['frozen']),))
        if payload['frozen']:c.execute("UPDATE runs SET cancel_requested=1,state='cancelling' WHERE state IN ('queued','running')")
        db.audit(c,current['id'],'admin.global.freeze',detail=payload)
    return {'requested':True}

@app.post('/api/admin/quarantine')
def quarantine(payload:dict=Body(...),current=Depends(admin)):
    tid=payload.get('tool_id');reason=str(payload.get('reason',''))
    if tid not in registry.definitions(current['id']) or len(reason)<5:raise ValueError('Select a node release and provide a reason')
    with db.tx() as c:
        if payload.get('release'):c.execute('DELETE FROM quarantine WHERE tool_id=?',(tid,))
        else:c.execute('INSERT INTO quarantine VALUES(?,?,?) ON CONFLICT(tool_id) DO UPDATE SET reason=excluded.reason,at=excluded.at',(tid,reason,time.time()))
        db.audit(c,current['id'],'admin.node.release' if payload.get('release') else 'admin.node.quarantine',tid,{'reason':reason})
    return {'ok':True}

@app.post('/api/tools/publish')
def publish_tool(payload:dict=Body(...),current=Depends(admin)):
    image=payload.get('image','');name=str(payload.get('name','')).strip();command=payload.get('command');ports=payload.get('inputs');outputs=payload.get('outputs');files=payload.get('output_files')
    if not re.fullmatch(r'[a-zA-Z0-9./_:-]+@sha256:[a-f0-9]{64}',image) or not 1<len(name)<100:raise ValueError('Supply a name and a digest-pinned image')
    if command is None and isinstance(payload.get('command_line'),str):command=[shlex.split(payload['command_line'])]
    if not isinstance(command,list) or not 1<=len(command)<=10 or any(not isinstance(row,list) or not row or any(not isinstance(x,str) or len(x)>8192 for x in row) for row in command):raise ValueError('Command must be an array of argv arrays')
    if not isinstance(ports,list) or not isinstance(outputs,list) or not isinstance(files,dict):raise ValueError('Explicit input/output port contracts are required')
    for p in ports+outputs:
        if not isinstance(p,dict) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,50}',str(p.get('key',''))) or not re.fullmatch(r'[A-Z][A-Z0-9_]*(?:\|[A-Z][A-Z0-9_]*)*',str(p.get('type',''))):raise ValueError('Invalid port definition')
        p.setdefault('required',True);p.setdefault('many',False)
    for p in outputs:
        out=files.get(p['key'],{});filename=out.get('file','')
        if not filename or Path(filename).is_absolute() or '..' in Path(filename).parts or out.get('type')!=p['type']:raise ValueError('Output file contracts must stay inside /work and match declared port types')
    license=payload.get('license','Review required')
    if license not in ['FOSS','Academic','Commercial BYOL','Paid API BYOK','Review required']:raise ValueError('Invalid licence category')
    delivery=payload.get('secret_delivery')
    if delivery is not None and (not isinstance(delivery,dict) or delivery.get('kind')!='file' or delivery.get('provider') not in ['sentieon','dragen','hgmd','annovar']):raise ValueError('Local licence delivery supports a scoped file for an approved commercial provider')
    capture=payload.get('capture_stdout',False)
    if type(capture)!=bool:raise ValueError('Stdout capture must be a toggle')
    if capture and (len(files)!=1 or any('*' in v['file'] for v in files.values())):raise ValueError('Stdout capture needs one explicit output file')
    tid=db.ident('custom_');spec=registry.node(tid,name,'Custom tools',ports,outputs,'Workspace-published OCI execution contract.',license=license,image=image,docs=payload.get('docs'),command=command,output_files=files,approved=True,secret_delivery=delivery,credential=delivery['provider'] if delivery else None,capture_stdout=capture)
    with db.tx() as c:
        c.execute('INSERT INTO custom_tools VALUES(?,?,?,1)',(tid,current['id'],db.encode(spec)))
        db.audit(c,current['id'],'tool.published',tid,{'image':image,'license':license})
    return spec

@app.post('/api/tools/{tid}/resolve-image')
def resolve_image(tid:str,payload:dict=Body(...),current=Depends(admin)):
    from .images import resolve
    spec=registry.definitions(current['id']).get(tid)
    if not spec:raise HTTPException(404)
    image=payload.get('image') or spec.get('image')
    if not image:raise ValueError('Supply the exact vendor/BioContainer image to review')
    resolved=resolve(image)
    return {'tool_id':tid,**resolved,'approval_required':True}

@app.post('/api/cloud/check')
def cloud_check(current=Depends(admin)):
    s=data.settings(current['id']);session=aws_session();identity=session.client('sts').get_caller_identity();result={'account':identity['Account'],'region':'us-east-1','bucket':None,'batch':None}
    if s['s3_bucket']:
        client=session.client('s3');location=client.get_bucket_location(Bucket=s['s3_bucket'])['LocationConstraint'] or 'us-east-1'
        encryption=client.get_bucket_encryption(Bucket=s['s3_bucket'])
        result['bucket']={'region':location,'encryption':encryption['ServerSideEncryptionConfiguration'],'region_ok':location=='us-east-1'}
    if s['batch_queue']:result['batch']=session.client('batch').describe_job_queues(jobQueues=[s['batch_queue']])['jobQueues']
    with db.tx() as c:db.audit(c,current['id'],'cloud.connection.checked',detail={'account':identity['Account'],'region':'us-east-1'})
    return result

@app.get('/health/ready')
def ready():return {'ok':bool(db.query('SELECT 1 AS ok',one=True)),'service':'ngs-studio','region':'us-east-1'}

from .extension_api import install as install_extensions
install_extensions(app,user,admin,publish_tool)
from .operations_api import install as install_operations
install_operations(app,user,admin)
app.add_middleware(Admission)

app.mount('/static',StaticFiles(directory=FRONTEND),name='static')
@app.get('/')
@app.get('/studio')
@app.get('/settings')
@app.get('/admin')
def index():return FileResponse(FRONTEND/'index.html')
