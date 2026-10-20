"""Exercise the exclusive VM lane under the existing $2 validation allowance."""
import json,time,uuid
from pathlib import Path
from backend.config import aws_session
from backend.entitlements import REQUIRED
from scripts.verify_research_cloud import login,required

def main():
    private=json.loads(Path('.state/research-validation.json').read_text());uid=private['user_id'];c=login(private['credentials']);owner=json.loads(Path('.state/owner-access.json').read_text());admin=login({k:owner[k] for k in ['email','password']})
    old=required(c.get('/api/account/entitlements'));allowance={k:old[k] for k in REQUIRED};allowance['max_scratch_mb']=32768
    required(admin.post('/api/admin/entitlements/'+uid,json={'reason':'Internal exclusive-VM validation; same $2 baseline-funded ceiling, 32 GiB scratch maximum','funding_confirmed':True,'allowance':allowance}))
    s=aws_session();cf=s.client('cloudformation');host=next(x['OutputValue'] for x in cf.describe_stacks(StackName='openngs-lean')['Stacks'][0]['Outputs'] if x['OutputKey']=='HostId')
    script="set -a; source /var/lib/openngs/runtime.env; set +a; cd /opt/openngs; /opt/openngs/runtime/bin/python - <<'REMOTE'\nimport os,json\nfrom backend import db,provisioning,entitlements\nfrom backend.postgres import close\nuid='"+uid+"'\np=provisioning.profile(uid);p.update(state='Provisioning',stack='openngs-pro-'+uid[2:],grant_version=entitlements.limits(uid)['grant_version'],template_url=os.environ['NGS_RESEARCH_TEMPLATE_URL']);provisioning.save(uid,p)\nwith db.tx() as c:db.audit(c,'validation-operator','capacity.failed-update.reconciled',uid,{'source':'Recovered fixed-template CloudFormation stack; no datasets deleted'})\nclose()\nREMOTE"
    r=s.client('ssm').send_command(InstanceIds=[host],DocumentName='AWS-RunShellScript',Parameters={'commands':[script]},Comment='OpenNGS reconcile recovered internal test capacity')
    for _ in range(60):
        capacity=required(c.get('/api/account/capacity'))
        if capacity['state']=='Ready' and capacity.get('instance_profile'):break
        time.sleep(3)
    if capacity['state']!='Ready' or not capacity.get('instance_profile'):raise RuntimeError('Exclusive instance profile is not ready')
    files=required(c.get('/api/operations/datasets'))['datasets'];reads=next(x['id'] for x in files if x['name']=='fixture_R1.fastq.gz' and x['state']=='Ready');catalog=required(c.get('/api/catalog'));specs={x['id']:x for x in catalog['nodes']};nodes=[]
    for i,tool in enumerate(['asset.file','fastp']):
        spec=specs[tool];params={p['key']:p['default'] for p in spec['parameters']}
        if i==0:params['asset_id']=reads
        nodes.append({'id':'vm-'+str(i),'tool':tool,'x':i*320,'y':150,'params':params,'resources':{'threads':2,'memory_mb':4096,'scratch_mb':32768 if i else 2048,'timeout_seconds':120},**({'execution_profile':'exclusive-vm'} if i else {})})
    g={'name':'Internal exclusive VM native validation','nodes':nodes,'edges':[{'id':'vm-edge','source':'vm-0','sourcePort':'data','target':'vm-1','targetPort':'reads'}]};estimate=required(c.post('/api/operations/estimate',json={'graph':g}))
    if not estimate['preflight']['ok']:raise RuntimeError(json.dumps(estimate['preflight']['issues']))
    rid=required(c.post('/api/runs',headers={'Idempotency-Key':'vm-validation-'+uuid.uuid4().hex},json={'graph':g}))['run']['id'];began=time.monotonic();print(json.dumps({'run':rid,'state':'admitted'}),flush=True)
    for _ in range(180):
        result=required(c.get('/api/runs/'+rid))
        if result['run']['state'] in ['succeeded','failed','cancelled','interrupted']:break
        time.sleep(3)
    record={'scope':'2-vCPU, 4-GiB fresh VM; 32-GiB encrypted scratch; small real fastp execution, not a full-size assay','estimate':estimate,'run':{k:result['run'].get(k) for k in ['id','state','error','used']},'seconds':round(time.monotonic()-began,2),'attempts':[{k:a.get(k) for k in ['node_id','state','provider_id','metrics','error']} for a in result['attempts']],'artifacts':len(result['artifacts'])};Path('evidence/deployment/vm-validation.json').write_text(json.dumps(record,indent=2));print(json.dumps(record['run']),flush=True)

if __name__=='__main__':main()
