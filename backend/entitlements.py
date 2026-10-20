"""Funded research allowances and transactional usage; never inferred from a label."""
import json
import math
import time
from datetime import datetime,timezone
from . import db
from .config import CLOUD,DEMO

EXPLORE={'access':'explore','label':'OpenNGS Explore','monthly_budget_usd':0,
 'max_upload_bytes':0,'max_storage_bytes':256*1024**2,'retention_days':7,
 'max_vcpus':2,'max_memory_mb':4096,'max_scratch_mb':20480,'max_gpu':0,
 'max_node_seconds':120,'max_job_seconds':3600,'max_concurrent_jobs':1,'max_queued_jobs':20,
 'daily_cpu_seconds':3600,'monthly_cpu_seconds':28800,'max_download_bytes_monthly':1024**3,
 'max_nodes':24,'max_output_bytes':64*1024**2}
REQUIRED={k for k in EXPLORE if k not in ['access','label','max_nodes','max_output_bytes']}

def initialize():
    with db.tx() as c:
        c.execute('CREATE TABLE IF NOT EXISTS research_allowances(user_id TEXT PRIMARY KEY,version BIGINT NOT NULL,state TEXT NOT NULL,body TEXT NOT NULL,updated REAL NOT NULL,actor TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS usage_holds(run_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,access TEXT NOT NULL,day TEXT NOT NULL,month TEXT NOT NULL,reserved BIGINT NOT NULL,used BIGINT NOT NULL DEFAULT 0,state TEXT NOT NULL)')
        c.execute('CREATE INDEX IF NOT EXISTS usage_owner_period ON usage_holds(user_id,month,state)')
        c.execute('CREATE TABLE IF NOT EXISTS download_holds(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,month TEXT NOT NULL,bytes BIGINT NOT NULL,created REAL NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS output_holds(run_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,bytes BIGINT NOT NULL,state TEXT NOT NULL)')

def research(uid):
    r=db.query('SELECT * FROM research_allowances WHERE user_id=?',(uid,),one=True)
    return r if r and r['state']=='active' else None

def limits(uid):
    r=research(uid)
    if r:return {**json.loads(r['body']),'access':'research-pro','label':'OpenNGS Research Pro','grant_version':r['version']}
    return dict(EXPLORE)

def restricted(uid):return DEMO and not bool(research(uid))

def grant(actor,uid,payload):
    reason=str(payload.get('reason','')).strip()
    if len(reason)<10:raise ValueError('Explain the funded allowance in at least ten characters')
    if payload.get('state')=='revoked':
        with db.tx() as c:
            c.execute("UPDATE research_allowances SET state='revoked',updated=?,actor=? WHERE user_id=?",(time.time(),actor['id'],uid))
            c.execute("UPDATE users SET tier=? WHERE id=?",('explore' if CLOUD else 'trial',uid))
            db.audit(c,actor['id'],'entitlement.research.revoked',uid,{'reason':reason})
        return limits(uid)
    body=payload.get('allowance',{})
    if set(body)!=REQUIRED:raise ValueError('Specify every allowance explicitly: '+', '.join(sorted(REQUIRED)))
    for key,value in body.items():
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<0:raise ValueError('Invalid allowance: '+key)
        if key!='monthly_budget_usd' and type(value)!=int:raise ValueError('Allowance must be an integer: '+key)
    if payload.get('funding_confirmed') is not True or not 0<body['monthly_budget_usd']<=100000:raise ValueError('Research Pro requires an explicitly funded additional monthly budget')
    if not 1<=body['max_vcpus']<=192 or not 512<=body['max_memory_mb']<=1572864 or not 1<=body['retention_days']<=3650 or not 1<=body['max_concurrent_jobs']<=50:raise ValueError('Resource allowance exceeds the supported administrative envelope')
    if not 1<=body['max_node_seconds']<=172800 or not body['max_node_seconds']<=body['max_job_seconds']<=604800 or not 1<=body['max_queued_jobs']<=10000:raise ValueError('Set finite job, node and queue limits')
    if not 0<body['max_upload_bytes']<=5*1024**4 or body['max_storage_bytes']<body['max_upload_bytes']:raise ValueError('Set funded dataset and stored-byte limits')
    if not 0<=body['max_gpu']<=8 or body['max_scratch_mb']>4*1024**2:raise ValueError('GPU/scratch allowance exceeds the supported profile envelope')
    if body['monthly_cpu_seconds']<body['daily_cpu_seconds'] or body['daily_cpu_seconds']<=0:raise ValueError('Daily/monthly CPU allowances are inconsistent')
    from . import budget
    with db.tx() as c:billable_bytes,baseline_bytes=budget.billable_storage_capacity(uid,body['max_storage_bytes'],c)
    recurring_storage=billable_bytes/1024**3*.023*(1+budget.policy()['tax_fraction'])
    if body['monthly_budget_usd']<=recurring_storage+1:raise ValueError('The additional allowance must cover its assigned retained-storage capacity, conservative tax reserve and at least $1 operational headroom')
    body.update(max_nodes=2000,max_output_bytes=body['max_storage_bytes'])
    with db.tx() as c:
        if not c.execute('SELECT id FROM users WHERE id=?',(uid,)).fetchone():raise ValueError('User does not exist')
        old=c.execute('SELECT version FROM research_allowances WHERE user_id=?',(uid,)).fetchone();version=(old['version'] if old else 0)+1
        c.execute("INSERT INTO research_allowances VALUES(?,?,'active',?,?,?) ON CONFLICT(user_id) DO UPDATE SET version=excluded.version,state=excluded.state,body=excluded.body,updated=excluded.updated,actor=excluded.actor",(uid,version,db.encode(body),time.time(),actor['id']))
        c.execute("UPDATE users SET tier='research-pro',quota_seconds=? WHERE id=?",(body['monthly_cpu_seconds'],uid))
        db.audit(c,actor['id'],'entitlement.research.funded',uid,{'reason':reason,'version':version,'allowance':body,'funding_confirmed':True,'baseline_storage_bytes':baseline_bytes,'additional_storage_capacity_bytes':billable_bytes})
    return limits(uid)

def storage_exposure(c,uid):
    datasets=c.execute("SELECT s3_key,bytes FROM datasets WHERE user_id=? AND state!='cancelled'",(uid,)).fetchall()
    keys={r['s3_key'] for r in datasets};total=sum(int(r['bytes']) for r in datasets);seen=set()
    for row in c.execute('SELECT path,bytes,metadata FROM artifacts WHERE user_id=?',(uid,)):
        objects=[{'uri':row['path'],'bytes':row['bytes']}]+json.loads(row['metadata']).get('secondary_objects',[])
        for item in objects:
            uri=item['uri']
            if uri in seen:continue
            seen.add(uri)
            if uri.startswith('s3://') and uri[5:].split('/',1)[1] in keys:continue
            total+=int(item['bytes'])
    return total

def output_storage_exposure(c,uid,access):
    total=storage_exposure(c,uid)
    if access=='explore':
        from .budget import baseline_stored_bytes
        total-=baseline_stored_bytes(uid,c)
    return max(0,total)

def reserve_output(c,user,rid,requested=None):
    p=limits(user['id']);pending=int(c.execute("SELECT COALESCE(SUM(bytes),0) FROM output_holds WHERE user_id=? AND state='reserved'",(user['id'],)).fetchone()[0])
    available=p['max_storage_bytes']-output_storage_exposure(c,user['id'],p['access'])-pending
    desired=p['max_output_bytes'] if p['access']=='explore' else (available if requested is None else requested)
    if type(desired)!=int or desired<=0 or desired>available or desired>p['max_output_bytes']:raise ValueError('The retained-storage allowance cannot cover the requested output reservation; completed data is preserved')
    c.execute("INSERT INTO output_holds VALUES(?,?,?,'reserved')",(rid,user['id'],desired));return desired

def hold(c,user,rid,seconds):
    if not CLOUD:return
    p=limits(user['id']);now=datetime.now(timezone.utc);day=now.strftime('%Y-%m-%d');month=now.strftime('%Y-%m')
    for field,period,ceiling in [('day',day,p['daily_cpu_seconds']),('month',month,p['monthly_cpu_seconds'])]:
        # Outstanding work stays reserved across period boundaries.
        total=c.execute('SELECT COALESCE(SUM(CASE WHEN state=\'reserved\' THEN reserved ELSE used END),0) FROM usage_holds WHERE user_id=? AND ('+field+"=? OR state='reserved')",(user['id'],period)).fetchone()[0]
        if total+seconds>ceiling:raise ValueError('The '+field+' CPU allowance cannot cover this job; reduce the request or wait for the next period')
    queued=c.execute("SELECT COUNT(*) FROM runs WHERE user_id=? AND state NOT IN ('succeeded','failed','cancelled','interrupted')",(user['id'],)).fetchone()[0]
    if queued>=p['max_queued_jobs']:raise ValueError('The assigned queued-work allowance is full; accepted jobs are preserved')
    if p['access']=='explore':
        held=c.execute("SELECT COUNT(*) FROM usage_holds WHERE user_id=? AND state='reserved'",(user['id'],)).fetchone()[0]*p['max_output_bytes']
        if output_storage_exposure(c,user['id'],p['access'])+held+p['max_output_bytes']>p['max_storage_bytes']:raise ValueError('Explore retained-output allowance cannot cover this run; existing outputs are preserved and an administrator must reconcile retention capacity')
    c.execute("INSERT INTO usage_holds VALUES(?,?,?,?,?,?,0,'reserved')",(rid,user['id'],p['access'],day,month,seconds))

def settle(c,rid,seconds):
    c.execute("UPDATE usage_holds SET state='settled',used=? WHERE run_id=? AND state='reserved'",(seconds,rid))
    c.execute("UPDATE output_holds SET state='settled' WHERE run_id=?",(rid,))

def reserve_download(user,size):
    if not CLOUD:return
    p=limits(user['id']);month=datetime.now(timezone.utc).strftime('%Y-%m')
    with db.tx() as c:
        used=c.execute('SELECT COALESCE(SUM(bytes),0) FROM download_holds WHERE user_id=? AND month=?',(user['id'],month)).fetchone()[0]
        if used+size>p['max_download_bytes_monthly']:raise ValueError('Monthly export-byte allowance is exhausted')
        c.execute('INSERT INTO download_holds VALUES(?,?,?,?,?)',(db.ident('download_'),user['id'],month,size,time.time()))

def remaining(uid):
    now=datetime.now(timezone.utc);p=limits(uid);usage={}
    with db.tx() as c:
        for field,key,cap in [('day',now.strftime('%Y-%m-%d'),'daily_cpu_seconds'),('month',now.strftime('%Y-%m'),'monthly_cpu_seconds')]:
            value=int(c.execute("SELECT COALESCE(SUM(CASE WHEN state='reserved' THEN reserved ELSE used END),0) FROM usage_holds WHERE user_id=? AND ("+field+"=? OR state='reserved')",(uid,key)).fetchone()[0]);usage[cap]=max(0,p[cap]-value)
        pending=int(c.execute("SELECT COALESCE(SUM(bytes),0) FROM output_holds WHERE user_id=? AND state='reserved'",(uid,)).fetchone()[0])
        from .budget import baseline_stored_bytes
        physical=storage_exposure(c,uid);baseline=baseline_stored_bytes(uid,c)
        usage['storage_bytes']=max(0,p['max_storage_bytes']-(max(0,physical-baseline) if p['access']=='explore' else physical)-pending)
        usage['physical_stored_bytes']=physical
        usage['baseline_funded_stored_bytes']=baseline
        charged=int(c.execute("SELECT COALESCE(SUM(CASE WHEN r.state='reserved' THEN r.reserved_micro ELSE r.charged_micro END),0) FROM cost_reservations r JOIN cost_allocations a ON a.reservation_id=r.id WHERE a.tenant=? AND a.account IN ('research-pro','validation') AND (r.period=? OR r.state='reserved')",(uid,now.strftime('%Y-%m'))).fetchone()[0])
        posted=c.execute('SELECT value FROM controls WHERE key=?',('pro-posted:'+uid+':'+now.strftime('%Y-%m'),)).fetchone()
        hold=c.execute('SELECT value FROM controls WHERE key=?',('pro-admission-hold:'+uid+':'+now.strftime('%Y-%m'),)).fetchone()
    from .budget import policy,snapshot,MICRO
    usage['budget_usd']=max(0,p['monthly_budget_usd']-max(charged*(1+policy()['tax_fraction']),int(posted['value']) if posted else 0)/MICRO) if research(uid) else snapshot()['available_after_safety_usd']
    usage['admission_open']=not (hold and hold['value']=='true') if research(uid) else snapshot()['admission_open']
    return usage

initialize()
