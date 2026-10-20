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
RESEARCH_MONTHLY_USD = 500
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
        c.execute('CREATE TABLE IF NOT EXISTS baseline_storage_allocations(plan_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,plan_sha256 TEXT NOT NULL,bytes BIGINT NOT NULL,created REAL NOT NULL,actor TEXT NOT NULL,reason TEXT NOT NULL)')

def period():return datetime.now(timezone.utc).strftime('%Y-%m')

def snapshot(c=None):
    if c is None:
        with db.tx() as connection:return snapshot(connection)
    p=policy();month=period()
    # Include unresolved commitments from previous months.
    outstanding=int(c.execute("SELECT COALESCE(SUM(reserved_micro),0) FROM cost_reservations WHERE state='reserved' AND id NOT IN (SELECT reservation_id FROM cost_allocations WHERE account='research-pro')").fetchone()[0])
    charged=int(c.execute("SELECT COALESCE(SUM(charged_micro),0) FROM cost_reservations WHERE period=? AND state='reconciled' AND id NOT IN (SELECT reservation_id FROM cost_allocations WHERE account='research-pro')",(month,)).fetchone()[0])
    posted=c.execute('SELECT gross_micro,checked FROM billing_checks WHERE period=?',(month,)).fetchone()
    completed_forecast=math.ceil((p['fixed_pretax_usd']*MICRO+charged)*(1+p['tax_fraction']))
    # Billing is delayed and cannot prove which active commitments it includes.
    # Preserve every outstanding hold above the larger completed/posted floor.
    total_forecast=max(completed_forecast,posted['gross_micro'] if posted else 0)+math.ceil(outstanding*(1+p['tax_fraction']))
    remaining=math.floor((p['limit_usd']-p['safety_usd'])*MICRO)-total_forecast
    alert=c.execute('SELECT value FROM controls WHERE key=?',('baseline-admission-hold:'+month,)).fetchone()
    held=bool(alert and alert['value']=='true')
    return {**p,'period':month,'reserved_usd':outstanding/MICRO,'reconciled_variable_usd':charged/MICRO,
        'forecast_including_tax_usd':total_forecast/MICRO,'available_after_safety_usd':max(0,remaining)/MICRO,
        'admission_open':remaining>0 and not held,'alert_admission_hold':held,'billing_checked':posted['checked'] if posted else None,
        'note':'Conservative estimates; delayed billing, abuse and unexpected AWS charges can still overshoot. No automatic paid scaling.'}

def research_snapshot(c,uid,monthly_budget_usd):
    """Independent tenant budget, including tax and unresolved earlier work."""
    rows=c.execute("SELECT r.state,COALESCE(SUM(r.reserved_micro),0) AS reserved,COALESCE(SUM(r.charged_micro),0) AS charged FROM cost_reservations r JOIN cost_allocations a ON a.reservation_id=r.id WHERE a.tenant=? AND a.account IN ('research-pro','validation') AND (r.period=? OR r.state='reserved') GROUP BY r.state",(uid,period())).fetchall()
    outstanding=sum(int(r['reserved']) for r in rows if r['state']=='reserved')
    charged=sum(int(r['charged']) for r in rows if r['state']=='reconciled')
    posted=c.execute('SELECT value FROM controls WHERE key=?',('pro-posted:'+uid+':'+period(),)).fetchone()
    tax=1+policy()['tax_fraction']
    forecast=max(math.ceil(charged*tax),int(posted['value']) if posted else 0)+math.ceil(outstanding*tax)
    ceiling=min(RESEARCH_MONTHLY_USD,float(monthly_budget_usd))
    remaining=max(0,math.floor(ceiling*MICRO)-forecast)
    alert=c.execute('SELECT value FROM controls WHERE key=?',('pro-admission-hold:'+uid+':'+period(),)).fetchone()
    frozen=c.execute("SELECT value FROM controls WHERE key='global_freeze'").fetchone()
    held=bool(alert and alert['value']=='true')
    return {'limit_usd':ceiling,'period':period(),'reserved_usd':outstanding/MICRO,
            'reconciled_pretax_usd':charged/MICRO,'posted_including_tax_usd':int(posted['value'])/MICRO if posted else 0,
            'forecast_including_tax_usd':forecast/MICRO,'available_usd':remaining/MICRO,
            'alert_admission_hold':held,'admission_open':remaining>0 and not held and not (frozen and frozen['value']=='true')}

def reserve(c,uid,operation,kind,pretax_usd,details):
    if not math.isfinite(pretax_usd) or pretax_usd<0:raise ValueError('Invalid cost estimate')
    old=c.execute('SELECT * FROM cost_reservations WHERE user_id=? AND operation=? AND kind=?',(uid,operation,kind)).fetchone()
    if old:return dict(old)
    frozen=c.execute("SELECT value FROM controls WHERE key='global_freeze'").fetchone()
    if frozen and frozen['value']=='true':raise BudgetFull('New paid work is paused by the infrastructure safety freeze; accepted reservations are preserved')
    state=snapshot(c);micro=math.ceil(pretax_usd*MICRO)
    from . import entitlements
    grant=entitlements.research(uid)
    account='research-pro' if grant else 'explore'
    validation=c.execute('SELECT value FROM controls WHERE key=?',('validation-tenant:'+uid,)).fetchone()
    if grant and validation and validation['value']=='true':account='validation'
    hold=c.execute('SELECT value FROM controls WHERE key=?',('baseline-admission-hold:'+period(),)).fetchone()
    if (not grant or account=='validation') and hold and hold['value']=='true':raise BudgetFull('Baseline cost alert is active; accepted work is preserved and new paid work waits for administrative reconciliation')
    if grant:
        p=json.loads(grant['body']) if isinstance(grant['body'],str) else grant['body']
        tenant=research_snapshot(c,uid,p['monthly_budget_usd'])
        if tenant['alert_admission_hold']:raise BudgetFull('The Research Pro tenant budget alert is active; new spending waits for administrative reconciliation')
        if math.ceil(micro*(1+state['tax_fraction']))>round(tenant['available_usd']*MICRO):raise BudgetFull('This user’s independent Research Pro monthly allowance, including tax, cannot cover this reservation')
    elif kind=='scientific' and pretax_usd>state['max_job_usd']:raise BudgetFull('Run exceeds the per-job spending limit; reduce resources or split the workflow')
    if (not grant or account=='validation') and micro*(1+state['tax_fraction'])>state['available_after_safety_usd']*MICRO:
        raise BudgetFull('Monthly budget capacity is full. Your work is saved; no additional paid work was started.')
    rid=db.ident('cost_')
    c.execute("INSERT INTO cost_reservations(id,user_id,operation,period,kind,reserved_micro,state,details,created) VALUES(?,?,?,?,?,?,'reserved',?,?)",(rid,uid,operation,period(),kind,micro,db.encode(details),time.time()))
    c.execute('INSERT INTO cost_allocations VALUES(?,?,?)',(rid,account,uid))
    db.audit(c,uid,'budget.reserved',rid,{'kind':kind,'account':account,'maximum_pretax_usd':micro/MICRO})
    return dict(c.execute('SELECT * FROM cost_reservations WHERE id=?',(rid,)).fetchone())

def reserve_shared(c, actor, operation, kind, pretax_usd, details):
    """Operator-only shared acquisition/build hold, independent of actor's tier.

    Shared catalogue work belongs to the baseline even when the administrator
    has a separately funded Pro workspace. There is deliberately no HTTP route.
    """
    admin = c.execute('SELECT role,frozen FROM users WHERE id=?', (actor,)).fetchone()
    if not admin or admin['role'] != 'admin' or admin['frozen']:
        raise ValueError('An active administrator must authorize shared spending')
    if not math.isfinite(pretax_usd) or pretax_usd < 0:
        raise ValueError('Invalid shared cost estimate')
    old = c.execute('SELECT * FROM cost_reservations WHERE user_id=? AND operation=? AND kind=?',
                    (actor, operation, kind)).fetchone()
    if old:
        allocation = c.execute('SELECT account FROM cost_allocations WHERE reservation_id=?', (old['id'],)).fetchone()
        if not allocation or allocation['account'] != 'shared-platform':
            raise ValueError('An existing reservation belongs to another cost pool')
        return dict(old)
    state = snapshot(c)
    frozen = c.execute("SELECT value FROM controls WHERE key='global_freeze'").fetchone()
    if not state['admission_open'] or (frozen and frozen['value'] == 'true'):
        raise BudgetFull('New shared spending is paused; existing commitments are preserved')
    micro = math.ceil(pretax_usd * MICRO)
    if math.ceil(micro * (1 + state['tax_fraction'])) > round(state['available_after_safety_usd'] * MICRO):
        raise BudgetFull('The combined baseline allowance cannot cover shared work and its tax reserve')
    rid = db.ident('cost_')
    c.execute("INSERT INTO cost_reservations(id,user_id,operation,period,kind,reserved_micro,state,details,created) VALUES(?,?,?,?,?,?,'reserved',?,?)",
              (rid, actor, operation, period(), kind, micro, db.encode(details), time.time()))
    c.execute('INSERT INTO cost_allocations VALUES(?,?,?)', (rid, 'shared-platform', actor))
    db.audit(c, actor, 'budget.shared.reserved', rid,
             {'kind': kind, 'maximum_pretax_usd': micro / MICRO, 'account': 'shared-platform'})
    return dict(c.execute('SELECT * FROM cost_reservations WHERE id=?', (rid,)).fetchone())


def reconcile(operation,uid,kind,actual_pretax_usd=None):
    with db.tx() as c:
        row=c.execute("SELECT * FROM cost_reservations WHERE user_id=? AND operation=? AND kind=? AND state='reserved'",(uid,operation,kind)).fetchone()
        if not row:return
        # Charge the conservative reservation until measured provider usage is known.
        amount=row['reserved_micro'] if actual_pretax_usd is None else math.ceil(actual_pretax_usd*MICRO)
        if amount<0:raise ValueError('Invalid actual cost')
        c.execute("UPDATE cost_reservations SET state='reconciled',charged_micro=?,finished=? WHERE id=?",(amount,time.time(),row['id']))
        db.audit(c,uid,'budget.reconciled',row['id'],{'pretax_usd':amount/MICRO,'source':'observed-allocation-at-conservative-tariff' if actual_pretax_usd is not None else 'conservative-reservation'})

def graph_estimate(g,definitions,uid=None):
    from .config import DEMO
    from .entitlements import restricted
    if uid and not restricted(uid) and DEMO:
        total=0;resources=[]
        for n in g['nodes']:
            spec=definitions[n['tool']]
            if spec.get('asset_family') or n['tool'] in ['asset.file','reference.asset','database.asset','utility.export','utility.gather','utility.condition']:continue
            if spec.get('api') or spec.get('license')=='Paid API BYOK':raise BudgetFull('External API execution requires a reviewed bounded tariff')
            if spec.get('resource')=='gpu':raise BudgetFull('GPU execution requires a published profile and bounded GPU tariff')
            r={**spec['resources'],**n.get('resources',{})};hours=(r['timeout_seconds']+600)/3600
            if r.get('scratch_mb',2048)>184320 or n.get('execution_profile')=='exclusive-vm':
                from .vm import select
                profile=select(r);hours=(r['timeout_seconds']+900)/3600
                price=hours*(profile['hourly_usd']+(20+profile['scratch_gib'])*.08/730)
            else:price=hours*(r['threads']*.06+r['memory_mb']/1024*.008+.02)
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

def allocate_baseline_storage(actor, plan_id, reason):
    """Explicit reviewed allocation from existing baseline storage, not a grant.

    This internal operator action has no user HTTP route. Source membership is
    established by the registered immutable migration plan, never asset tags.
    """
    from . import migration
    admin = db.query('SELECT role FROM users WHERE id=?', (actor,), one=True)
    if not admin or admin['role'] != 'admin' or len(str(reason).strip()) < 10:
        raise ValueError('A platform administrator and allocation reason are required')
    record = db.query('SELECT value FROM controls WHERE key=?', ('migration:plan:'+plan_id,), one=True)
    if not record:
        raise ValueError('Only a registered immutable migration plan can use baseline storage')
    registration = json.loads(record['value']);plan = migration.load_plan(registration)
    amount = int(plan['total_bytes'])
    if amount <= 0 or amount != sum(int(o['bytes']) for o in plan['objects']):
        raise ValueError('Migration byte total is invalid')
    with db.tx() as c:
        current = c.execute('SELECT value FROM controls WHERE key=?', ('migration:plan:'+plan_id,)).fetchone()
        if not current or current['value'] != record['value']:
            raise ValueError('Migration registration changed during allocation')
        old = c.execute('SELECT * FROM baseline_storage_allocations WHERE plan_id=?', (plan_id,)).fetchone()
        if old:
            if old['plan_sha256'] != registration['sha256'] or old['bytes'] != amount or old['user_id'] != plan['owner_id']:
                raise ValueError('An existing baseline allocation cannot change immutable identity')
            return dict(old)
        allocated = int(c.execute('SELECT COALESCE(SUM(bytes),0) FROM baseline_storage_allocations').fetchone()[0])
        if allocated + amount > min(600, policy()['max_storage_gib']) * 1024**3:
            raise BudgetFull('Reviewed migrations exceed the funded baseline storage capacity')
        c.execute('INSERT INTO baseline_storage_allocations VALUES(?,?,?,?,?,?,?)',
                  (plan_id, plan['owner_id'], registration['sha256'], amount, time.time(), actor, reason.strip()))
        db.audit(c, actor, 'budget.baseline.storage.allocated', plan_id,
                 {'owner': plan['owner_id'], 'bytes': amount, 'plan_sha256': registration['sha256'],
                  'included_in_existing_fixed_forecast': True, 'research_access_granted': False})
        return dict(c.execute('SELECT * FROM baseline_storage_allocations WHERE plan_id=?', (plan_id,)).fetchone())


def baseline_storage(uid, c):
    """Return reviewed immutable allocations; altered registrations fail closed."""
    rows = c.execute('SELECT * FROM baseline_storage_allocations WHERE user_id=?', (uid,)).fetchall()
    valid = []
    for row in rows:
        registered = c.execute('SELECT value FROM controls WHERE key=?', ('migration:plan:'+row['plan_id'],)).fetchone()
        proof = json.loads(registered['value']) if registered else {}
        if proof.get('sha256') == row['plan_sha256'] and proof.get('owner_id') == uid:
            valid.append(dict(row))
    return valid


def baseline_stored_bytes(uid, c):
    """Actual verified migrated objects only; aliases and user metadata add zero."""
    datasets = {}
    for allocation in baseline_storage(uid, c):
        rows = c.execute("""SELECT DISTINCT d.id,d.bytes FROM datasets d
          JOIN migration_assets m ON m.dataset_id=d.id AND m.user_id=d.user_id
          JOIN migration_objects o ON o.user_id=d.user_id AND o.sha256=d.sha256 AND o.version_id=d.version_id
          WHERE d.user_id=? AND o.plan_id=? AND d.state='Ready' AND o.state='Ready'""",
          (uid, allocation['plan_id'])).fetchall()
        if sum(int(r['bytes']) for r in rows) > allocation['bytes']:
            raise BudgetFull('Migrated storage exceeds its reviewed baseline allocation')
        for row in rows:
            datasets[row['id']] = int(row['bytes'])
    return sum(datasets.values())


def billable_storage_capacity(uid, maximum, c):
    included = sum(int(r['bytes']) for r in baseline_storage(uid, c))
    return max(0, maximum-included), min(maximum, included)


def reserve_storage_capacity(uid,allowance):
    """Fund retained capacity once per month; upload/job charges exclude it."""
    with db.tx() as c:
        target,included=billable_storage_capacity(uid,allowance['max_storage_bytes'],c)
        rows=c.execute("SELECT r.details FROM cost_reservations r JOIN cost_allocations a ON a.reservation_id=r.id WHERE a.tenant=? AND a.account IN ('research-pro','validation') AND r.kind='retained-capacity' AND r.period=?",(uid,period())).fetchall()
        covered=sum(json.loads(x['details']).get('additional_bytes',0) for x in rows);additional=max(0,target-covered)
        if not additional:return
        return reserve(c,uid,'retained-capacity-v2:'+period()+':'+str(target),'retained-capacity',additional/1024**3*.023,
                       {'additional_bytes':additional,'baseline_funded_bytes':included,'total_tenant_capacity_bytes':allowance['max_storage_bytes'],
                        'scope':'Only additional Pro retained capacity; reviewed migration storage remains in the baseline fixed forecast'})

initialize()
