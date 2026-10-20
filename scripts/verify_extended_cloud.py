"""Exercise newly published tool routing on the existing internal test tenant.

No allowance is increased here. A failed admission leaves the recorded estimate
for operator review. Uses existing verified synthetic data; no local genomics
installation, personal data upload or full-size assay inference is involved.
"""
import hashlib
import csv
import io
import argparse
import json
from pathlib import Path
import time
import uuid
from scripts.verify_research_cloud import login,required
from backend.config import aws_session

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--migrated',action='store_true',help='Use exact verified cross-bucket engineering fixtures')
    parser.add_argument('--estimate-only',action='store_true',help='Validate existing allowances and source grants without submitting work')
    args=parser.parse_args()
    secret=json.loads(Path('.state/research-validation.json').read_text())
    if not secret['credentials']['email'].endswith('@openngs.invalid'):
        raise ValueError('Only the internal synthetic validation tenant may be used')
    c=login(secret['credentials']);capacity=required(c.get('/api/account/capacity'))
    needed={'bowtie2','trimmomatic','multiqc','salmon'}
    if capacity.get('state')!='Ready' or not needed.issubset(capacity.get('runtime_profiles',{})):
        raise ValueError('The qualified extension is not published for the internal validation tenant')
    datasets=required(c.get('/api/operations/datasets'))['datasets']
    def find(path):
        digest=hashlib.sha256(Path(path).read_bytes()).hexdigest()
        rows=[r for r in datasets if r['state']=='Ready' and r['sha256']==digest]
        if not rows:raise ValueError('Missing exact previously ingested engineering fixture: '+str(path))
        return rows[0]['id']
    reads=find('demo-data/sample.fastq/fixture_R1.fastq.gz')
    reference=find('demo-data/sample.fastq/engineering.fa')
    migration=None
    if args.migrated:
        migration=json.loads(Path('evidence/deployment/migration-validation-sources.json').read_text())
        if migration['owner_id']!=secret['user_id'] or migration['state']!='Ready' or migration['source_bucket']==capacity['bucket']:
            raise ValueError('Cross-bucket validation requires the exact Ready synthetic tenant sources')
        reads=migration['reads'];reference=migration['reference']
    specs={n['id']:n for n in required(c.get('/api/catalog'))['nodes']}
    tools=['asset.file','reference.asset','trimmomatic','bowtie2','fastqc','multiqc','salmon']
    nodes=[]
    for i,tool in enumerate(tools):
        params={p['key']:p['default'] for p in specs[tool]['parameters']}
        if i==0:params['asset_id']=reads
        if i==1:params.update(asset_id=reference,assembly='engineering' if migration else 'custom')
        nodes.append({'id':'n'+str(i),'tool':tool,'params':params,'resources':{'threads':2,'memory_mb':4096,'scratch_mb':2048,'timeout_seconds':120},'x':i*260,'y':160+(i%2)*160})
    links=[(0,'data',2,'reads'),(2,'reads',3,'reads'),(1,'reference',3,'reference'),
           (0,'data',4,'reads'),(4,'report',5,'reports'),(0,'data',6,'reads'),(1,'reference',6,'reference')]
    g={'name':'Internal extension routing qualification on 1,000 engineering read pairs','maximum_output_bytes':32*1024**2,
       'nodes':nodes,'edges':[{'id':'e'+str(i),'source':'n'+str(a),'sourcePort':ap,'target':'n'+str(b),'targetPort':bp} for i,(a,ap,b,bp) in enumerate(links)]}
    estimate=required(c.post('/api/operations/estimate',json={'graph':g}))
    evidence={'scope':'Real tenant-isolated AWS execution on 1,000 synthetic read pairs and exact 20-kb engineering reference; not representative assay accuracy.',
              'estimate':estimate,'started':time.time()}
    if migration:evidence['migration_sources']=migration
    destination=Path('evidence/deployment/extended-cloud-validation.json')
    destination.write_text(json.dumps(evidence,indent=2))
    if not estimate['preflight']['ok'] or not estimate['fits_allowance']:
        raise ValueError('Existing allowance cannot admit this qualification; no job was submitted')
    if args.estimate_only:
        print(json.dumps({'preflight':estimate['preflight']['ok'],'fits_allowance':estimate['fits_allowance'],
                          'estimate':estimate['estimate'],'cross_bucket':bool(migration)}))
        return
    run=required(c.post('/api/runs',headers={'Idempotency-Key':'extended-'+uuid.uuid4().hex},json={'graph':g}))['run']
    evidence['run_id']=run['id'];print(json.dumps({'run_id':run['id'],'stage':'admitted'}),flush=True)
    last=None
    for _ in range(600):
        result=required(c.get('/api/runs/'+run['id']))
        status=[(r['node_id'],r['state']) for r in result['attempts']]
        if status!=last:print(json.dumps({'attempts':status}),flush=True);last=status
        evidence.update(state=result['run']['state'],error=result['run'].get('error'),
                        attempts=[{k:a.get(k) for k in ['node_id','state','provider_id','metrics','error']} for a in result['attempts']],
                        artifacts=[{k:a.get(k) for k in ['node_id','name','format','bytes','digest']} for a in result['artifacts']])
        destination.write_text(json.dumps(evidence,indent=2))
        if result['run']['state'] in {'succeeded','failed','cancelled','interrupted'}:break
        time.sleep(3)
    if result['run']['state']=='succeeded':
        sam=next(a for a in result['artifacts'] if a['node_id']=='n3' and a['name']=='aligned.sam')
        body=c.get('/api/artifacts/'+sam['id'],follow_redirects=True)
        body.raise_for_status()
        rows=[line.split('\t') for line in body.text.splitlines() if line and not line.startswith('@')]
        mapped=sum(not (int(row[1])&4) for row in rows)
        if len(rows)!=2000 or mapped==0:raise AssertionError('Cloud alignment did not preserve 2,000 read records with mapped reads')
        quant=next(a for a in result['artifacts'] if a['node_id']=='n6' and a['name']=='quant.sf')
        qbody=c.get('/api/artifacts/'+quant['id'],follow_redirects=True);qbody.raise_for_status()
        quantified=sum(float(r['NumReads']) for r in csv.DictReader(io.StringIO(qbody.text),delimiter='\t'))
        if quantified<=0 or quantified>1000:raise AssertionError('Salmon did not quantify the engineering read fragments')
        provider_ids=[a['provider_id'] for a in result['attempts'] if a.get('provider_id')]
        jobs=aws_session().client('batch').describe_jobs(jobs=provider_ids)['jobs']
        expected={capacity['runtime_profiles'][tool]['image'] for tool in needed}
        extension_ids={a['provider_id'] for a in result['attempts'] if a['node_id'] in {'n2','n3','n5','n6'}}
        actual=[job for job in jobs if job['jobId'] in extension_ids]
        if len(actual)!=4 or any(job['status']!='SUCCEEDED' or job['container']['image'] not in expected for job in actual):
            raise AssertionError('Four extension nodes must execute in the exact published immutable runtime')
        evidence['worker_jobs']=[{'job_id':job['jobId'],'image':job['container']['image'],
            'resources':job['container'].get('resourceRequirements'), 'started':job.get('startedAt'),
            'stopped':job.get('stoppedAt'),'status':job['status']} for job in jobs]
        evidence['assertions']={'input_pairs':1000,'alignment_records':len(rows),'mapped_records':mapped,
                                'salmon_assigned_fragments':quantified,'immutable_extension_jobs':len(actual),
                                'published_runtime_family':'extended-cpu-v1'}
    evidence['elapsed_seconds']=round(time.time()-evidence['started'],3)
    destination.write_text(json.dumps(evidence,indent=2))
    print(json.dumps({'state':evidence['state'],'run_id':run['id'],'error':evidence.get('error'),'assertions':evidence.get('assertions')}),flush=True)

if __name__=='__main__':main()
