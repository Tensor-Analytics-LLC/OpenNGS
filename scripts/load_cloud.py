"""100 independent synthetic users against the real HTTPS control plane.

Sessions are temporary and remain private. This measures interactive reads,
not concurrent whole-genome jobs or clinical scientific correctness.
"""
import asyncio,collections,json,math,random,time
from pathlib import Path
import httpx
from backend.config import aws_session

BASE='https://d2j3o1re9v00qo.cloudfront.net'
def prepare():
    s=aws_session();o={x['OutputKey']:x['OutputValue'] for x in s.client('cloudformation').describe_stacks(StackName='openngs-lean')['Stacks'][0]['Outputs']};bucket=json.loads(Path('evidence/deployment/release.json').read_text())['bucket']
    remote="""set -a; source /var/lib/openngs/runtime.env; set +a
cd /opt/openngs
/opt/openngs/runtime/bin/python - <<'REMOTE'
import json,secrets,time
from backend import db,security
from backend.config import aws_session
from backend.postgres import close
import os
fixtures=[];password=security.password_hash(secrets.token_urlsafe(30))
with db.tx() as c:
 for i in range(100):
  email='load-fixture-20261020-'+str(i)+'@openngs.invalid';row=c.execute('SELECT id FROM users WHERE email=?',(email,)).fetchone()
  if row:uid=row['id']
  else:
   uid=db.ident('u_');c.execute('INSERT INTO users(id,email,password,role,tier,created) VALUES(?,?,?,?,?,?)',(uid,email,password,'user','explore',time.time()))
  token,csrf=security.issue_session(c,uid);c.execute('UPDATE sessions SET expires=? WHERE token_hash=?',(time.time()+1200,__import__('hashlib').sha256(token.encode()).hexdigest()));fixtures.append({'token':token,'csrf':csrf,'uid':uid})
 db.audit(c,'validation-operator','load.fixtures.created','interactive-100',{'synthetic_users':100,'session_lifetime_seconds':1200})
aws_session().client('s3').put_object(Bucket=os.environ['NGS_BUCKET'],Key='operations/checkpoints/validation/load-sessions.json',Body=json.dumps(fixtures).encode(),ServerSideEncryption='aws:kms',SSEKMSKeyId=os.environ['NGS_KMS_KEY'])
close()
print('100 temporary synthetic user sessions prepared')
REMOTE"""
    r=s.client('ssm').send_command(InstanceIds=[o['HostId']],DocumentName='AWS-RunShellScript',Parameters={'commands':[remote],'executionTimeout':['60']},Comment='OpenNGS prepare bounded interactive load validation')
    for _ in range(30):
        try:v=s.client('ssm').get_command_invocation(CommandId=r['Command']['CommandId'],InstanceId=o['HostId'])
        except s.client('ssm').exceptions.InvocationDoesNotExist:time.sleep(1);continue
        if v['Status']=='Success':break
        if v['Status'] in ['Failed','TimedOut','Cancelled']:raise RuntimeError('Synthetic load preparation failed')
        time.sleep(1)
    obj=s.client('s3').get_object(Bucket=bucket,Key='operations/checkpoints/validation/load-sessions.json');return json.loads(obj['Body'].read())

async def phase(fixtures,seconds):
    samples=[];start=time.monotonic();rng=random.Random(200)
    async with httpx.AsyncClient(base_url=BASE,timeout=12,limits=httpx.Limits(max_connections=120,max_keepalive_connections=120)) as c:
        async def visitor(fixture,index):
            await asyncio.sleep(index*.025)
            paths=['/api/session','/api/graphs','/api/catalog','/api/account/entitlements']
            while time.monotonic()-start<seconds:
                path=paths[rng.randrange(len(paths))];began=time.monotonic()
                try:
                    r=await c.get(path,headers={'Cookie':'ngs_session='+fixture['token']});code=r.status_code
                except httpx.HTTPError:code='network-error'
                samples.append((code,round((time.monotonic()-began)*1000,2)))
                await asyncio.sleep(1.5+rng.random()*1.5)
        await asyncio.gather(*(visitor(f,i) for i,f in enumerate(fixtures)))
    latency=sorted(t for code,t in samples if code==200)
    def p(x):return latency[min(len(latency)-1,math.ceil(x*len(latency))-1)] if latency else None
    return {'independent_users':len(fixtures),'seconds':round(time.monotonic()-start,2),'requests':len(samples),'statuses':dict(collections.Counter(str(x[0]) for x in samples)),'successful_latency_ms':{'p50':p(.5),'p95':p(.95),'p99':p(.99),'maximum':max(latency) if latency else None},'scope':'Authenticated session, workflow list, catalogue and allowance reads with human-like think time; not 100 simultaneous genomic analyses'}

def main():
    fixtures=prepare();print(json.dumps({'users_ready':len(fixtures)}),flush=True)
    result={'url':BASE,'started':time.time(),'phases':[]}
    for count,seconds in [(10,20),(100,60)]:
        p=asyncio.run(phase(fixtures[:count],seconds));result['phases'].append(p);print(json.dumps(p),flush=True);Path('evidence/deployment/interactive-load.json').write_text(json.dumps(result,indent=2))

if __name__=='__main__':main()
