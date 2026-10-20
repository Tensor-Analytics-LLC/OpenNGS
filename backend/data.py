import concurrent.futures
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError
from . import db
from .config import DATA, SSD, REGION, aws_session

DEFAULT_ROOT = str(SSD / 'decode/reference-data') if SSD.exists() else str(DATA)
DEFAULTS = {'ssd_roots':[DEFAULT_ROOT], 'aws_region':'us-east-1','s3_bucket':os.environ.get('NGS_BUCKET',''),'kms_key_arn':os.environ.get('NGS_KMS_KEY',''),'batch_queue':'','batch_job_definition':'','backend':'portable','max_concurrency':2,'max_upload_gib':20,'max_node_seconds':600,'rights_approved':[],'image_approvals':{},'theme':'dark'}
PUBLIC = [
 {'id':'giab-hg002','name':'GIAB HG002 · GRCh38 v4.2.1','family':'giab','format':'INDEXED_VCF','assembly':'GRCh38','bucket':'giab','key':'release/AshkenazimTrio/HG002_NA24385_son/NISTv4.2.1/GRCh38/HG002_GRCh38_1_22_v4.2.1_benchmark.vcf.gz','region':'us-east-1','docs':'https://registry.opendata.aws/giab/','license':'Public benchmark; cite NIST'},
 {'id':'gnomad-v4-chr22','name':'gnomAD genomes · v4.1 chr22','family':'gnomad','format':'INDEXED_VCF','assembly':'GRCh38','bucket':'gnomad-public-us-east-1','key':'release/4.1/vcf/genomes/gnomad.genomes.v4.1.sites.chr22.vcf.bgz','region':'us-east-1','docs':'https://gnomad.broadinstitute.org/data','license':'Review gnomAD data terms'},
 {'id':'1000g','name':'1000 Genomes · public archive','family':'1000genomes','format':'COLLECTION','bucket':'1000genomes','prefix':'phase3/','region':'us-east-1','docs':'https://registry.opendata.aws/1000-genomes/','license':'Public data; cite consortium'},
 {'id':'broad-grch38','name':'Broad GRCh38 · assembly38','family':'reference','format':'FASTA','assembly':'GRCh38','bucket':'broad-references','key':'hg38/v0/Homo_sapiens_assembly38.fasta','region':'us-east-1','docs':'https://gatk.broadinstitute.org/hc/en-us/articles/360035890811-Resource-bundle','license':'Reference data; verify individual asset terms'},
 {'id':'ncbi-clinvar','name':'ClinVar · GRCh38','family':'clinvar','format':'INDEXED_VCF','assembly':'GRCh38','https':'https://ftp.ncbi.nlm.nih.gov/pub/clinvar/vcf_GRCh38/clinvar.vcf.gz','region':None,'docs':'https://www.ncbi.nlm.nih.gov/clinvar/docs/maintenance_use/','license':'NCBI data usage policy; retain release date'},
 {'id':'ensembl','name':'Ensembl · reference releases','family':'ensembl','format':'COLLECTION','https':'https://ftp.ensembl.org/pub/','region':None,'docs':'https://www.ensembl.org/info/about/legal/disclaimer.html','license':'Versioned provider terms'},
 {'id':'t2t-chm13','name':'T2T-CHM13 · v2.0','family':'reference','format':'FASTA','assembly':'T2T-CHM13','https':'https://s3-us-west-2.amazonaws.com/human-pangenomics/T2T/CHM13/assemblies/chm13v2.0.fa.gz','region':'us-west-2','docs':'https://github.com/marbl/CHM13','license':'Public reference; cite T2T Consortium'},
]
PUBLIC_IDENTITIES = {
 'giab-hg002': {'release':'4.2.1','cohort':'HG002','scope':'autosomes 1–22 benchmark VCF','mutable':False},
 'gnomad-v4-chr22': {'release':'4.1','cohort':'genomes','scope':'chr22','mutable':False},
 '1000g': {'release':'Phase 3','cohort':'1000 Genomes','scope':'archive collection; individual object must be selected','mutable':False},
 'broad-grch38': {'release':'hg38/v0','cohort':'Broad assembly38 reference','scope':'complete named FASTA; companion indices separate','mutable':False},
 'ncbi-clinvar': {'release':'latest (mutable; release date unpinned)','cohort':'ClinVar','scope':'GRCh38 VCF','mutable':True},
 'ensembl': {'release':'unselected','cohort':'Ensembl','scope':'release directory; individual version must be selected','mutable':True},
 't2t-chm13': {'release':'2.0','cohort':'CHM13','scope':'complete named FASTA; companion indices separate','mutable':False},
}
for _source in PUBLIC:
    _source['source_identity'] = {'assembly':_source.get('assembly'), **PUBLIC_IDENTITIES[_source['id']]}
    _source['identity_note'] = 'Declared release and scope; immutable object version, matching companions and scientific compatibility still require verification.'
    if _source['source_identity']['mutable']:
        _source['identity_note'] = 'Mutable provider listing: this does not identify the exact release of an existing local dataset.'
POOL=concurrent.futures.ThreadPoolExecutor(max_workers=3,thread_name_prefix='data')

def settings(uid):
    row=db.query('SELECT body FROM settings WHERE user_id=?',(uid,),one=True)
    result={**DEFAULTS, **(json.loads(row['body']) if row else {})}
    from .config import DEMO
    from .entitlements import restricted,limits
    if restricted(uid):result.update(ssd_roots=[],backend='hosted-demo',max_node_seconds=120,max_upload_gib=1)
    elif DEMO:
        allowance=limits(uid)
        from .provisioning import profile
        capacity=profile(uid)
        result.update(ssd_roots=[],backend='aws-batch',max_node_seconds=allowance['max_node_seconds'],max_upload_gib=max(1,allowance['max_scratch_mb']//1024),s3_bucket=capacity.get('bucket',''),kms_key_arn=os.getenv('NGS_KMS_KEY',''),batch_queue=capacity.get('queue',''),batch_job_definition=capacity.get('definition',''),max_concurrency=allowance['max_concurrent_jobs'],research_profile=capacity)
        result['image_approvals']={k:capacity['image'] for k in capacity.get('tools',[]) } if capacity.get('image') else {}
        result['image_approvals'].update({k:v['image'] for k,v in capacity.get('runtime_profiles',{}).items()})
    return result

def update_settings(user, patch):
    from .config import DEMO
    if DEMO and set(patch)-{'theme'}:raise ValueError('Hosted runtime and data limits are managed through funded administrator allowances')
    allowed={'ssd_roots','s3_bucket','kms_key_arn','batch_queue','batch_job_definition','backend','max_upload_gib','max_node_seconds','theme'}
    if user['role']=='admin':allowed|={'rights_approved','image_approvals','max_concurrency'}
    if set(patch)-allowed:raise ValueError('Unknown or unauthorized setting')
    new={**settings(user['id']),**patch}
    if new['backend'] not in ['portable','docker','aws-batch','hosted-demo']:raise ValueError('Invalid backend')
    if new['theme'] not in ['dark','light']:raise ValueError('Invalid theme')
    if not isinstance(new['ssd_roots'],list) or len(new['ssd_roots'])>8:raise ValueError('Choose at most eight data directories')
    for root in new['ssd_roots']:
        p=Path(root).expanduser().resolve()
        if not p.is_dir() or p==Path('/') or p==Path.home():raise ValueError('Choose a specific existing data directory')
        if user['role']!='admin' and not (p.is_relative_to(Path(DEFAULT_ROOT).resolve()) or p.is_relative_to(DATA/'uploads'/user['id'])):raise ValueError('Only the owner can register additional local filesystem roots')
        if user['role']=='admin' and not (p.is_relative_to(SSD) or p.is_relative_to(DATA)):raise ValueError('Data directories must be on the connected SSD or in the Studio data store')
    if user['role']!='admin' and any(k in patch for k in ['s3_bucket','kms_key_arn','batch_queue','batch_job_definition','backend']):raise ValueError('Cloud and execution profiles are configured by the workspace owner')
    for key,low,high in [('max_upload_gib',1,2048),('max_node_seconds',10,86400),('max_concurrency',1,8)]:
        if not isinstance(new[key],int) or not low<=new[key]<=high:raise ValueError(f'{key} outside supported bounds')
    if user['role']!='admin' and (new['max_upload_gib']>20 or new['max_node_seconds']>600):raise ValueError('Trial limits require an administrator override')
    if new['s3_bucket'] and not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]',new['s3_bucket']):raise ValueError('Invalid S3 bucket')
    if new['kms_key_arn'] and not new['kms_key_arn'].startswith('arn:aws:kms:us-east-1:'):raise ValueError('KMS key must be in us-east-1')
    if not isinstance(new['rights_approved'],list) or not all(isinstance(x,str) and len(x)<100 for x in new['rights_approved']):raise ValueError('Invalid rights approvals')
    for tool,image in new['image_approvals'].items():
        if not re.fullmatch(r'[a-zA-Z0-9./_:-]+@sha256:[a-f0-9]{64}',image):raise ValueError(f'{tool}: image must be digest pinned')
    with db.tx() as c:
        c.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET body=excluded.body',(user['id'],db.encode(new)))
        db.audit(c,user['id'],'settings.changed',detail={'fields':sorted(patch)})
    return new

def permitted_path(uid,path):
    p=Path(path).expanduser().resolve(strict=True)
    roots=[Path(x).expanduser().resolve() for x in settings(uid)['ssd_roots']]+[DATA]
    if not any(p.is_relative_to(root) for root in roots):raise ValueError('File is outside registered data roots')
    if not p.is_file():raise ValueError('A regular file is required')
    return p

def file_type(path):
    n=path.name.lower()
    if n.endswith(('.fai','.gzi','.bai','.crai','.csi','.tbi','.mmi','.bt2','.ht2','.dict','.idx','.gzi')):return 'INDEX'
    if n.endswith(('.vcf','.vcf.gz','.vcf.bgz')):
        return 'INDEXED_VCF' if Path(str(path)+'.tbi').exists() or Path(str(path)+'.csi').exists() else 'VCF'
    for endings,fmt in [(('.fastq','.fastq.gz','.fq','.fq.gz'),'FASTQ'),(('.fasta','.fa','.fa.gz','.fasta.gz','.fa.bgz','.fna','.fna.gz'),'FASTA'),(('.bam',),'BAM'),(('.sam',),'SAM'),(('.cram',),'CRAM'),(('.bed','.bed.gz'),'BED'),(('.interval_list',),'INTERVALLIST'),(('.h5ad',),'H5AD'),(('.rds',),'RDS'),(('.pod5',),'POD5'),(('.zip','.tar','.tar.gz','.tgz'),'ARCHIVE'),(('.bcf',),'BCF'),(('.chain','.chain.gz'),'CHAIN'),(('.gtf','.gtf.gz'),'GTF'),(('.gff','.gff3','.gff3.gz'),'GFF3'),(('.csv',),'CSV'),(('.tsv','.tsv.gz'),'TSV'),(('.json',),'REPORT'),(('.bw','.bigwig'),'BIGWIG')]:
        if n.endswith(endings):return fmt
    if n.endswith(('.vcf.bgz','.bgz','.tsv','.tsv.gz','.csv','.txt','.txt.gz','.gz','.zip','.tar.gz','.obo','.json','.db','.sqlite','.bin','.h5','.onnx','.pt')):return 'DATABASE'
    return None

def family(path):
    t=str(path).lower()
    for key in ['gnomad','clinvar','alphamissense','spliceai','dbnsfp','exomiser','hpo','omim','vep','revel','cadd','giab','ensembl','mane','clingen']:
        if key in t:return key
    return 'reference' if file_type(path)=='FASTA' else 'local'

def scan(uid,root):
    root=Path(root).expanduser().resolve(strict=True)
    if str(root) not in [str(Path(x).expanduser().resolve()) for x in settings(uid)['ssd_roots']]:raise ValueError('Register this directory in Settings before scanning')
    sid=db.ident('scan_')
    with db.tx() as c:
        c.execute('INSERT INTO scans(id,user_id,state,root,started) VALUES(?,?,?,?,?)',(sid,uid,'running',str(root),time.time()))
        db.audit(c,uid,'assets.scan.started',sid)
    POOL.submit(scan_worker,sid,uid,root)
    return sid

def scan_worker(sid,uid,root):
    count=total=0
    try:
        for parent,dirs,files in os.walk(root,followlinks=False):
            dirs[:]=[d for d in dirs if not d.startswith('.') and not (Path(parent)/d).is_symlink()]
            if len(Path(parent).relative_to(root).parts)>=10:dirs[:]=[]
            for name in files:
                if name.startswith('.') or count>=50000:continue
                p=Path(parent)/name
                if p.is_symlink():continue
                fmt=file_type(p)
                if not fmt:continue
                try:st=p.stat()
                except OSError:continue
                meta={'root':str(root),'index_paths':[str(i) for i in [Path(str(p)+'.tbi'),Path(str(p)+'.bai'),Path(str(p)+'.csi'),Path(str(p)+'.fai')] if i.exists()]}
                if fmt=='FASTQ':
                    candidates={re.sub(r'([._])R1([._])',r'\g<1>R2\2',name,count=1),name.replace('_1.f','_2.f',1)}
                    candidates={x for x in candidates if x!=name and (p.parent/x).is_file()}
                    if len(candidates)==1:fmt='FASTQ_PAIR';meta['mate']=str(p.parent/next(iter(candidates)));meta['pairing']='filename-inferred; validate identifiers before scientific interpretation'
                    elif len(candidates)>1:meta['pairing']='ambiguous';meta['mate_candidates']=sorted(candidates)
                assembly='GRCh38' if any(s in str(p).lower() for s in ['grch38','hg38']) else 'GRCh37' if any(s in str(p).lower() for s in ['grch37','hg19']) else 'T2T-CHM13' if 'chm13' in str(p).lower() else None
                aid='ssd_'+hashlib.sha256((uid+str(p)).encode()).hexdigest()[:24]
                with db.tx() as c:
                    c.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,path) DO UPDATE SET bytes=excluded.bytes,mtime=excluded.mtime,format=excluded.format,metadata=CASE WHEN assets.bytes=excluded.bytes AND assets.mtime=excluded.mtime THEN json_patch(assets.metadata,excluded.metadata) ELSE excluded.metadata END',(aid,uid,str(p),p.name,fmt,st.st_size,st.st_mtime,family(p),assembly,db.encode(meta)))
                    count+=1;total+=st.st_size
                    if count%50==0:c.execute('UPDATE scans SET files=?,bytes=? WHERE id=?',(count,total,sid))
        with db.tx() as c:
            c.execute("UPDATE scans SET state='complete',files=?,bytes=?,finished=? WHERE id=?",(count,total,time.time(),sid))
            db.audit(c,uid,'assets.scan.completed',sid,{'files':count,'bytes':total})
    except Exception as e:
        with db.tx() as c:c.execute("UPDATE scans SET state='failed',error=?,finished=? WHERE id=?",(str(e)[:500],time.time(),sid))

def unsigned_s3():
    import boto3
    return boto3.client('s3',region_name='us-east-1',config=Config(signature_version=UNSIGNED,connect_timeout=4,read_timeout=8,retries={'max_attempts':1}))

def probe_public(source_id):
    row=next((dict(x) for x in PUBLIC if x['id']==source_id),None)
    if not row:raise ValueError('Unknown public source')
    try:
        if row.get('key'):
            h=unsigned_s3().head_object(Bucket=row['bucket'],Key=row['key'])
            row.update(available=True,bytes=h['ContentLength'],etag=h.get('ETag'),version=h.get('VersionId'),uri=f"s3://{row['bucket']}/{row['key']}",checked_at=time.time())
        elif row.get('prefix'):
            h=unsigned_s3().list_objects_v2(Bucket=row['bucket'],Prefix=row['prefix'],MaxKeys=10)
            row.update(available=True,objects=[{'key':x['Key'],'bytes':x['Size']} for x in h.get('Contents',[])],checked_at=time.time())
        else:
            import httpx
            h=httpx.head(row['https'],timeout=8,follow_redirects=True)
            row.update(available=h.status_code<400,status=h.status_code,bytes=int(h.headers.get('content-length',0)),checked_at=time.time())
    except Exception as e:row.update(available=False,error=type(e).__name__+': '+str(e)[:180],checked_at=time.time())
    return row

def asset_source_identity(asset):
    metadata=json.loads(asset['metadata']) if isinstance(asset['metadata'],str) else asset['metadata']
    identity={'assembly':asset.get('assembly'),'release':None,'cohort':None,'scope':None,'mutable':False,
              'sha256':metadata.get('sha256'),'version_id':metadata.get('version_id'),
              'basis':'Filename declaration and preserved source checksum; provider release equivalence is not independently qualified'}
    match=re.fullmatch(r'gnomad\.(exomes|genomes)\.v([0-9.]+)\.sites\.(chr[^.]+)\.vcf\.bgz(?:\.tbi)?',asset['name'])
    if match:identity.update(cohort=match[1],release=match[2],scope=match[3])
    elif asset['name'].startswith('HG002_GRCh38_1_22_v4.2.1_benchmark'):
        identity.update(cohort='HG002',release='4.2.1',scope='autosomes 1–22 benchmark VCF' if asset['name'].endswith('.vcf.gz') else 'benchmark companion; file type must match')
    elif asset.get('family')=='clinvar':
        identity.update(cohort='ClinVar',release='unrecorded original snapshot',scope='Original named source file; inspect release header')
    return identity


def compare_source_identity(identity, requested):
    if not requested:
        return 'family_only_not_interchangeable', 'Family and assembly are discovery hints; datasets can differ in cohort, release and scope.'
    if identity.get('mutable') or requested.get('mutable'):
        return 'unverified_mutable_release', 'A mutable provider URL cannot establish equivalence to an existing release snapshot.'
    mismatches=[key for key in ['assembly','release','cohort','scope'] if identity.get(key) and requested.get(key) and identity[key]!=requested[key]]
    if mismatches:
        return 'different_identity', 'Different '+', '.join(mismatches)+'. This is not a replacement for the selected source.'
    return 'declared_identity_requires_verification', 'Matching declared labels are not proof of identical bytes, compatible indices or complete release coverage.'


def resolve(uid, requested_family, assembly=None, backend='aws-batch', source_id=None, asset_id=None):
    candidates=[]
    requested=None
    if source_id and asset_id:raise ValueError('Select one source identity to compare')
    if source_id:
        source=next((p for p in PUBLIC if p['id']==source_id and p['family']==requested_family),None)
        if not source:raise ValueError('Unknown source identity for this data family')
        requested=source['source_identity']
    if asset_id:
        from .migration import resolve_asset
        selected=db.query('SELECT * FROM assets WHERE id=? AND user_id=? AND family=?',(asset_id,uid,requested_family),one=True)
        if not selected:raise ValueError('Selected source is not in this workspace and data family')
        requested=asset_source_identity(resolve_asset(uid,selected))
    for p in PUBLIC:
        if p['family']!=requested_family or (assembly and p.get('assembly') not in [None,assembly]):continue
        rank=0 if backend=='aws-batch' and p.get('region')=='us-east-1' else 3
        compatibility,note=compare_source_identity(p['source_identity'],requested)
        candidates.append({'kind':'public','rank':rank,'id':p['id'],'name':p['name'],'region':p.get('region'),'uri':f"s3://{p['bucket']}/{p.get('key',p.get('prefix',''))}" if p.get('bucket') else p['https'],'requires_probe':True,'source_identity':p['source_identity'],'compatibility':compatibility,'automatic_substitution_allowed':False,'reason':note+' '+('Located in us-east-1; this is a transport advantage only.' if rank==0 else 'Provider access, transfer costs and rights require evaluation.')})
    for a in db.query('SELECT * FROM assets WHERE user_id=? AND family=?',(uid,requested_family)):
        if assembly and a['assembly'] not in [None,assembly]:continue
        from .migration import resolve_asset
        a=resolve_asset(uid,a)
        cloud=a['path'].startswith('s3://')
        identity=asset_source_identity(a);compatibility,note=compare_source_identity(identity,requested)
        metadata=json.loads(a['metadata']) if isinstance(a['metadata'],str) else a['metadata']
        candidates.append({'kind':'cloud' if cloud else 'ssd','rank':0 if cloud or backend!='aws-batch' else 2,'id':a['id'],'name':a['name'],'bytes':a['bytes'],'assembly':a['assembly'],'uri':a['path'] if cloud else None,'source_identity':identity,'compatibility':compatibility,'automatic_substitution_allowed':False,'provenance_warnings':metadata.get('provenance_warnings',[]),'reason':note+' '+('Verified private AWS bytes; original source identity retained.' if cloud else ('Use the explicitly selected SSD file for local execution.' if backend!='aws-batch' else 'Transfer only this exact selected source if a verified identical cloud copy is unavailable.'))})
    return sorted(candidates,key=lambda x:(x['rank'],x['name']))

def hash_file(path,cancel=None):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        while chunk:=f.read(8*1024*1024):
            if cancel and cancel():raise RuntimeError('Transfer cancelled')
            h.update(chunk)
    return h.hexdigest()

def storage_exposure(c,uid):
    local=c.execute("SELECT COALESCE(SUM(bytes),0) FROM assets WHERE user_id=? AND json_extract(metadata,'$.uploaded')=1",(uid,)).fetchone()[0]
    cached=c.execute("SELECT COALESCE(SUM(size),0) FROM (SELECT MAX(total) AS size FROM transfers WHERE user_id=? AND state='complete' GROUP BY COALESCE(digest,id))",(uid,)).fetchone()[0]
    pending=c.execute("SELECT COALESCE(SUM(total),0) FROM transfers WHERE user_id=? AND state IN ('hashing','uploading','staging','browser-upload')",(uid,)).fetchone()[0]
    return local+cached+pending

def start_transfer(user,asset_id):
    if user['frozen']:raise ValueError('Workspace is frozen')
    row=db.query('SELECT * FROM assets WHERE id=? AND user_id=?',(asset_id,user['id']),one=True)
    if not row:raise ValueError('Asset not found')
    p=permitted_path(user['id'],row['path']);s=settings(user['id'])
    if not s['s3_bucket'] or not s['kms_key_arn']:raise ValueError('Configure a private us-east-1 cache bucket and KMS key first')
    if p.stat().st_size>s['max_upload_gib']*1024**3:raise ValueError('Asset exceeds the configured upload ceiling')
    tid=db.ident('transfer_')
    with db.tx() as c:
        if storage_exposure(c,user['id'])+p.stat().st_size>s['max_upload_gib']*1024**3:raise ValueError('Retained cache and concurrent upload quota exhausted')
        c.execute('INSERT INTO transfers(id,user_id,asset_id,state,total,started) VALUES(?,?,?,?,?,?)',(tid,user['id'],asset_id,'hashing',p.stat().st_size,time.time()))
        db.audit(c,user['id'],'transfer.admitted',tid,{'bytes':p.stat().st_size})
    POOL.submit(transfer_worker,tid,user['id'],p,s)
    return tid

def transfer_worker(tid,uid,path,s):
    upload_id=None;client=None;key=None
    def cancelled():return bool(db.query('SELECT cancel_requested FROM transfers WHERE id=?',(tid,),one=True)['cancel_requested'])
    try:
        before=path.stat();digest=hash_file(path,cancelled)
        client=aws_session().client('s3',config=Config(connect_timeout=10,read_timeout=60,retries={'max_attempts':2}))
        location=client.get_bucket_location(Bucket=s['s3_bucket'])['LocationConstraint'] or 'us-east-1'
        if location!='us-east-1':raise ValueError('Cache bucket is outside us-east-1')
        key=f'tenants/{uid}/cache/sha256/{digest}/blob'
        try:
            h=client.head_object(Bucket=s['s3_bucket'],Key=key)
            if h.get('Metadata',{}).get('sha256')==digest and h['ContentLength']==before.st_size:
                with db.tx() as c:c.execute("UPDATE transfers SET state='complete',bytes=total,digest=?,s3_uri=?,finished=? WHERE id=?",(digest,f"s3://{s['s3_bucket']}/{key}",time.time(),tid))
                return
        except ClientError as e:
            if e.response['Error']['Code'] not in ['404','NoSuchKey','NotFound']:raise
        create=client.create_multipart_upload(Bucket=s['s3_bucket'],Key=key,ServerSideEncryption='aws:kms',SSEKMSKeyId=s['kms_key_arn'],Metadata={'sha256':digest,'tenant':uid})
        upload_id=create['UploadId']
        with db.tx() as c:c.execute("UPDATE transfers SET state='uploading',digest=? WHERE id=?",(digest,tid))
        chunk_size=max(64*1024*1024,((before.st_size+9998)//9999+1024*1024-1)//(1024*1024)*(1024*1024))
        parts=[];progress_lock=threading.Lock();progress=0
        def upload_part(number):
            nonlocal progress
            if cancelled():raise RuntimeError('Transfer cancelled')
            with path.open('rb') as f:f.seek((number-1)*chunk_size);body=f.read(chunk_size)
            response=client.upload_part(Bucket=s['s3_bucket'],Key=key,UploadId=upload_id,PartNumber=number,Body=body)
            with progress_lock:
                progress+=len(body)
                with db.tx() as c:c.execute('UPDATE transfers SET bytes=? WHERE id=?',(progress,tid))
            return {'PartNumber':number,'ETag':response['ETag']}
        count=max(1,(before.st_size+chunk_size-1)//chunk_size)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:parts=list(pool.map(upload_part,range(1,count+1)))
        after=path.stat()
        if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise RuntimeError('Source changed during transfer; upload aborted')
        if cancelled():raise RuntimeError('Transfer cancelled')
        client.complete_multipart_upload(Bucket=s['s3_bucket'],Key=key,UploadId=upload_id,MultipartUpload={'Parts':sorted(parts,key=lambda x:x['PartNumber'])})
        upload_id=None
        h=client.head_object(Bucket=s['s3_bucket'],Key=key)
        if h['ContentLength']!=before.st_size or h.get('Metadata',{}).get('sha256')!=digest:raise RuntimeError('Completed object metadata did not verify')
        with db.tx() as c:
            c.execute("UPDATE transfers SET state='complete',digest=?,s3_uri=?,finished=? WHERE id=?",(digest,f"s3://{s['s3_bucket']}/{key}",time.time(),tid))
            db.audit(c,uid,'transfer.completed',tid,{'sha256':digest,'bytes':before.st_size})
    except Exception as e:
        if upload_id and client:
            try:client.abort_multipart_upload(Bucket=s['s3_bucket'],Key=key,UploadId=upload_id)
            except Exception:pass
        with db.tx() as c:c.execute('UPDATE transfers SET state=?,error=?,finished=? WHERE id=?',('cancelled' if cancelled() else 'failed',str(e)[:500],time.time(),tid))
