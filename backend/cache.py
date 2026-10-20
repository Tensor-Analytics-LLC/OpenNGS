"""Per-workspace content-addressed node reuse, with output integrity revalidation."""
import hashlib
import json
import time
from pathlib import Path
from . import db,data
from .config import aws_session

def initialize():
    with db.tx() as c:
        c.execute('CREATE TABLE IF NOT EXISTS node_cache(user_id TEXT,cache_key TEXT,outputs TEXT,created REAL,last_hit REAL,hits INTEGER DEFAULT 0,PRIMARY KEY(user_id,cache_key))')
        c.execute('CREATE TABLE IF NOT EXISTS file_digests(path TEXT PRIMARY KEY,size INTEGER,mtime_ns INTEGER,digest TEXT)')
        from .config import DATABASE_URL
        if DATABASE_URL:
            c.execute('ALTER TABLE file_digests ALTER COLUMN size TYPE BIGINT')
            c.execute('ALTER TABLE file_digests ALTER COLUMN mtime_ns TYPE BIGINT')

def file_identity(uid,path,cloud_settings=None):
    if path.startswith('s3://'):
        bucket,key=path[5:].split('/',1);s=cloud_settings or data.settings(uid)
        from .migration import authorize_source
        proof=authorize_source(uid,path)
        if (bucket!=s['s3_bucket'] and not proof) or not key.startswith('tenants/'+uid+'/'):raise ValueError('Cache input is outside the tenant prefix')
        args={'Bucket':bucket,'Key':key}
        if proof:args['VersionId']=proof['VersionId']
        head=aws_session().client('s3').head_object(**args);digest=head.get('Metadata',{}).get('sha256')
        if not digest:raise ValueError('S3 cache object lacks a cryptographic content identity')
        return {'digest':digest,'version':head.get('VersionId'),'bytes':head['ContentLength']}
    p=data.permitted_path(uid,path);st=p.stat()
    known=db.query('SELECT * FROM file_digests WHERE path=? AND size=? AND mtime_ns=?',(str(p),st.st_size,st.st_mtime_ns),one=True)
    digest=known['digest'] if known else data.hash_file(p)
    if not known:
        after=p.stat()
        if (st.st_size,st.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise ValueError('Input changed while computing its content identity')
        with db.tx() as c:c.execute('INSERT INTO file_digests VALUES(?,?,?,?) ON CONFLICT(path) DO UPDATE SET size=excluded.size,mtime_ns=excluded.mtime_ns,digest=excluded.digest',(str(p),st.st_size,st.st_mtime_ns,digest))
    return {'digest':digest,'bytes':st.st_size}

def key(uid,spec,node,inputs,backend,extra_spec=None,cloud_settings=None):
    from .demo import SOURCES
    if backend=='aws-batch' and spec['id'] in SOURCES:return None
    if spec.get('api') or spec.get('asset_family') or spec.get('credential') or spec['id'] in ['oncokb','franklin','sample.clinvar','asset.file','reference.asset','database.asset','utility.export']:return None
    identities={}
    for port,rows in sorted(inputs.items()):
        identities[port]=[]
        for item in rows:
            meta={k:v for k,v in item.get('metadata',{}).items() if k not in ['report','mate','index','index_paths']}
            identities[port].append({'format':item['format'],'file':file_identity(uid,item['path'],cloud_settings),'metadata':meta,'indices':[file_identity(uid,p,cloud_settings) for p in item.get('metadata',{}).get('index_paths',[])],'secondary':{field:file_identity(uid,item['metadata'][field],cloud_settings) for field in ['mate','index','dictionary'] if item.get('metadata',{}).get(field)}})
    revision=hashlib.sha256()
    for name in ['portable.py','scientific.py','compiler.py']:
        revision.update(Path(__file__).with_name(name).read_bytes())
    payload={'schema':2,'implementation':revision.hexdigest(),'spec':spec,'published':extra_spec,'params':node.get('params',{}),'advanced':node.get('advanced',''),'resources':node.get('resources'),'image':node.get('_approved_image'),'backend':backend,'inputs':identities}
    return hashlib.sha256(db.encode(payload).encode()).hexdigest()

def load(uid,key,cloud_settings=None):
    if not key:return None
    row=db.query('SELECT * FROM node_cache WHERE user_id=? AND cache_key=?',(uid,key),one=True)
    if not row:return None
    outputs=json.loads(row['outputs'])
    try:
        for items in outputs.values():
            for item in items:
                identity=file_identity(uid,item['path'],cloud_settings)
                if item.get('digest','').removeprefix('sha256:')!=identity['digest']:return None
                for path,expected in item.get('metadata',{}).get('index_digests',{}).items():
                    if file_identity(uid,path,cloud_settings)['digest']!=expected:return None
                for field in ['mate','index','dictionary']:
                    if item.get('metadata',{}).get(field):
                        expected=item['metadata'].get(field+'_sha256')
                        if not expected or file_identity(uid,item['metadata'][field],cloud_settings)['digest']!=expected.removeprefix('sha256:'):return None
        from . import retention
        with db.tx() as c:
            retention.ensure_outputs(uid,outputs,c)
            c.execute('UPDATE node_cache SET last_hit=?,hits=hits+1 WHERE user_id=? AND cache_key=?',(time.time(),uid,key))
        return outputs
    except (ValueError,OSError):return None

def store(uid,key,outputs,cloud_settings=None):
    if not key:return
    for items in outputs.values():
        for item in items:
            if item.get('metadata',{}).get('index_paths'):item['metadata']['index_digests']={p:file_identity(uid,p,cloud_settings)['digest'] for p in item['metadata']['index_paths']}
            for field in ['mate','index','dictionary']:
                if item.get('metadata',{}).get(field):item['metadata'][field+'_sha256']=file_identity(uid,item['metadata'][field],cloud_settings)['digest']
    with db.tx() as c:c.execute('INSERT INTO node_cache VALUES(?,?,?,?,?,0) ON CONFLICT(user_id,cache_key) DO UPDATE SET outputs=excluded.outputs,created=excluded.created',(uid,key,db.encode(outputs),time.time(),time.time()))

initialize()
