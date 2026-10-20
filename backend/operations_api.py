import base64
import json
import math
from fastapi import Body,Depends,HTTPException,Query
from . import budget,cloud_data,data,db,mfa,entitlements,billing

def install(app,user,admin):
    @app.get('/api/operations/migration')
    def migration_status(current=Depends(user)):
        from . import migration
        return migration.status(current['id'])
    @app.get('/api/operations/retention')
    def retention_status(current=Depends(user)):
        from . import retention
        return retention.status(current['id'])

    @app.get('/api/operations/admin/retention')
    def retention_global(current=Depends(admin)):
        from . import retention
        return retention.status()

    @app.patch('/api/operations/admin/retention')
    def retention_config(payload:dict=Body(...),current=Depends(admin)):
        from . import retention
        if set(payload)-{'enabled','reason'} or type(payload.get('enabled')) is not bool:
            raise ValueError('Choose an explicit retention setting and reason')
        return retention.configure(current['id'],payload['enabled'],str(payload.get('reason','')))

    @app.post('/api/operations/runs/{rid}/retention')
    def retention_pin(rid:str,payload:dict=Body(...),current=Depends(user)):
        from . import retention
        if set(payload)-{'pinned','reason'} or type(payload.get('pinned')) is not bool:
            raise ValueError('Choose an explicit pin setting')
        return retention.pin(current['id'],rid,payload['pinned'],str(payload.get('reason','Keep these derived results')))

    @app.get('/api/admin/audit/anchors')
    def audit_anchors(current=Depends(admin)):
        from . import audit_anchor
        return audit_anchor.verify()

    @app.get('/api/operations/capabilities')
    def capability_matrix(current=Depends(user)):
        from . import capabilities
        return {**capabilities.matrix(current['id']),'dependencies':capabilities.dependencies(),'workflows':capabilities.workflow_status()}

    @app.get('/api/account/entitlements')
    def allowances(current=Depends(user)):return entitlements.limits(current['id'])

    @app.get('/api/account/usage')
    def account_usage(current=Depends(user)):
        from datetime import datetime,timezone
        now=datetime.now(timezone.utc)
        reset=datetime(now.year+(now.month==12),1 if now.month==12 else now.month+1,1,tzinfo=timezone.utc)
        allowance=entitlements.limits(current['id']);remaining=entitlements.remaining(current['id'])
        return {'allowance':allowance,'remaining':remaining,'shared_budget':budget.snapshot(),
                'execution_paused':not remaining['admission_open'] or remaining['budget_usd']<=0,
                'reset_at':reset.isoformat().replace('+00:00','Z'),'scope':'user' if allowance['access']=='research-pro' else 'shared'}

    @app.get('/api/account/capacity')
    def capacity(current=Depends(user)):
        from .provisioning import profile
        return profile(current['id'])

    @app.post('/api/admin/entitlements/{uid}')
    def grant(uid:str,payload:dict=Body(...),current=Depends(admin)):return entitlements.grant(current,uid,payload)

    @app.post('/api/admin/tool-rights/{uid}/{tool_id}')
    def tool_permission(uid:str,tool_id:str,payload:dict=Body(...),current=Depends(admin)):
        from . import tool_rights
        return tool_rights.set_permission(current,uid,tool_id,payload)

    @app.post('/api/admin/budget/reconcile')
    def reconcile_budget(current=Depends(admin)):return billing.reconcile_posted()

    @app.post('/api/admin/budget/acknowledge')
    def acknowledge_budget(payload:dict=Body(...),current=Depends(admin)):
        reason=str(payload.get('reason','')).strip()
        if len(reason)<10:raise ValueError('Explain the cost reconciliation before reopening paid admission')
        billing.reconcile_posted()
        if budget.snapshot()['available_after_safety_usd']<=0:raise ValueError('The conservative ledger still has no budget headroom')
        with db.tx() as c:
            c.execute('DELETE FROM controls WHERE key=?',('baseline-admission-hold:'+budget.period(),))
            db.audit(c,current['id'],'billing.admission.reopened',budget.period(),{'reason':reason,'baseline_ceiling_usd':200})
        return budget.snapshot()

    @app.get('/api/operations/accounting')
    def accounting(current=Depends(admin)):
        rows=db.query("SELECT COALESCE(a.account,'explore') AS account,COALESCE(a.tenant,r.user_id) AS tenant,r.period,r.kind,r.state,SUM(r.reserved_micro) AS reserved_micro,SUM(r.charged_micro) AS charged_micro FROM cost_reservations r LEFT JOIN cost_allocations a ON a.reservation_id=r.id GROUP BY COALESCE(a.account,'explore'),COALESCE(a.tenant,r.user_id),r.period,r.kind,r.state")
        return {'shared_platform_monthly_pretax_usd':budget.policy()['fixed_pretax_usd'],'baseline':budget.snapshot(),'usage':rows,'additional_shared_capacity':db.query("SELECT key,value FROM controls WHERE key LIKE 'pro-capacity-cost:%'"),'note':'Platform baseline is counted once. Research Pro rows are additional tenant charges. Unconfigured shared capacity has not been authorized.'}

    @app.post('/api/operations/estimate')
    def estimate(payload:dict=Body(...),current=Depends(user)):
        from . import graph,registry,runner
        g=payload.get('graph');check=graph.preflight(g,current,runner.capabilities())
        estimated=budget.graph_estimate(g,registry.definitions(current['id']),current['id']) if check['ok'] else None
        remaining=entitlements.remaining(current['id'])
        allowance=entitlements.limits(current['id'])
        output_bytes=allowance['max_output_bytes'] if allowance['access']=='explore' else g.get('maximum_output_bytes',remaining['storage_bytes'])
        output_fits=type(output_bytes)==int and 0<output_bytes<=min(allowance['max_output_bytes'],remaining['storage_bytes'])
        compute_fits=bool(check['ok'] and estimated and remaining['admission_open'] and estimated['pretax_usd']*(1+budget.policy()['tax_fraction'])<=remaining['budget_usd'] and check['reservation_cpu_seconds']<=min(remaining['daily_cpu_seconds'],remaining['monthly_cpu_seconds']))
        return {'preflight':check,'estimate':estimated,'allowance':allowance,'remaining':remaining,'compute_fits':compute_fits,'output_fits':output_fits,'output_reservation_bytes':output_bytes,'fits_allowance':compute_fits and output_fits,'currency':'USD','note':'Conservative upper reservation includes bounded startup. Queue delay is additional. No scientific truncation is performed; admission rechecks remaining allowances transactionally.'}
    @app.post('/api/account/mfa/setup')
    def setup_mfa(current=Depends(user)):return mfa.setup(current)

    @app.post('/api/account/mfa/enable')
    def enable_mfa(payload:dict=Body(...),current=Depends(user)):return mfa.enable(current,payload.get('code',''))

    @app.post('/api/account/sessions/revoke')
    def revoke_sessions(current=Depends(user)):
        with db.tx() as c:
            c.execute('DELETE FROM sessions WHERE user_id=?',(current['id'],));db.audit(c,current['id'],'sessions.revoked',current['id'])
        return {'signed_out':True}
    @app.get('/api/operations/budget')
    def cost(current=Depends(user)):
        result=budget.snapshot()
        result['alerts']=db.query('SELECT at,severity,message,period FROM billing_alerts '+('' if current['role']=='admin' else 'WHERE tenant IS NULL OR tenant=? ')+'ORDER BY at DESC LIMIT 10',() if current['role']=='admin' else (current['id'],))
        result['reservations']=db.query('SELECT id,kind,reserved_micro,charged_micro,state,created FROM cost_reservations WHERE user_id=? ORDER BY created DESC LIMIT 30',(current['id'],))
        return result

    @app.get('/api/operations/datasets')
    def datasets(q:str=Query('',max_length=200),after:str=Query('',max_length=2048),limit:int=Query(100,ge=1,le=100),state:str=Query('',max_length=32),current=Depends(user)):
        from . import migration  # Initialize verified alias metadata on fresh installations.
        q=q.strip()
        if state not in ['', 'Ready', 'Verifying', 'Transferring', 'Action required', 'cancelled']:
            raise ValueError('Choose a supported dataset state')
        where='d.user_id=?';args=[current['id']]
        if state:
            where+=' AND d.state=?';args.append(state)
        if q:
            pattern='%'+q.lower().replace('!','!!').replace('%','!%').replace('_','!_')+'%'
            where+=" AND (LOWER(d.name) LIKE ? ESCAPE '!' OR LOWER(d.id) LIKE ? ESCAPE '!' OR EXISTS (SELECT 1 FROM migration_assets m JOIN assets a ON a.id=m.asset_id AND a.user_id=m.user_id WHERE m.dataset_id=d.id AND m.user_id=d.user_id AND LOWER(a.name) LIKE ? ESCAPE '!'))"
            args.extend([pattern]*3)
        total=int(db.query('SELECT COUNT(*) AS total FROM datasets d WHERE '+where,args,one=True)['total'])
        if after:
            try:
                cursor=json.loads(base64.urlsafe_b64decode(after+'='*(-len(after)%4)))
                if set(cursor)!={'v','created','id','q','state','owner'} or cursor['v']!=1 or cursor['q']!=q or cursor['state']!=state or cursor['owner']!=current['id'] or type(cursor['created']) not in [int,float] or not math.isfinite(cursor['created']) or not isinstance(cursor['id'],str) or not 0<len(cursor['id'])<=200:
                    raise ValueError()
            except Exception:
                raise ValueError('Dataset cursor is invalid or belongs to a different search') from None
            where+=' AND (d.created<? OR (d.created=? AND d.id<?))';args.extend([cursor['created'],cursor['created'],cursor['id']])
        # Legacy PostgreSQL REAL columns otherwise serialize a shortened float32
        # value that differs from the value used by SQL's cursor comparison.
        rows=db.query('SELECT d.id,d.source_asset,d.name,d.bytes,d.sha256,d.state,CAST(d.created AS DOUBLE PRECISION) AS created,d.updated,d.manifest,a.family,a.format,a.assembly,a.metadata,(SELECT m.source_path FROM migration_assets m WHERE m.user_id=d.user_id AND m.dataset_id=d.id ORDER BY m.asset_id LIMIT 1) AS discovery_source_path FROM datasets d LEFT JOIN assets a ON a.user_id=d.user_id AND a.id=COALESCE(d.source_asset,d.id) WHERE '+where+' ORDER BY d.created DESC,d.id DESC LIMIT ?',(*args,limit+1))
        next_cursor=None
        if len(rows)>limit:
            rows=rows[:limit];last=rows[-1]
            next_cursor=base64.urlsafe_b64encode(db.encode({'v':1,'created':last['created'],'id':last['id'],'q':q,'state':state,'owner':current['id']}).encode()).decode().rstrip('=')
        for row in rows:
            manifest=json.loads(row.pop('manifest') or '{}');meta=json.loads(row.pop('metadata') or '{}')
            row['format']=row.get('format') or manifest.get('format') or 'DATABASE'
            row['family']=row.get('family') or 'dataset'
            row['assembly']=row.get('assembly') or manifest.get('assembly')
            from .asset_listing import public_metadata
            row['metadata']=public_metadata({k:v for k,v in {**manifest,**meta}.items() if k in ['release','version','assembly','sample_id','contig_style','cohort','description','source_identity','sha256','version_id','dataset_id']})
            from .asset_presentation import describe
            row['presentation']=describe(row,row.pop('discovery_source_path') or '')
        inventory=db.query("SELECT COUNT(*) AS files,COALESCE(SUM(a.bytes),0) AS bytes FROM assets a WHERE a.user_id=? AND a.path NOT LIKE 's3://%' AND NOT EXISTS (SELECT 1 FROM migration_assets m JOIN datasets d ON d.id=m.dataset_id AND d.user_id=m.user_id WHERE m.asset_id=a.id AND m.user_id=a.user_id AND d.state='Ready')",(current['id'],),one=True)
        inventory={key:int(value) for key,value in inventory.items()}
        return {'datasets':rows,'total':total,'next_cursor':next_cursor,'limit':limit,'q':q,'state':state,'preparing_inventory':inventory}

    @app.post('/api/operations/datasets/{ident}/companions')
    def companions(ident:str,payload:dict=Body(...),current=Depends(user)):
        if entitlements.restricted(current['id']):raise ValueError('Personal dataset binding requires funded Research Pro access')
        return cloud_data.bind(current['id'],ident,payload)

    @app.post('/api/operations/transfers')
    def initiate(payload:dict=Body(...),current=Depends(user)):return cloud_data.initiate(current,payload)

    @app.get('/api/operations/transfers/{tid}')
    def transfer(tid:str,current=Depends(user)):return cloud_data.status(current['id'],tid)

    @app.post('/api/operations/transfers/{tid}/parts/{number}')
    def part(tid:str,number:int,current=Depends(user)):return cloud_data.sign_part(current['id'],tid,number)

    @app.post('/api/operations/transfers/{tid}/complete')
    def complete(tid:str,current=Depends(user)):return cloud_data.complete(current['id'],tid)

    @app.post('/api/operations/transfers/{tid}/cancel')
    def cancel(tid:str,current=Depends(user)):
        r=cloud_data.owned(current['id'],tid)
        if r['state']!='uploading':raise ValueError('Only an incomplete upload can be cancelled')
        cloud_data.client().abort_multipart_upload(Bucket=data.settings(current['id'])['s3_bucket'],Key=r['s3_key'],UploadId=r['upload_id'])
        with db.tx() as c:
            c.execute("UPDATE multipart_transfers SET state='cancelled' WHERE id=?",(tid,))
            c.execute("UPDATE datasets SET state='cancelled' WHERE id=?",(r['dataset_id'],))
            db.audit(c,current['id'],'dataset.transfer.cancelled',r['dataset_id'])
        # Request and part-storage charges already incurred remain conservatively charged.
        budget.reconcile(tid,current['id'],'storage')
        return {'state':'cancelled'}
