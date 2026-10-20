import json
import re
import shlex
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import Body,Depends,HTTPException,Request,Response
from fastapi.responses import StreamingResponse,FileResponse
from . import db,registry,runner,data,security,onboarding,workbench

def install(app,user,admin,publish_tool):
    @app.get('/api/data/resolve')
    def resolve(family:str,assembly:str|None=None,source_id:str|None=None,asset_id:str|None=None,current=Depends(user)):
        return {'candidates':data.resolve(current['id'],family,assembly,data.settings(current['id'])['backend'],source_id,asset_id)}

    @app.post('/api/assets/{aid}/inspect')
    def inspect_asset(aid:str,current=Depends(user)):
        return workbench.inspect_header(current['id'],aid)

    @app.post('/api/onboarding/inspect')
    def inspect(payload:dict=Body(...),current=Depends(admin)):
        result=onboarding.inspect(payload)
        with db.tx() as c:db.audit(c,current['id'],'tool.contract.inspected',detail={'kind':result['kind']})
        return result

    @app.post('/api/onboarding/publish')
    def publish(payload:dict=Body(...),current=Depends(admin)):
        draft=payload.get('draft',{})
        if not payload.get('reviewed'):raise ValueError('Review the scientific, output, licence and endpoint contracts before publication')
        parameters=draft.get('parameters',[])
        if not isinstance(parameters,list) or len(parameters)>200:raise ValueError('Invalid parameter schema')
        for p in parameters:
            if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,70}',str(p.get('key',''))) or p.get('kind') not in ['text','asset','boolean','number','select'] or 'default' not in p:raise ValueError('Invalid scalar parameter contract')
            if p['kind']=='select' and (not isinstance(p.get('options'),list) or p['default'] not in p['options']):raise ValueError('Invalid enumeration')
        if len({p['key'] for p in parameters})!=len(parameters):raise ValueError('Duplicate parameter keys')
        if draft.get('api'):
            config=draft['api'];parsed=urlsplit(config.get('url',''))
            if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.port not in [None,443] or config.get('method') not in ['GET','POST'] or config.get('unsupported_auth'):raise ValueError('Unsupported API endpoint or authentication contract')
            auth=config.get('auth')
            if auth and (auth.get('kind') not in ['bearer','header'] or not re.fullmatch(r'[A-Za-z][A-Za-z0-9-]{0,80}',auth.get('header','')) or auth['header'].lower() in ['host','content-length','transfer-encoding','connection']):raise ValueError('Invalid API credential header')
            tid=db.ident('custom_');spec=registry.node(tid,str(draft.get('name','API'))[:100],'Clinical APIs',draft.get('inputs',[]),[registry.port('response','REPORT')],'Reviewed OpenAPI operation; data transmission requires explicit node consent.',parameters,license='Paid API BYOK' if auth else 'Review required',api=config,credential='customapi' if auth else None,approved=True,docs=draft.get('docs'))
            with db.tx() as c:
                c.execute('INSERT INTO custom_tools VALUES(?,?,?,1)',(tid,current['id'],db.encode(spec)));db.audit(c,current['id'],'api.contract.published',tid,{'host':parsed.hostname,'method':config['method']})
            return spec
        bindings=draft.get('bindings',[])
        for binding in bindings:
            if not isinstance(binding,dict) or binding.get('key') not in {p['key'] for p in parameters}|{p['key'] for p in draft.get('inputs',[])} or not isinstance(binding.get('prefix',''),str):raise ValueError('Invalid command binding')
        spec=publish_tool(draft,current)
        spec.update(parameters=parameters,bindings=bindings,resources={**spec['resources'],**draft.get('resources',{})})
        with db.tx() as c:c.execute('UPDATE custom_tools SET body=? WHERE id=? AND user_id=?',(db.encode(spec),spec['id'],current['id']))
        return spec

    @app.post('/api/onboarding/help')
    def help_probe(payload:dict=Body(...),current=Depends(admin)):
        import subprocess
        from .images import resolve
        image=resolve(payload.get('image',''))['image']
        binary=payload.get('binary','')
        if not re.fullmatch(r'[A-Za-z0-9_./-]{1,120}',binary):raise ValueError('Choose one explicit executable path')
        if not runner.capabilities()['docker']:raise ValueError('Isolated help introspection needs an available Docker runtime. Paste help text or a CWL/OpenAPI contract to inspect without running code.')
        command=['docker','run','--rm','--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges','--pids-limit','32','--memory','256m','--cpus','.5','--user','65534:65534','--tmpfs','/tmp:rw,noexec,nosuid,size=16m','--entrypoint',binary,image,'--help']
        result=subprocess.run(command,capture_output=True,timeout=20)
        text=(result.stdout+result.stderr)[:1024**2].decode('utf8','replace')
        inspected=onboarding.inspect({'kind':'help','content':text,'image':image,'name':binary});inspected['draft']['command']=[[binary]]
        return inspected

    @app.get('/api/tools/readiness')
    def readiness(current=Depends(user)):
        from . import capabilities
        observed={x['id']:x for x in capabilities.matrix(current['id'])['capabilities']}
        settings=data.settings(current['id']);result=[]
        for spec in registry.catalogue(current['id']):
            credential=security.credential_for(current['id'],spec['credential'],spec['id']) if spec.get('credential') else None
            capability=observed[spec['id']]
            available=spec['builtin'] and not spec.get('asset_family') and spec['id'] not in ['asset.file','reference.asset','database.asset']
            result.append({'id':spec['id'],'runtime_ready':bool(available),'dependency_ready':False,'workflow_ready':False,'status':capability['status'],'credential_bound':bool(credential),'credential_verified':False,'rights_approved':spec['license']=='FOSS' or spec['id'] in settings['rights_approved'],'backend':settings['backend'],'reason':capability['reason']+'; graph-specific inputs and dependencies must pass preflight'})
        return result

    @app.post('/api/results/{aid}/index')
    def index(aid:str,current=Depends(user)):return workbench.start_index(current['id'],aid)

    @app.get('/api/results/{aid}/status')
    def status(aid:str,current=Depends(user)):
        from . import remote_viewer
        prior=db.query("SELECT id FROM viewer_requests WHERE user_id=? AND asset_id=? AND operation='variant-index' ORDER BY created DESC,id DESC LIMIT 1",(current['id'],aid),one=True)
        if prior:return remote_viewer.status(current['id'],prior['id'])
        row=workbench.owned(current['id'],aid)
        if remote_viewer.required(current['id'],row):return remote_viewer.index_status(current['id'],aid)
        with workbench.LOCK:result=workbench.JOBS.get((current['id'],aid))
        if result:return result
        if workbench.index_path(current['id'],aid).exists():
            with workbench.connection(workbench.index_path(current['id'],aid)) as c:rows=json.loads(c.execute("SELECT value FROM meta WHERE key='rows'").fetchone()[0])
            return {'state':'ready','rows':rows}
        return {'state':'absent'}

    @app.get('/api/viewer/{request_id}')
    def viewer_status(request_id:str,current=Depends(user)):
        from . import remote_viewer
        return remote_viewer.status(current['id'],request_id)

    @app.post('/api/results/{aid}/query')
    def query(aid:str,payload:dict=Body(...),current=Depends(user)):return workbench.query(current['id'],aid,payload.get('filters',{}),payload.get('offset',0),payload.get('limit',100))

    @app.post('/api/results/{aid}/stats')
    def stats(aid:str,payload:dict=Body(...),current=Depends(user)):return workbench.stats(current['id'],aid,payload.get('filters',{}))

    @app.post('/api/results/{aid}/export/{fmt}')
    def export(aid:str,fmt:str,payload:dict=Body(...),current=Depends(user)):
        if fmt not in ['csv','tsv','vcf','pdf']:raise ValueError('Unsupported export format')
        workbench.owned(current['id'],aid);workbench.where(payload.get('filters',{}))
        with db.tx() as c:db.audit(c,current['id'],'results.exported',aid,{'format':fmt,'filters':payload.get('filters',{})})
        headers={'Content-Disposition':f'attachment; filename="OpenNGS-research-review.{fmt}"'}
        if fmt=='pdf':return Response(workbench.pdf(current['id'],aid,payload.get('filters',{})),media_type='application/pdf',headers=headers)
        return StreamingResponse(workbench.export(current['id'],aid,payload.get('filters',{}),fmt),media_type='text/plain',headers=headers)

    @app.post('/api/tracks/pileup')
    def pileup(payload:dict=Body(...),current=Depends(user)):
        result=workbench.pileup(current['id'],payload['alignment_id'],payload['reference_id'],str(payload['chrom']),int(payload['start']),int(payload['end']))
        with db.tx() as c:db.audit(c,current['id'],'track.region.accessed',payload['alignment_id'],{'chrom':payload['chrom'],'start':payload['start'],'end':payload['end']})
        return result

    @app.get('/api/tracks/{aid}/bytes')
    def track_bytes(aid:str,request:Request,secondary:str|None=None,current=Depends(user)):
        if secondary not in [None,'index','mate']:raise ValueError('Unsupported secondary binding')
        row=workbench.owned(current['id'],aid)
        source=row['path'] if row['path'].startswith('s3://') else workbench.source(current['id'],row,secondary)
        if source.startswith('s3://'):
            # Proxy only the admitted private object; no general URL proxy.
            from .config import aws_session
            uri=row['metadata'][secondary] if secondary else row['path'];bucket,key=uri[5:].split('/',1);range_header=request.headers.get('range')
            args=workbench.object_args(current['id'],row,secondary)
            if range_header:
                if not re.fullmatch(r'bytes=\d+-\d*',range_header):raise ValueError('One forward byte range is required')
                lo,hi=range_header[6:].split('-')
                if not hi:raise ValueError('Specify a bounded byte range')
                if int(hi)<int(lo) or int(hi)-int(lo)+1>16*1024**2:raise ValueError('Track ranges are limited to 16 MiB per request')
                args['Range']=range_header
            elif row['bytes']>16*1024**2:raise ValueError('Large tracks require indexed byte-range requests')
            response=aws_session().client('s3').get_object(**args)
            from . import entitlements
            try:entitlements.reserve_download(current,response['ContentLength'])
            except BaseException:response['Body'].close();raise
            def chunks():
                try:yield from response['Body'].iter_chunks(1024**2)
                finally:response['Body'].close()
            headers={'Accept-Ranges':'bytes','Content-Length':str(response['ContentLength'])}
            if response.get('ContentRange'):headers['Content-Range']=response['ContentRange']
            return StreamingResponse(chunks(),status_code=206 if range_header else 200,media_type='application/octet-stream',headers=headers)
        return FileResponse(source,media_type='application/octet-stream')

    @app.post('/api/omics/{aid}')
    def omics(aid:str,payload:dict=Body(...),current=Depends(user)):return workbench.omics(current['id'],aid,payload.get('x'),payload.get('y'))

    @app.get('/api/runs/{rid}/summary')
    def summary(rid:str,current=Depends(user)):return workbench.run_summary(current['id'],rid)

    @app.get('/api/cache')
    def cache_status(current=Depends(user)):
        return db.query('SELECT cache_key,created,last_hit,hits FROM node_cache WHERE user_id=? ORDER BY last_hit DESC LIMIT 200',(current['id'],))
