"""Conservative application-paid usage reservations; billing alerts are secondary.

All amounts are integer micro-USD. Fixed costs, taxes and uncertainty are reserved
for the entire month before accepting variable work. Unknown external outcomes
retain their reservations. This is admission control, not an AWS billing cap.
"""
import calendar
import json
import math
import os
import time
from datetime import datetime, timezone
from . import db

MICRO = 1_000_000
class BudgetFull(ValueError): pass

def policy():
    return {'limit_usd': min(200.0, float(os.getenv('NGS_MONTHLY_LIMIT_USD', '200'))),
        'fixed_pretax_usd': float(os.getenv('NGS_FIXED_MONTHLY_USD', '110')),
        'tax_fraction': float(os.getenv('NGS_TAX_FRACTION', '.18')),
        'safety_usd': float(os.getenv('NGS_BUDGET_SAFETY_USD', '15')),
        'target_usd': 180.0,
        'max_job_usd': float(os.getenv('NGS_MAX_JOB_USD', '2')),
        'max_storage_gib': float(os.getenv('NGS_STORAGE_GIB', '600')),
        'region': 'us-east-1'}

def initialize():
    with db.tx() as c:
        c.execute('CREATE TABLE IF NOT EXISTS cost_reservations(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,operation TEXT NOT NULL,period TEXT NOT NULL,kind TEXT NOT NULL,reserved_micro BIGINT NOT NULL,charged_micro BIGINT NOT NULL DEFAULT 0,state TEXT NOT NULL,details TEXT NOT NULL,created REAL NOT NULL,finished REAL,UNIQUE(user_id,operation,kind))')
        c.execute('CREATE INDEX IF NOT EXISTS cost_period_state ON cost_reservations(period,state)')
        c.execute('CREATE TABLE IF NOT EXISTS billing_checks(period TEXT PRIMARY KEY,gross_micro BIGINT NOT NULL,checked REAL NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS cost_allocations(reservation_id TEXT PRIMARY KEY,account TEXT NOT NULL,tenant TEXT NOT NULL)')

def period():return datetime.now(timezone.utc).strftime('%Y-%m')

def snapshot(c=None):
    if c is None:
        with db.tx() as connection:return snapshot(connection)
    p=policy();month=period()
    # Include unresolved commitments from previous months.
    outstanding=c.execute("SELECT COALESCE(SUM(reserved_micro),0) FROM cost_reservations WHERE state='reserved' AND id NOT IN (SELECT reservation_id FROM cost_allocations WHERE account='research-pro')").fetchone()[0]
    charged=c.execute("SELECT COALESCE(SUM(charged_micro),0) FROM cost_reservations WHERE period=? AND state='reconciled' AND id NOT IN (SELECT reservation_id FROM cost_allocations WHERE account='research-pro')",(month,)).fetchone()[0]
    posted=c.execute('SELECT gross_micro,checked FROM billing_checks WHERE period=?',(month,)).fetchone()
    total_forecast=math.ceil((p['fixed_pretax_usd']*MICRO+outstanding+charged)*(1+p['tax_fraction']))
    # Posted gross already includes tax. Do not add it a second time.
    total_forecast=max(total_forecast,posted['gross_micro'] if posted else 0)
    remaining=math.floor((p['limit_usd']-p['safety_usd'])*MICRO)-total_forecast
    return {**p,'period':month,'reserved_usd':outstanding/MICRO,'reconciled_variable_usd':charged/MICRO,
        'forecast_including_tax_usd':total_forecast/MICRO,'available_after_safety_usd':max(0,remaining)/MICRO,
        'admission_open':remaining>0,'billing_checked':posted['checked'] if posted else None,
        'note':'Conservative estimates; delayed billing, abuse and unexpected AWS charges can still overshoot. No automatic paid scaling.'}

def reserve(c,uid,operation,kind,pretax_usd,details):
    if not math.isfinite(pretax_usd) or pretax_usd<0:raise ValueError('Invalid cost estimate')
    old=c.execute('SELECT * FROM cost_reservations WHERE user_id=? AND operation=? AND kind=?',(uid,operation,kind)).fetchone()
    if old:return dict(old)
    state=snapshot(c);micro=math.ceil(pretax_usd*MICRO)
    from . import entitlements
    grant=entitlements.research(uid)
    account='research-pro' if grant else 'explore'
    if grant:
        p=json.loads(grant['body']) if isinstance(grant['body'],str) else grant['body']
        held=c.execute("SELECT COALESCE(SUM(CASE WHEN r.state='reserved' THEN r.reserved_micro ELSE r.charged_micro END),0) FROM cost_reservations r JOIN cost_allocations a ON a.reservation_id=r.id WHERE a.tenant=? AND a.account='research-pro' AND (r.period=? OR r.state='reserved')",(uid,period())).fetchone()[0]
        remaining=p['monthly_budget_usd']*MICRO-held*(1+state['tax_fraction'])
        if micro*(1+state['tax_fraction'])>remaining:raise BudgetFull('Research Pro additional monetary allowance cannot cover this reservation')
    elif kind=='scientific' and pretax_usd>state['max_job_usd']:raise BudgetFull('Run exceeds the per-job spending limit; reduce resources or split the workflow')
    if not grant and micro*(1+state['tax_fraction'])>state['available_after_safety_usd']*MICRO:
        raise BudgetFull('Monthly budget capacity is full. Your work is saved; no additional paid work was started.')
    rid=db.ident('cost_')
    c.execute("INSERT INTO cost_reservations(id,user_id,operation,period,kind,reserved_micro,state,details,created) VALUES(?,?,?,?,?,?,'reserved',?,?)",(rid,uid,operation,period(),kind,micro,db.encode(details),time.time()))
    c.execute('INSERT INTO cost_allocations VALUES(?,?,?)',(rid,account,uid))
    db.audit(c,uid,'budget.reserved',rid,{'kind':kind,'maximum_pretax_usd':micro/MICRO})
    return dict(c.execute('SELECT * FROM cost_reservations WHERE id=?',(rid,)).fetchone())

def reconcile(operation,uid,kind,actual_pretax_usd=None):
    with db.tx() as c:
        row=c.execute("SELECT * FROM cost_reservations WHERE user_id=? AND operation=? AND kind=? AND state='reserved'",(uid,operation,kind)).fetchone()
        if not row:return
        # Charge the conservative reservation until measured provider usage is known.
        amount=row['reserved_micro'] if actual_pretax_usd is None else math.ceil(actual_pretax_usd*MICRO)
        if amount<0:raise ValueError('Invalid actual cost')
        c.execute("UPDATE cost_reservations SET state='reconciled',charged_micro=?,finished=? WHERE id=?",(amount,time.time(),row['id']))
        db.audit(c,uid,'budget.reconciled',row['id'],{'pretax_usd':amount/MICRO,'source':'measured' if actual_pretax_usd is not None else 'conservative-reservation'})

def graph_estimate(g,definitions,uid=None):
    from .config import DEMO
    from .entitlements import restricted
    if uid and not restricted(uid) and DEMO:
        total=0;resources=[]
        for n in g['nodes']:
            spec=definitions[n['tool']]
            if spec.get('api') or spec.get('license')=='Paid API BYOK':raise BudgetFull('External API execution requires a reviewed bounded tariff')
            if spec.get('resource')=='gpu':raise BudgetFull('GPU execution requires a published profile and bounded GPU tariff')
            r={**spec['resources'],**n.get('resources',{})};hours=(r['timeout_seconds']+600)/3600
            price=hours*(r['threads']*.06+r['memory_mb']/1024*.008+r.get('scratch_mb',2048)/1024*.0002)
            total+=price;resources.append({'node':n['id'],'maximum_hours_with_staging':hours,'upper_pretax_usd':price})
        return {'pretax_usd':round(total+.10,6),'nodes':resources,'backend':'Research Pro reviewed cloud queue','validation':'Estimate only; capacity and dependencies must pass preflight'}
    if DEMO:
        seconds=sum({**definitions[n['tool']]['resources'],**n.get('resources',{})}['timeout_seconds'] for n in g['nodes'])
        # One Fargate task for the entire bounded graph. Includes pull/staging,
        # output storage, publication requests and conservative processing overhead.
        total=(seconds+180)/3600*(2*.04048+4*.004445)+.03
        return {'pretax_usd':round(total,6),'maximum_seconds':seconds,'cpu':2,'memory_gib':4,'output_bytes':64*1024*1024,'backend':'Fargate single-run sandbox'}
    total=0;nodes=[]
    for n in g['nodes']:
        spec=definitions[n['tool']]
        if spec.get('api') or spec.get('license')=='Paid API BYOK':
            raise BudgetFull('Application-paid API calls require a reviewed, bounded tariff before admission')
        if n['tool'].startswith(('asset.','reference.','database.')):continue
        r={**spec['resources'],**n.get('resources',{})};hours=r['timeout_seconds']/3600
        # Current isolated VM lane: t3.medium per node; forbid implicit resizing.
        if r['threads']>2 or r['memory_mb']>4096 or r.get('scratch_mb',2048)>8192:
            raise BudgetFull('This deployment permits at most 2 vCPU, 4 GiB RAM and 8 GiB scratch per scientific node')
        # Boot/staging/reaping allowance, disk, a bounded output and request overhead.
        estimate=(hours+10/60)*.0416+(16*.08*(hours+10/60)/730)+.01+.25*.023
        total+=estimate;nodes.append({'node':n['id'],'maximum_hours':hours,'estimate_usd':estimate})
    return {'pretax_usd':round(total,6),'nodes':nodes,'output_gib_ceiling_per_node':.25,'includes':'maximum configured runtime, boot/reaper allowance, disk, requests and one month of bounded output storage'}

initialize()
