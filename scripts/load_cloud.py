"""Explicitly authorized synthetic-user load against the HTTPS control plane.

Sessions are temporary and remain private. This measures interactive reads,
not concurrent whole-genome jobs or clinical scientific correctness.
No accounts or sessions are created unless --allow-synthetic-users COUNT is
supplied. This is an operator validation script, never a deployment step.
"""
import argparse,asyncio,collections,hashlib,json,math,random,time,uuid
from pathlib import Path
import httpx
from backend.config import aws_session

BASE='https://d2j3o1re9v00qo.cloudfront.net'
def prepare(*,allow_synthetic_users=None):
    if type(allow_synthetic_users) is not int or not 1<=allow_synthetic_users<=100:
        raise ValueError('Explicit authorization is required: --allow-synthetic-users COUNT (1–100). This creates persistent synthetic test accounts; it must not run on an admin-only deployment without approval.')
    checkpoint='operations/checkpoints/validation/load-sessions-'+uuid.uuid4().hex+'.json'
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
 for i in range(AUTHORIZED_COUNT):
  email='load-fixture-20261020-'+str(i)+'@openngs.invalid';row=c.execute('SELECT id FROM users WHERE email=?',(email,)).fetchone()
  if row:uid=row['id']
  else:
   uid=db.ident('u_');c.execute('INSERT INTO users(id,email,password,role,tier,created) VALUES(?,?,?,?,?,?)',(uid,email,password,'user','explore',time.time()))
  token,csrf=security.issue_session(c,uid);c.execute('UPDATE sessions SET expires=? WHERE token_hash=?',(time.time()+1200,__import__('hashlib').sha256(token.encode()).hexdigest()));fixtures.append({'token':token,'csrf':csrf,'uid':uid})
 db.audit(c,'validation-operator','load.fixtures.created','interactive-load',{'synthetic_users':AUTHORIZED_COUNT,'session_lifetime_seconds':1200,'explicit_operator_opt_in':True})
aws_session().client('s3').put_object(Bucket=os.environ['NGS_BUCKET'],Key=SESSION_CHECKPOINT,Body=json.dumps(fixtures).encode(),ServerSideEncryption='aws:kms',SSEKMSKeyId=os.environ['NGS_KMS_KEY'])
close()
print('Authorized temporary synthetic user sessions prepared')
REMOTE"""
    remote=remote.replace('AUTHORIZED_COUNT',str(allow_synthetic_users)).replace('SESSION_CHECKPOINT',repr(checkpoint))
    r=s.client('ssm').send_command(InstanceIds=[o['HostId']],DocumentName='AWS-RunShellScript',Parameters={'commands':[remote],'executionTimeout':['60']},Comment='OpenNGS prepare bounded interactive load validation')
    for _ in range(30):
        try:v=s.client('ssm').get_command_invocation(CommandId=r['Command']['CommandId'],InstanceId=o['HostId'])
        except s.client('ssm').exceptions.InvocationDoesNotExist:time.sleep(1);continue
        if v['Status']=='Success':break
        if v['Status'] in ['Failed','TimedOut','Cancelled']:raise RuntimeError('Synthetic load preparation failed')
        time.sleep(1)
    else:raise TimeoutError('Synthetic load preparation did not finish; no load requests were sent')
    obj=s.client('s3').get_object(Bucket=bucket,Key=checkpoint)
    fixtures=json.loads(obj['Body'].read())
    if len(fixtures)!=allow_synthetic_users:raise ValueError('Synthetic session count does not match the authorized count')
    return fixtures,{'host':o['HostId'],'bucket':bucket,'checkpoint':checkpoint,'version':obj.get('VersionId')}

def cleanup(fixtures,context):
    """Revoke only this invocation's test sessions; preserve accounts/audit."""
    hashes=[{'uid':f['uid'],'hash':hashlib.sha256(f['token'].encode()).hexdigest()} for f in fixtures]
    remote="""set -a; source /var/lib/openngs/runtime.env; set +a
cd /opt/openngs
/opt/openngs/runtime/bin/python - <<'REMOTE'
import json,time
from backend import db
from backend.postgres import close
rows=json.loads(SESSION_HASHES)
with db.tx() as c:
 for row in rows:c.execute('DELETE FROM sessions WHERE user_id=? AND token_hash=?',(row['uid'],row['hash']))
 db.audit(c,'validation-operator','load.sessions.revoked','interactive-load',{'sessions':len(rows)})
close()
print('Temporary load sessions revoked')
REMOTE""".replace('SESSION_HASHES',repr(json.dumps(hashes)))
    s=aws_session();ssm=s.client('ssm')
    result=ssm.send_command(InstanceIds=[context['host']],DocumentName='AWS-RunShellScript',
        Parameters={'commands':[remote],'executionTimeout':['60']},Comment='OpenNGS revoke exact load-validation sessions')
    for _ in range(30):
        try:status=ssm.get_command_invocation(CommandId=result['Command']['CommandId'],InstanceId=context['host'])['Status']
        except ssm.exceptions.InvocationDoesNotExist:time.sleep(1);continue
        if status=='Success':break
        if status in ['Failed','TimedOut','Cancelled']:raise RuntimeError('Load-session revocation failed; sessions automatically expire after 20 minutes')
        time.sleep(1)
    else:raise TimeoutError('Load-session revocation unconfirmed; sessions automatically expire after 20 minutes')
    args={'Bucket':context['bucket'],'Key':context['checkpoint']}
    if context.get('version'):args['VersionId']=context['version']
    s.client('s3').delete_object(**args)

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

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-synthetic-users',type=int,metavar='COUNT',
        help='Explicit permission to create/reuse 1–100 persistent synthetic accounts and temporary sessions. Omit to prevent all AWS activity.')
    args=parser.parse_args(argv)
    if args.allow_synthetic_users is None or not 1<=args.allow_synthetic_users<=100:
        parser.error('No load run was started. Explicit --allow-synthetic-users COUNT (1–100) is required; do not use on an admin-only deployment.')
    fixtures,context=prepare(allow_synthetic_users=args.allow_synthetic_users)
    print(json.dumps({'users_ready':len(fixtures)}),flush=True)
    result={'url':BASE,'started':time.time(),'phases':[]}
    try:
        for count,seconds in [(min(10,len(fixtures)),20),(len(fixtures),60)]:
            p=asyncio.run(phase(fixtures[:count],seconds));result['phases'].append(p);print(json.dumps(p),flush=True);Path('evidence/deployment/interactive-load.json').write_text(json.dumps(result,indent=2))
    finally:cleanup(fixtures,context)

if __name__=='__main__':main()
