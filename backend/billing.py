"""Project-only posted cost checks and authenticated AWS budget-alert consumption."""
import json,math,os,re,time
from datetime import datetime,timedelta,timezone
from . import budget,db
from .config import aws_session

PROJECTS=['OpenNGS','ngs-studio','OpenNGS-validation','OpenNGS-Pro','OpenNGS-worker']
def initialize():
    with db.tx() as c:c.execute('CREATE TABLE IF NOT EXISTS billing_alerts(id TEXT PRIMARY KEY,at REAL NOT NULL,severity TEXT NOT NULL,message TEXT NOT NULL,period TEXT NOT NULL)')
    try:
        with db.tx() as c:c.execute('ALTER TABLE billing_alerts ADD COLUMN tenant TEXT')
    except Exception as e:
        if type(e).__name__ not in ['DuplicateColumn','OperationalError'] or ('already exists' not in str(e) and 'duplicate column' not in str(e)):raise

def reconcile_posted():
    now=datetime.now(timezone.utc);start=now.replace(day=1).date().isoformat();end=(now.date()+timedelta(days=1)).isoformat()
    client=aws_session().client('ce');token=None;groups=[]
    while True:
        response=client.get_cost_and_usage(TimePeriod={'Start':start,'End':end},Granularity='MONTHLY',Metrics=['UnblendedCost'],GroupBy=[{'Type':'TAG','Key':'Project'},{'Type':'TAG','Key':'TenantId'}],Filter={'And':[{'Tags':{'Key':'Project','Values':PROJECTS}},{'Not':{'Dimensions':{'Key':'RECORD_TYPE','Values':['Credit','Refund']}}}]},**({'NextPageToken':token} if token else {}))
        groups.extend(g for r in response['ResultsByTime'] for g in r.get('Groups',[]));token=response.get('NextPageToken')
        if not token:break
    service_cost=0;tenants={};pro_shared=0
    for group in groups:
        project,tenant=(key.partition('$')[2] for key in group['Keys']);amount=float(group['Metrics']['UnblendedCost']['Amount'])
        internal=db.query('SELECT value FROM controls WHERE key=?',('validation-tenant:'+tenant,),one=True) if tenant else None
        if project not in ['OpenNGS-Pro','OpenNGS-worker'] or (internal and internal['value']=='true'):service_cost+=amount
        elif tenant:tenants[tenant]=tenants.get(tenant,0)+amount
        else:pro_shared+=amount
    conservative=math.ceil(max(0,service_cost)*(1+budget.policy()['tax_fraction'])*budget.MICRO)
    with db.tx() as c:
        c.execute('INSERT INTO billing_checks VALUES(?,?,?) ON CONFLICT(period) DO UPDATE SET gross_micro=excluded.gross_micro,checked=excluded.checked',(budget.period(),conservative,time.time()))
        for uid,amount in tenants.items():
            c.execute('INSERT INTO controls VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',('pro-posted:'+uid+':'+budget.period(),str(math.ceil(max(0,amount)*(1+budget.policy()['tax_fraction'])*budget.MICRO))))
        c.execute('INSERT INTO controls VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',('pro-capacity-cost:'+budget.period(),db.encode({'posted_pretax_usd':pro_shared,'scope':'Pro-tagged shared capacity, excluded from the educational baseline; must be separately funded'})))
        db.audit(c,'billing-service','billing.project.reconciled',budget.period(),{'tagged_service_cost_usd':service_cost,'including_conservative_tax_reserve_usd':conservative/budget.MICRO,'source':'AWS Cost Explorer; delayed posted charges, credits/refunds excluded'})
    return {'tagged_baseline_service_cost_usd':service_cost,'including_tax_reserve_usd':conservative/budget.MICRO,'research_pro_tenants_pretax_usd':tenants,'research_pro_shared_pretax_usd':pro_shared,'note':'Delayed tagged charges; tax is reserved conservatively. Untagged historical charges require invoice reconciliation.'}

def record_alert(identifier,topic,message):
    expected=[os.getenv('NGS_BUDGET_WARNING_TOPIC'),os.getenv('NGS_BUDGET_CRITICAL_TOPIC')]
    pro=topic==os.getenv('NGS_PRO_BUDGET_TOPIC') and bool(topic)
    if (topic not in expected and not pro) or not topic:raise ValueError('Unrecognized budget topic')
    tenant=None
    if pro:
        match=re.search(r'openngs-pro-(u_[a-f0-9]{32})',str(message))
        if not match:raise ValueError('Research budget notification has no exact tenant budget identity')
        tenant=match.group(1)
    severity='critical' if topic==expected[1] or pro else 'warning';month=budget.period()
    with db.tx() as c:
        c.execute('INSERT INTO billing_alerts(id,at,severity,message,period,tenant) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING',(identifier,time.time(),severity,str(message)[:6000],month,tenant))
        if severity=='critical' or pro:
            control='pro-admission-hold:'+tenant+':'+month if tenant else 'baseline-admission-hold:'+month
            c.execute('INSERT INTO controls VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(control,'true'))
        db.audit(c,'billing-service','billing.alert.'+severity,identifier,{'topic':topic,'period':month,'tenant':tenant,'accepted_work_preserved':True})

def poll():
    queue=os.getenv('NGS_BUDGET_QUEUE_URL')
    if not queue:time.sleep(10);return
    client=aws_session().client('sqs');response=client.receive_message(QueueUrl=queue,MaxNumberOfMessages=10,WaitTimeSeconds=20)
    for item in response.get('Messages',[]):
        event=json.loads(item['Body']);record_alert(event['MessageId'],event['TopicArn'],event['Message']);client.delete_message(QueueUrl=queue,ReceiptHandle=item['ReceiptHandle'])

def main():
    initialize();last_attempt=float('-inf')
    while True:
        if time.monotonic()-last_attempt>6*3600:
            last_attempt=time.monotonic()
            try:reconcile_posted()
            except Exception as e:print(db.encode({'event':'billing.reconcile.retry','exception':type(e).__name__}),flush=True)
        try:poll()
        except Exception as e:
            print(db.encode({'event':'billing.alert.retry','exception':type(e).__name__}),flush=True);time.sleep(10)

initialize()
if __name__=='__main__':main()
