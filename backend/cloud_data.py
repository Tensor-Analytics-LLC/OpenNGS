"""Durable dataset registry and resumable direct-to-S3 multipart transfers."""
import hashlib
import json
import math
import re
import time
from pathlib import Path
from botocore.config import Config
from . import budget,data,db,entitlements
from .config import aws_session,CLOUD

PART=16*1024*1024
def client():return aws_session().client('s3',config=Config(signature_version='s3v4',connect_timeout=5,read_timeout=30,retries={'mode':'adaptive','max_attempts':3}))
def initialize():
    with db.tx() as c:
        c.execute('CREATE TABLE IF NOT EXISTS datasets(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,source_asset TEXT,name TEXT NOT NULL,bytes BIGINT NOT NULL,sha256 TEXT,state TEXT NOT NULL,s3_key TEXT,version_id TEXT,manifest TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL)')
        c.execute('CREATE INDEX IF NOT EXISTS datasets_user_state ON datasets(user_id,state)')
        c.execute('CREATE TABLE IF NOT EXISTS multipart_transfers(id TEXT PRIMARY KEY,dataset_id TEXT NOT NULL,user_id TEXT NOT NULL,upload_id TEXT,state TEXT NOT NULL,part_size BIGINT NOT NULL,source_identity TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,error TEXT)')

def owned(uid,tid):
    row=db.query('SELECT t.*,d.s3_key,d.bytes,d.sha256,d.manifest,d.name FROM multipart_transfers t JOIN datasets d ON d.id=t.dataset_id WHERE t.id=? AND t.user_id=?',(tid,uid),one=True)
    if not row:raise ValueError('Transfer not found')
    return row

def initiate(user,payload):
    uid=user['id'];s=data.settings(uid);name=str(payload.get('name',''))
    size=payload.get('bytes');sha=payload.get('sha256','');operation=payload.get('operation_id','')
    if user['frozen']:raise ValueError('Workspace is frozen')
    if entitlements.restricted(uid):raise ValueError('Explore cannot ingest personal datasets; a funded Research Pro allowance is required')
    if not s['s3_bucket'] or not s['kms_key_arn']:raise ValueError('Cloud storage is not configured')
    maximum=entitlements.limits(uid)['max_upload_bytes'] if CLOUD else 5*1024**4
    if not re.fullmatch(r'[A-Za-z0-9._ -]{1,160}',name) or type(size)!=int or not 0<size<=maximum or not re.fullmatch('[a-f0-9]{64}',sha) or not re.fullmatch('[a-zA-Z0-9_-]{12,100}',operation):raise ValueError('Provide name, SHA-256, stable operation ID and size within the upload allowance')
    # Idempotency is tenant-bound; never reuse an upload for changed source content.
    tid='mp_'+hashlib.sha256((uid+':'+operation).encode()).hexdigest()[:40]
    existing=db.query('SELECT id FROM multipart_transfers WHERE id=? AND user_id=?',(tid,uid),one=True)
    if existing:
        row=owned(uid,tid)
        if row['sha256']!=sha or row['bytes']!=size:raise ValueError('Source changed; use a new transfer ID')
        return status(uid,tid)
    dataset=db.ident('ds_');key=f'tenants/{uid}/datasets/{dataset}/{name}'
    with db.tx() as c:
        total=entitlements.storage_exposure(c,uid)
        total+=int(c.execute("SELECT COALESCE(SUM(bytes),0) FROM output_holds WHERE user_id=? AND state='reserved'",(uid,)).fetchone()[0])
        ceiling=entitlements.limits(uid)['max_storage_bytes'] if CLOUD else budget.policy()['max_storage_gib']*1024**3
        if total+size>ceiling:raise budget.BudgetFull('Assigned dataset storage ceiling reached')
        amount=.02 if entitlements.research(uid) else size/1024**3*.023+.02
        budget.reserve(c,uid,tid,'storage',amount,{'bytes':size,'key':key,'retention':'Research retained capacity is reserved separately per month; this transfer reserves requests and verification overhead'})
        c.execute('INSERT INTO datasets VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(dataset,uid,payload.get('asset_id'),name,size,sha,'Transferring',key,None,db.encode({'format':data.file_type(Path(name)),'source_identity':payload.get('source_identity',{}),'parts':[]}),time.time(),time.time()))
        part_size=max(PART,math.ceil(size/9999/(1024**2))*1024**2)
        c.execute('INSERT INTO multipart_transfers VALUES(?,?,?,?,?,?,?,?,?,?)',(tid,dataset,uid,None,'creating',part_size,db.encode(payload.get('source_identity',{})),time.time(),time.time(),None))
    try:
        upload=client().create_multipart_upload(Bucket=s['s3_bucket'],Key=key,ServerSideEncryption='aws:kms',SSEKMSKeyId=s['kms_key_arn'],Metadata={'sha256':sha,'tenant':uid,'dataset':dataset})
        with db.tx() as c:c.execute("UPDATE multipart_transfers SET upload_id=?,state='uploading',updated=? WHERE id=?",(upload['UploadId'],time.time(),tid))
    except Exception:
        with db.tx() as c:c.execute("UPDATE multipart_transfers SET state='action-required',error='Creation outcome needs cloud reconciliation',updated=? WHERE id=?",(time.time(),tid))
        raise
    return status(uid,tid)

def status(uid,tid):
    row=owned(uid,tid);parts=[]
    if row['state']=='uploading':
        kwargs={'Bucket':data.settings(uid)['s3_bucket'],'Key':row['s3_key'],'UploadId':row['upload_id']}
        while True:
            page=client().list_parts(**kwargs);parts.extend({'number':p['PartNumber'],'bytes':p['Size'],'etag':p['ETag']} for p in page.get('Parts',[]))
            if not page.get('IsTruncated'):break
            kwargs['PartNumberMarker']=page['NextPartNumberMarker']
    return {'id':tid,'dataset_id':row['dataset_id'],'state':row['state'],'bytes':row['bytes'],'sha256':row['sha256'],'part_size':row['part_size'],'parts':parts,'uploaded_bytes':sum(p['bytes'] for p in parts),'source_identity':json.loads(row['source_identity'])}

def sign_part(uid,tid,number):
    row=owned(uid,tid)
    if row['state']!='uploading' or not 1<=number<=math.ceil(row['bytes']/row['part_size']):raise ValueError('Transfer state or part number is invalid')
    length=min(row['part_size'],row['bytes']-(number-1)*row['part_size'])
    url=client().generate_presigned_url('upload_part',Params={'Bucket':data.settings(uid)['s3_bucket'],'Key':row['s3_key'],'UploadId':row['upload_id'],'PartNumber':number,'ContentLength':length},ExpiresIn=900)
    return {'url':url,'bytes':length,'expires_in':900}

def complete(uid,tid):
    row=owned(uid,tid)
    if row['state'] in ['verifying','complete']:return {'state':row['state'],'dataset_id':row['dataset_id']}
    s=data.settings(uid)
    if row['state']=='completing':
        try:
            head=client().head_object(Bucket=s['s3_bucket'],Key=row['s3_key'])
            if head['ContentLength']==row['bytes'] and head.get('Metadata',{}).get('sha256')==row['sha256']:
                with db.tx() as c:
                    c.execute("UPDATE multipart_transfers SET state='verifying',updated=? WHERE id=?",(time.time(),tid))
                    c.execute("UPDATE datasets SET state='Verifying',version_id=?,updated=? WHERE id=?",(head.get('VersionId'),time.time(),row['dataset_id']))
                return {'state':'verifying','dataset_id':row['dataset_id']}
        except client().exceptions.ClientError as e:
            if e.response['Error']['Code'] not in ['404','NoSuchKey','NotFound']:raise
        raise ValueError('Cloud completion outcome is being reconciled; completed parts and reservations are preserved')
    state=status(uid,tid);parts=state['parts'];expected=math.ceil(row['bytes']/row['part_size'])
    if len(parts)!=expected or [p['number'] for p in parts]!=list(range(1,expected+1)) or sum(p['bytes'] for p in parts)!=row['bytes']:raise ValueError('Upload is incomplete')
    for p in parts:
        if p['bytes']!=min(row['part_size'],row['bytes']-(p['number']-1)*row['part_size']):raise ValueError('Part length is invalid')
    with db.tx() as c:c.execute("UPDATE multipart_transfers SET state='completing',updated=? WHERE id=?",(time.time(),tid))
    result=client().complete_multipart_upload(Bucket=s['s3_bucket'],Key=row['s3_key'],UploadId=row['upload_id'],MultipartUpload={'Parts':[{'PartNumber':p['number'],'ETag':p['etag']} for p in parts]})
    with db.tx() as c:
        c.execute("UPDATE multipart_transfers SET state='verifying',updated=? WHERE id=?",(time.time(),tid))
        c.execute("UPDATE datasets SET state='Verifying',version_id=?,updated=? WHERE id=?",(result.get('VersionId'),time.time(),row['dataset_id']))
        db.audit(c,uid,'dataset.verify.queued',row['dataset_id'],{'bytes':row['bytes']})
    return {'state':'verifying','dataset_id':row['dataset_id']}

def verify_one():
    pending=db.query("SELECT * FROM multipart_transfers WHERE state='completing' ORDER BY updated LIMIT 1",one=True)
    if pending:
        try:complete(pending['user_id'],pending['id'])
        except ValueError:pass
    row=db.query("SELECT * FROM datasets WHERE state='Verifying' ORDER BY created LIMIT 1",one=True)
    if not row:return False
    uid=row['user_id'];s=data.settings(uid);kwargs={'Bucket':s['s3_bucket'],'Key':row['s3_key']}
    if row['version_id']:kwargs['VersionId']=row['version_id']
    response=client().get_object(**kwargs);sha=hashlib.sha256();size=0
    try:
        for chunk in response['Body'].iter_chunks(4*1024*1024):sha.update(chunk);size+=len(chunk)
    finally:response['Body'].close()
    good=sha.hexdigest()==row['sha256'] and size==row['bytes']
    with db.tx() as c:
        c.execute('UPDATE datasets SET state=?,updated=? WHERE id=?',('Ready' if good else 'Action required',time.time(),row['id']))
        c.execute('UPDATE multipart_transfers SET state=?,updated=? WHERE dataset_id=?',('complete' if good else 'action-required',time.time(),row['id']))
        if good:
            uri='s3://'+s['s3_bucket']+'/'+row['s3_key'];meta=json.loads(row['manifest']);meta.update(dataset_id=row['id'],sha256=row['sha256'],version_id=row['version_id'],uploaded=True)
            source=c.execute('SELECT id FROM assets WHERE id=? AND user_id=?',(row['source_asset'],uid)).fetchone() if row['source_asset'] else None
            if source:
                c.execute('UPDATE assets SET path=?,metadata=? WHERE id=? AND user_id=?',(uri,db.encode(meta),row['source_asset'],uid))
            else:c.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,path) DO UPDATE SET metadata=excluded.metadata',(row['id'],uid,uri,row['name'],meta.get('format') or 'DATABASE',size,time.time(),'cloud',None,db.encode(meta)))
        db.audit(c,uid,'dataset.verified' if good else 'dataset.integrity.failed',row['id'],{'bytes':size})
    transfer=db.query('SELECT id FROM multipart_transfers WHERE dataset_id=?',(row['id'],),one=True)
    if transfer:budget.reconcile(transfer['id'],uid,'storage')
    return True

def bind(uid,dataset_id,payload):
    """Bind verified multi-file data; every companion remains tenant-owned."""
    allowed={'assembly','mate_id','index_id','dictionary_id','index_ids','sample_id'}
    if set(payload)-allowed:raise ValueError('Unknown dataset companion or reference field')
    def get(ident):
        row=db.query("SELECT * FROM datasets WHERE id=? AND user_id=? AND state='Ready'",(ident,uid),one=True)
        if not row:raise ValueError('Every primary and companion dataset must be verified, Ready and owned by this workspace')
        meta=json.loads(row['manifest']);return row,meta,'s3://'+data.settings(uid)['s3_bucket']+'/'+row['s3_key']
    row,manifest,uri=get(dataset_id);asset=db.query('SELECT * FROM assets WHERE id=? AND user_id=?',(dataset_id,uid),one=True)
    if not asset:raise ValueError('Verified primary asset is not registered')
    meta=json.loads(asset['metadata']);fmt=asset['format']
    for field in ['mate','index','dictionary']:
        if field+'_id' in payload and payload[field+'_id'] in [None,'']:
            meta.pop(field,None);meta.pop(field+'_sha256',None)
            if field=='mate' and fmt=='FASTQ_PAIR':fmt='FASTQ'
            if field=='index':fmt={'INDEXED_BAM':'BAM','INDEXED_CRAM':'CRAM','INDEXED_VCF':'VCF'}.get(fmt,fmt)
        if payload.get(field+'_id'):
            companion,cm,path=get(payload[field+'_id'])
            if field=='mate' and (fmt not in ['FASTQ','FASTQ_PAIR'] or cm.get('format')!='FASTQ' or companion['id']==dataset_id):raise ValueError('A paired FASTQ requires a distinct verified FASTQ mate')
            if field=='index' and cm.get('format')!='INDEX':raise ValueError('Bind a verified index file')
            # Preserve existing versioned INDEX-classified dictionary manifests;
            # new dictionaries have a separate semantic type, never intervals.
            if field=='dictionary' and cm.get('format') not in ['INDEX','REFERENCE_DICTIONARY']:raise ValueError('Bind a verified sequence dictionary file')
            meta[field]=path;meta[field+'_sha256']=companion['sha256']
            meta[field+'_version_id']=companion['version_id']
            if field=='mate':fmt='FASTQ_PAIR'
            if field=='index':fmt={'BAM':'INDEXED_BAM','CRAM':'INDEXED_CRAM','VCF':'INDEXED_VCF','BGZIP_VCF':'INDEXED_VCF'}.get(fmt,fmt)
    ids=payload.get('index_ids',[])
    if not isinstance(ids,list) or len(ids)>32:raise ValueError('At most 32 explicit companion indices are supported')
    if ids:
        meta['index_paths']=[];meta['index_paths_sha256']={}
        for ident in ids:
            companion,cm,path=get(ident)
            if cm.get('format') not in ['INDEX','REFERENCE_DICTIONARY']:raise ValueError('Every index bundle member must be a verified index or sequence dictionary')
            meta['index_paths'].append(path);meta['index_paths_sha256'][path]=companion['sha256']
    elif 'index_ids' in payload:
        meta.pop('index_paths',None);meta.pop('index_paths_sha256',None)
    if payload.get('assembly'):
        if payload['assembly'] not in ['GRCh37','GRCh38','T2T-CHM13','rCRS','engineering','custom']:raise ValueError('Choose an explicit supported assembly label')
        meta['assembly']=payload['assembly'];meta['assembly_verification']='Declared by researcher; worker/reference identity validation is still required'
    if payload.get('sample_id'):
        if not re.fullmatch('[A-Za-z0-9_.-]{1,100}',payload['sample_id']):raise ValueError('Invalid scientific sample identifier')
        meta['sample_id']=payload['sample_id']
    with db.tx() as c:
        c.execute('UPDATE assets SET format=?,assembly=?,metadata=? WHERE id=? AND user_id=?',(fmt,meta.get('assembly'),db.encode(meta),dataset_id,uid))
        db.audit(c,uid,'dataset.companions.bound',dataset_id,{'companion_ids':{k:v for k,v in payload.items() if k.endswith('_id') or k=='index_ids'},'assembly':meta.get('assembly')})
    return {'id':dataset_id,'format':fmt,'metadata':meta}

initialize()

if __name__=='__main__':
    while True:
        try:verify_one()
        except Exception as e:print(db.encode({'event':'dataset.verifier.retry','exception':type(e).__name__}),flush=True)
        time.sleep(5)
