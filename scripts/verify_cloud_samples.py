"""Real public-endpoint integration probes; no credentials or signed URLs in output."""
import json,time,uuid,os
from pathlib import Path
import httpx
root=Path.cwd();base=os.getenv('NGS_VERIFY_URL','https://d2j3o1re9v00qo.cloudfront.net')
secret=json.loads(Path(os.getenv('NGS_VERIFY_CREDENTIAL_FILE','.state/owner-access.json')).read_text())
client=httpx.Client(base_url=base,headers={'Origin':base,'X-NGS-Client':'studio'},timeout=30)
r=client.post('/api/auth/login',json={'email':secret['email'],'password':secret['password']});r.raise_for_status();client.headers['X-CSRF-Token']=r.json()['csrf']
catalog=client.get('/api/catalog').json();templates={x['id']:x for x in catalog['templates']};evidence={'url':base,'started':time.time(),'catalogue_count':len(catalog['nodes']),'templates':list(templates),'runs':[]}
for ident in ['quickstart','giab','clinvar','pileup','native-qc','dna-engineering']:
 if ident not in templates:print(json.dumps({'missing_template':ident}),flush=True);continue
 g=templates[ident]['graph'];r=client.post('/api/runs',headers={'Idempotency-Key':'cloud-verify-'+uuid.uuid4().hex},json={'graph':g})
 if r.status_code!=200:print(json.dumps({'template':ident,'admission_status':r.status_code,'detail':r.json() if r.headers.get('content-type','').startswith('application/json') else r.text[:200]}),flush=True);continue
 rid=r.json()['run']['id'];began=time.monotonic();print(json.dumps({'template':ident,'run':rid,'state':'accepted'}),flush=True)
 last=None
 while time.monotonic()-began<1800:
  response=client.get('/api/runs/'+rid);response.raise_for_status();result=response.json();state=result['run']['state']
  if state!=last:print(json.dumps({'run':rid,'state':state}),flush=True);last=state
  if state in ['succeeded','failed','cancelled','interrupted']:break
  time.sleep(5)
 entry={'template':ident,'run':rid,'state':state,'elapsed_seconds':round(time.monotonic()-began,3),'allocated_cpu_seconds':result['run']['used'],'artifacts':len(result['artifacts']),'error':result['run'].get('error')}
 evidence['runs'].append(entry);(root/'evidence/deployment/cloud-samples.json').write_text(json.dumps(evidence,indent=2));print(json.dumps(entry),flush=True)
 if state!='succeeded':break
 if ident=='clinvar':
  aid=next(a['id'] for a in result['artifacts'] if a['format']=='VCF');r=client.post('/api/results/'+aid+'/index',json={});r.raise_for_status()
  for _ in range(60):
   status=client.get('/api/results/'+aid+'/status').json()
   if status['state'] in ['ready','failed']:break
   time.sleep(1)
  evidence['clinvar_index']=status
  if status['state']=='ready':
   rows=client.post('/api/results/'+aid+'/query',json={'filters':{'classification':'VUS'},'limit':100});rows.raise_for_status();evidence['clinvar_rows']={k:rows.json()[k] for k in ['total','matched']}
  (root/'evidence/deployment/cloud-samples.json').write_text(json.dumps(evidence,indent=2))
print(json.dumps({'verification_complete':True,'runs':len(evidence['runs'])}),flush=True)
