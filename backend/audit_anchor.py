"""WORM checkpoints of the audit chain; raw personal audit bodies remain in RDS."""
import hashlib,json,os,time
from datetime import datetime,timezone
from . import db
from .config import aws_session

def anchor():
    bucket=os.getenv('NGS_AUDIT_BUCKET')
    if not bucket:return {'state':'Unavailable','reason':'Immutable archive has not been configured'}
    row=db.query('SELECT seq,hash FROM audit ORDER BY seq DESC LIMIT 1',one=True)
    if not row:return {'state':'Empty'}
    last=db.query("SELECT value FROM controls WHERE key='audit:last-anchor'",one=True)
    if last and json.loads(last['value'])['seq']==row['seq']:return json.loads(last['value'])
    key='anchors/'+str(row['seq']).zfill(20)+'-'+row['hash']+'.json';payload=db.encode({'schema':1,'seq':row['seq'],'head':row['hash'],'at':time.time()}).encode();s3=aws_session().client('s3')
    try:r=s3.put_object(Bucket=bucket,Key=key,Body=payload,IfNoneMatch='*',Metadata={'sha256':hashlib.sha256(payload).hexdigest()})
    except s3.exceptions.ClientError as e:
        if e.response['Error']['Code']!='PreconditionFailed':raise
    head=s3.head_object(Bucket=bucket,Key=key);version=head['VersionId']
    if head.get('ObjectLockMode')!='GOVERNANCE' or not head.get('ObjectLockRetainUntilDate'):raise ValueError('Audit object lacks verified WORM retention')
    record={'state':'Anchored','seq':row['seq'],'head':row['hash'],'bucket':bucket,'key':key,'version_id':version,'retained_until':head['ObjectLockRetainUntilDate'].isoformat(),'scope':'Hash-chain checkpoint; governance bypass is not granted to the application or scientific workers'}
    with db.tx() as c:c.execute("INSERT INTO controls VALUES('audit:last-anchor',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(db.encode(record),))
    return record

def verify():
    last=db.query("SELECT value FROM controls WHERE key='audit:last-anchor'",one=True)
    if not last:return {'valid':False,'state':'Awaiting first checkpoint'}
    a=json.loads(last['value']);s3=aws_session().client('s3');obj=s3.get_object(Bucket=a['bucket'],Key=a['key'],VersionId=a['version_id']);body=obj['Body'].read(2049);obj['Body'].close()
    if len(body)>2048:raise ValueError('Unexpected audit checkpoint size')
    payload=json.loads(body);row=db.query('SELECT hash FROM audit WHERE seq=?',(payload['seq'],),one=True)
    retained=obj.get('ObjectLockRetainUntilDate')
    valid=bool(row and row['hash']==payload['head'] and payload['seq']==a['seq'] and payload['head']==a['head'] and hashlib.sha256(body).hexdigest()==obj.get('Metadata',{}).get('sha256') and obj.get('ObjectLockMode')=='GOVERNANCE' and retained and retained>datetime.now(timezone.utc))
    return {'valid':valid,'checkpoint':a,'note':'Verification covers this immutable chain prefix. AWS administrators with governance-bypass permission remain privileged; independent archive review and full CloudTrail data-event coverage are separate controls.'}

if __name__=='__main__':
    while True:
        try:print(db.encode({'event':'audit.checkpoint','result':anchor()}),flush=True)
        except Exception as e:print(db.encode({'event':'audit.checkpoint.retry','exception':type(e).__name__}),flush=True)
        time.sleep(300)
