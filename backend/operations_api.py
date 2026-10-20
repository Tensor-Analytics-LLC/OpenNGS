import json
from fastapi import Body,Depends,HTTPException
from . import budget,cloud_data,data,db,mfa,entitlements,billing

def install(app,user,admin):
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

    @app.get('/api/account/capacity')
    def capacity(current=Depends(user)):
        from .provisioning import profile
        return profile(current['id'])

    @app.post('/api/admin/entitlements/{uid}')
    def grant(uid:str,payload:dict=Body(...),current=Depends(admin)):return entitlements.grant(current,uid,payload)

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
    def datasets(current=Depends(user)):
        rows=db.query('SELECT id,source_asset,name,bytes,sha256,state,created,updated FROM datasets WHERE user_id=? ORDER BY created DESC LIMIT 100',(current['id'],))
        inventory=db.query("SELECT COUNT(*) AS files,COALESCE(SUM(bytes),0) AS bytes FROM assets WHERE user_id=? AND path NOT LIKE 's3://%'",(current['id'],),one=True)
        return {'datasets':rows,'local_only_inventory':inventory}

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
