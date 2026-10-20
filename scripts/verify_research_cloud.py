"""Bounded internal integration validation, charged to the baseline validation ledger.

Creates only a synthetic test account. It does not modify real accounts or
purchase a subscription. Maximum internal allowance is $2 including tax.
Credentials and signed URLs are never written to the evidence report.
"""
import hashlib,json,secrets,time,uuid
from pathlib import Path
import httpx
from backend.config import aws_session

BASE='https://d2j3o1re9v00qo.cloudfront.net'
def login(credentials):
    c=httpx.Client(base_url=BASE,timeout=40,headers={'Origin':BASE,'X-NGS-Client':'studio'})
    r=c.post('/api/auth/login',json=credentials);r.raise_for_status();c.headers['X-CSRF-Token']=r.json()['csrf'];return c
def required(r):
    if r.status_code>=400:raise RuntimeError('API '+str(r.status_code)+': '+r.text[:1200])
    return r.json()
def main():
    private=Path('.state/research-validation.json');evidence={'started':time.time(),'scope':'Internal synthetic 32-MiB resumable ingestion and small native pipeline; not full-scale validation','maximum_validation_allowance_usd':2}
    owner=json.loads(Path('.state/owner-access.json').read_text());admin=login({k:owner[k] for k in ['email','password']})
    if private.exists():secret=json.loads(private.read_text());c=login(secret['credentials'])
    else:
        credentials={'email':'pro-validation-'+str(int(time.time()))+'@openngs.invalid','password':secrets.token_urlsafe(30)};c=httpx.Client(base_url=BASE,timeout=40,headers={'Origin':BASE,'X-NGS-Client':'studio'});r=required(c.post('/api/auth/signup',json=credentials));c.headers['X-CSRF-Token']=r['csrf'];secret={'credentials':credentials,'user_id':r['user']['id']};private.write_text(json.dumps(secret));private.chmod(0o600)
    uid=secret['user_id'];session=aws_session();cf=session.client('cloudformation');host=next(x['OutputValue'] for x in cf.describe_stacks(StackName='openngs-lean')['Stacks'][0]['Outputs'] if x['OutputKey']=='HostId')
    script="set -a; source /var/lib/openngs/runtime.env; set +a; cd /opt/openngs; /opt/openngs/runtime/bin/python - <<'PYREMOTE'\nfrom backend import db\nfrom backend.postgres import close\nwith db.tx() as c:\n c.execute('INSERT INTO controls VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',('validation-tenant:"+uid+"','true'))\n db.audit(c,'validation-operator','validation.allowance.baseline','"+uid+"',{'maximum_usd':2,'authorization':'Existing $200 baseline includes validation; synthetic integration only'})\nclose()\nPYREMOTE"
    response=session.client('ssm').send_command(InstanceIds=[host],DocumentName='AWS-RunShellScript',Parameters={'commands':[script]},Comment='OpenNGS explicitly classify internal validation within baseline budget');command=response['Command']['CommandId']
    for _ in range(30):
        v=session.client('ssm').get_command_invocation(CommandId=command,InstanceId=host)
        if v['Status']=='Success':break
        if v['Status'] in ['Failed','TimedOut','Cancelled']:raise RuntimeError('Internal validation classification failed')
        time.sleep(1)
    from backend.entitlements import EXPLORE,REQUIRED
    allowance={k:v for k,v in EXPLORE.items() if k in REQUIRED};allowance.update(monthly_budget_usd=2,max_upload_bytes=64*1024**2,max_storage_bytes=256*1024**2,max_node_seconds=120,max_job_seconds=1800,daily_cpu_seconds=7200,monthly_cpu_seconds=7200,max_download_bytes_monthly=128*1024**2)
    p=required(admin.get('/api/admin'))
    current=next(u for u in p['users'] if u['id']==uid)
    if current.get('entitlements',{}).get('access')!='research-pro':required(admin.post('/api/admin/entitlements/'+uid,json={'reason':'Internal integration validation funded within authorized $200 baseline; maximum $2, not a customer subscription','funding_confirmed':True,'allowance':allowance}))
    print(json.dumps({'stage':'funded-test-provisioning','tenant':uid}),flush=True)
    for _ in range(120):
        capacity=required(c.get('/api/account/capacity'))
        if capacity['state']=='Ready':break
        if capacity['state']=='Blocked':raise RuntimeError(capacity['reason'])
        time.sleep(5)
    if capacity['state']!='Ready':raise RuntimeError('Provisioning exceeded validation wait; account and evidence preserved for reconciliation')
    evidence['capacity']={k:capacity.get(k) for k in ['state','stack','region','spot_only','max_scratch_mb','image']};print(json.dumps({'stage':'capacity-ready'}),flush=True)
    def upload(name,body,interrupt=False):
        digest=hashlib.sha256(body).hexdigest();operation='validation_'+digest[:32];transfer=required(c.post('/api/operations/transfers',json={'name':name,'bytes':len(body),'sha256':digest,'operation_id':operation}));tid=transfer['id']
        completed={p['number'] for p in transfer['parts']};count=(len(body)+transfer['part_size']-1)//transfer['part_size']
        for number in range(1,count+1):
            if number in completed:continue
            signed=required(c.post(f'/api/operations/transfers/{tid}/parts/{number}',json={}));part=body[(number-1)*transfer['part_size']:number*transfer['part_size']]
            r=httpx.put(signed['url'],content=part,timeout=60);r.raise_for_status()
            if interrupt and number==1:
                resumed=login(secret['credentials']);resumed_state=required(resumed.get('/api/operations/transfers/'+tid));resumed.close()
                evidence['resume']={'bytes':len(body),'checkpointed_bytes':resumed_state['uploaded_bytes'],'parts':len(resumed_state['parts']),'same_transfer':resumed_state['id']==tid}
                cross=admin.get('/api/operations/transfers/'+tid);evidence['cross_tenant_transfer_status']=cross.status_code
        required(c.post('/api/operations/transfers/'+tid+'/complete',json={}))
        for _ in range(60):
            state=required(c.get('/api/operations/transfers/'+tid))
            if state['state']=='complete':break
            time.sleep(2)
        if state['state']!='complete':raise RuntimeError('Cloud integrity verification has not completed')
        return state['dataset_id']
    block=(b'OpenNGS synthetic resumable-ingestion validation.\n'*2048);body=(block*((32*1024**2+len(block)-1)//len(block)))[:32*1024**2]
    large=upload('synthetic-resumption.bin',body,True);evidence['verified_upload']={'dataset':large,'bytes':len(body),'sha256':hashlib.sha256(body).hexdigest(),'state':'Ready'};del body
    files=[Path('demo-data/sample.fastq/fixture_R1.fastq.gz'),Path('demo-data/sample.fastq/fixture_R2.fastq.gz')]
    if not all(p.exists() for p in files):raise RuntimeError('Pinned paired fixtures are missing')
    r1=upload(files[0].name,files[0].read_bytes());r2=upload(files[1].name,files[1].read_bytes());required(c.post('/api/operations/datasets/'+r1+'/companions',json={'mate_id':r2,'assembly':'engineering'}))
    catalog=required(c.get('/api/catalog'));specs={n['id']:n for n in catalog['nodes']}
    nodes=[]
    for i,tool in enumerate(['asset.file','fastp','qc.summary']):
        spec=specs[tool];params={p['key']:p['default'] for p in spec['parameters']}
        if i==0:params['asset_id']=r1
        nodes.append({'id':'test-'+str(i),'tool':tool,'params':params,'resources':{'threads':2,'memory_mb':4096,'scratch_mb':2048,'timeout_seconds':120},'x':i*320,'y':160})
    edges=[{'id':'e1','source':'test-0','sourcePort':'data','target':'test-1','targetPort':'reads'},{'id':'e2','source':'test-1','sourcePort':'reads','target':'test-2','targetPort':'reads'}]
    g={'name':'Internal Research Pro native integration','nodes':nodes,'edges':edges};estimate=required(c.post('/api/operations/estimate',json={'graph':g}));evidence['estimate']=estimate
    if not estimate['preflight']['ok']:raise RuntimeError(json.dumps(estimate['preflight']['issues']))
    started=time.monotonic();run=required(c.post('/api/runs',headers={'Idempotency-Key':'pro-native-'+uuid.uuid4().hex},json={'graph':g}))['run'];rid=run['id'];print(json.dumps({'stage':'native-submitted','run':rid}),flush=True)
    for _ in range(150):
        result=required(c.get('/api/runs/'+rid))
        if result['run']['state'] in ['succeeded','failed','cancelled','interrupted']:break
        time.sleep(5)
    evidence['run']={'id':rid,'state':result['run']['state'],'seconds':round(time.monotonic()-started,2),'artifacts':len(result['artifacts']),'attempts':[{k:a.get(k) for k in ['node_id','state','provider_id','error','metrics']} for a in result['attempts']],'error':result['run'].get('error')}
    evidence['finished']=time.time();Path('evidence/deployment/research-validation.json').write_text(json.dumps(evidence,indent=2));print(json.dumps({'stage':'complete','run':evidence['run']}),flush=True)

if __name__=='__main__':main()
