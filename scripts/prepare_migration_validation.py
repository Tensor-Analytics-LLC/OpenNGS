"""Migrate three tiny engineering fixtures for scoped cross-bucket execution.

Uses only the existing internal validation tenant. The ordinary immutable plan,
raw cloud checksum verification and alias registration paths are exercised.
"""
import base64
import hashlib
import json
from pathlib import Path
import time
from botocore.exceptions import ClientError
from backend.config import aws_session

def main():
    secret=json.loads(Path('.state/research-validation.json').read_text())
    if not secret['credentials']['email'].endswith('@openngs.invalid'):
        raise ValueError('Only the internal synthetic validation tenant may be used')
    uid=secret['user_id'];release=json.loads(Path('evidence/deployment/release.json').read_text())
    session=aws_session();s3=session.client('s3');objects=[]
    paths=[Path('demo-data/sample.fastq')/name for name in ['fixture_R1.fastq.gz','fixture_R2.fastq.gz','engineering.fa']]
    for i,path in enumerate(paths):
        body=path.read_bytes();sha=hashlib.sha256(body).hexdigest()
        did='ds_migration_'+hashlib.sha256((uid+':'+sha).encode()).hexdigest()[:40]
        aid='asset_migration_'+hashlib.sha256((uid+':'+path.name).encode()).hexdigest()[:32]
        meta={'provenance':'Repository engineering fixture; migration/worker integration only'}
        if i==0:meta['mate']=str(paths[1].resolve())
        alias={'id':aid,'user_id':uid,'path':str(path.resolve()),'name':'Migration validation '+path.name,
            'format':'FASTQ_PAIR' if i==0 else 'FASTQ' if i==1 else 'FASTA','bytes':len(body),
            'mtime':path.stat().st_mtime,'family':'sample' if i<2 else 'reference',
            'assembly':'engineering','metadata':meta,'mtime_ns':path.stat().st_mtime_ns}
        objects.append({'sha256':sha,'bytes':len(body),'dataset_id':did,'key':f'tenants/{uid}/datasets/{did}/{path.name}',
                        'aliases':[alias],'name':path.name})
    plan={'schema':1,'owner_id':uid,'bucket':release['bucket'],'kms':release['kms'],
          'objects':objects,'total_bytes':sum(o['bytes'] for o in objects),
          'source_records_preserved':True,'scope':'Small synthetic migration qualification, not user-source migration completion'}
    encode=lambda x:json.dumps(x,sort_keys=True,separators=(',',':')).encode()
    plan['plan_id']='migration_'+hashlib.sha256(encode(plan)).hexdigest()[:24]
    def put(key,body,sha):
        try:
            return s3.put_object(Bucket=release['bucket'],Key=key,Body=body,IfNoneMatch='*',
                ServerSideEncryption='aws:kms',SSEKMSKeyId=release['kms'],BucketKeyEnabled=True,
                ChecksumSHA256=base64.b64encode(bytes.fromhex(sha)).decode(),Metadata={'sha256':sha})
        except ClientError as error:
            if error.response['Error']['Code']!='PreconditionFailed':raise
            head=s3.head_object(Bucket=release['bucket'],Key=key)
            if head['ContentLength']!=len(body) or head.get('Metadata',{}).get('sha256')!=sha:
                raise ValueError('Existing immutable test object differs')
            return head
    raw=encode(plan);key=f"operations/checkpoints/migration/{plan['plan_id']}/plan.json"
    plan_version=put(key,raw,hashlib.sha256(raw).hexdigest())['VersionId']
    receipts=[]
    for path,obj in zip(paths,objects):
        uploaded=put(obj['key'],path.read_bytes(),obj['sha256'])
        receipt={'plan_id':plan['plan_id'],'sha256':obj['sha256'],'key':obj['key'],
                 'bytes':obj['bytes'],'version_id':uploaded['VersionId']}
        data=encode(receipt);put(f"operations/checkpoints/migration/{plan['plan_id']}/uploaded/{obj['sha256']}.json",data,hashlib.sha256(data).hexdigest())
        receipts.append(receipt)
    registration={'plan_id':plan['plan_id'],'owner_id':uid,'bucket':release['bucket'],'key':key,
                  'version_id':plan_version,'sha256':hashlib.sha256(raw).hexdigest()}
    # Secrets never enter command text. Only public fixture identity and the
    # already-authorized internal tenant ID are sent to the application host.
    script="""set -a
source /var/lib/openngs/runtime.env
set +a
cd /opt/openngs
/opt/openngs/runtime/bin/python - <<'PYREMOTE'
import hashlib,json
from backend import migration,db
from backend.postgres import close
registration=REGISTRATION
migration.register_plan('internal-validation-operator',registration)
plan=migration.load_plan(registration)
migration.discover(500)
receipts=RECEIPTS
for obj,receipt in zip(plan['objects'],receipts):
    result=migration.client().get_object(Bucket=plan['bucket'],Key=obj['key'],VersionId=receipt['version_id'])
    body=result['Body'].read(obj['bytes']+1)
    result['Body'].close()
    if len(body)!=obj['bytes']:raise ValueError('Raw fixture length mismatch')
    migration.import_manifest(plan,obj,receipt,hashlib.sha256(body).hexdigest(),'raw-cloud-sha256')
for obj in plan['objects']:
    row=db.query('SELECT * FROM assets WHERE id=? AND user_id=?',(obj['aliases'][0]['id'],plan['owner_id']),one=True)
    resolved=migration.resolve_asset(plan['owner_id'],row)
    if json.loads(resolved['metadata']).get('migration_companions_pending'):raise ValueError('Migration companion not ready')
print(json.dumps({'plan_id':plan['plan_id'],'state':'Ready','objects':len(plan['objects']),'bytes':plan['total_bytes']}))
close()
PYREMOTE
""".replace('REGISTRATION',repr(registration)).replace('RECEIPTS',repr(receipts))
    stack=session.client('cloudformation').describe_stacks(StackName='openngs-lean')['Stacks'][0]
    host=next(x['OutputValue'] for x in stack['Outputs'] if x['OutputKey']=='HostId')
    response=session.client('ssm').send_command(InstanceIds=[host],DocumentName='AWS-RunShellScript',
        Parameters={'commands':[script]},Comment='Verify exact tiny synthetic migration sources for scoped worker integration')
    command=response['Command']['CommandId']
    for _ in range(60):
        try:invocation=session.client('ssm').get_command_invocation(CommandId=command,InstanceId=host)
        except session.client('ssm').exceptions.InvocationDoesNotExist:time.sleep(1);continue
        if invocation['Status'] in ['Success','Failed','TimedOut','Cancelled']:break
        time.sleep(1)
    if invocation['Status']!='Success':
        raise RuntimeError('Synthetic migration registration failed: '+invocation.get('StandardErrorContent','')[-1600:])
    record={'plan_id':plan['plan_id'],'owner_id':uid,'bytes':plan['total_bytes'],
            'reads':objects[0]['aliases'][0]['id'],'reference':objects[2]['aliases'][0]['id'],
            'source_bucket':release['bucket'],'objects':[
                {'dataset_id':o['dataset_id'],'sha256':o['sha256'],'version_id':r['version_id'],'bytes':o['bytes']}
                for o,r in zip(objects,receipts)],'state':'Ready'}
    Path('evidence/deployment/migration-validation-sources.json').write_text(json.dumps(record,indent=2))
    print(json.dumps({'plan_id':record['plan_id'],'state':'Ready','objects':3,'bytes':record['bytes']}))

if __name__=='__main__':main()
