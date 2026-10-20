"""Exact curated sample materialization for funded research workspaces.

The operator publishes verified repository fixtures once. Research source nodes
route their frozen tenant-owned records; no genomic parser runs in the API.
"""
import base64
import hashlib
import json
import os
import time
from pathlib import Path

from botocore.exceptions import ClientError
from . import budget, data, db, demo, entitlements
from .config import ROOT, aws_session


def manifest():
    raw=(ROOT/'demo-data/manifest.json').read_bytes()
    return json.loads(raw),hashlib.sha256(raw).hexdigest()


def outputs(uid, tool):
    row=db.query('SELECT value FROM controls WHERE key=?',('curated-cloud:'+uid,),one=True)
    record=json.loads(row['value']) if row else {}
    _,digest=manifest()
    if record.get('manifest_sha256')!=digest or tool not in record.get('outputs',{}):
        raise ValueError('The exact curated source has not been staged for this research workspace')
    return json.loads(db.encode(record['outputs'][tool]))


def publish(actor,uid):
    if actor.get('role')!='admin' or actor.get('frozen') or not entitlements.research(uid):
        raise ValueError('An active administrator and funded workspace are required')
    if not db.query("SELECT id FROM users WHERE id=? AND frozen=0 AND role!='retired'",(uid,),one=True):
        raise ValueError('Choose an active destination workspace')
    frozen,digest=manifest();settings=data.settings(uid)
    if settings.get('research_profile',{}).get('state')!='Ready':
        raise ValueError('Funded private cloud storage must be ready')
    if set(frozen['outputs'])!=demo.SOURCES:
        raise ValueError('The curated release differs from the declared source catalogue')
    files=frozen['files'];total=sum(f['bytes'] for f in files)
    if total>64*1024**2 or not files:
        raise ValueError('Curated materialization is bounded to a reviewed 64 MiB release')
    prepared=[]
    for f in files:
        path=(ROOT/'demo-data'/f['path']).resolve()
        if not path.is_relative_to((ROOT/'demo-data').resolve()) or path.is_symlink() or not path.is_file():
            raise ValueError('Invalid curated release path')
        if path.stat().st_size!=f['bytes'] or hashlib.sha256(path.read_bytes()).hexdigest()!=f['sha256']:
            raise ValueError('Curated sample differs from its published digest')
        ident='ds_curated_'+hashlib.sha256((uid+':'+f['path']+':'+f['sha256']).encode()).hexdigest()[:40]
        key=f'tenants/{uid}/datasets/{ident}/{path.name}'
        prepared.append({**f,'path_on_host':path,'id':ident,'key':key})
    additional=sum(f['bytes'] for f in prepared if not db.query('SELECT id FROM datasets WHERE id=? AND user_id=?',(f['id'],uid),one=True))
    operation='curated-cloud-'+digest+'-'+uid
    with db.tx() as c:
        held=int(c.execute("SELECT COALESCE(SUM(bytes),0) FROM output_holds WHERE user_id=? AND state='reserved'",(uid,)).fetchone()[0])
        if entitlements.storage_exposure(c,uid)+held+additional>entitlements.limits(uid)['max_storage_bytes']:
            raise budget.BudgetFull('Complete curated release does not fit stored-byte allowance')
        budget.reserve(c,uid,operation,'storage',.05,{'bytes':additional,'scope':'Exact curated sample publication; retained capacity funded separately'})
    client=aws_session().client('s3');published={}
    for f in prepared:
        args={'Bucket':settings['s3_bucket'],'Key':f['key']};checksum=base64.b64encode(bytes.fromhex(f['sha256'])).decode()
        try:
            head=client.head_object(**args,ChecksumMode='ENABLED')
        except ClientError as error:
            if error.response['Error']['Code'] not in ['404','NoSuchKey','NotFound']:raise
            with f['path_on_host'].open('rb') as body:
                put=client.put_object(**args,Body=body,ServerSideEncryption='aws:kms',SSEKMSKeyId=settings['kms_key_arn'],
                    ChecksumSHA256=checksum,Metadata={'sha256':f['sha256'],'tenant':uid,'dataset':f['id']})
            head=client.head_object(**args,VersionId=put['VersionId'],ChecksumMode='ENABLED')
        if head['ContentLength']!=f['bytes'] or head.get('ChecksumSHA256')!=checksum or head.get('Metadata',{}).get('sha256')!=f['sha256'] or head.get('VersionId') in [None,'','null']:
            raise ValueError('Curated cloud object identity differs from its exact release')
        existing=db.query('SELECT user_id,bytes,sha256,s3_key,version_id FROM datasets WHERE id=?',(f['id'],),one=True)
        if existing and (existing['user_id']!=uid or existing['bytes']!=f['bytes'] or existing['sha256']!=f['sha256'] or existing['s3_key']!=f['key'] or existing['version_id']!=head['VersionId']):
            raise ValueError('An existing curated dataset has a different immutable identity')
        published['/opt/studio/demo-data/'+f['path']]={**f,'uri':'s3://'+settings['s3_bucket']+'/'+f['key'],'version_id':head['VersionId']}
    result=json.loads(db.encode(frozen['outputs']))
    for ports in result.values():
        for items in ports.values():
            for item in items:
                primary=published[item['path']];meta=item.setdefault('metadata',{})
                item.update(path=primary['uri'],digest='sha256:'+primary['sha256'],bytes=primary['bytes'])
                meta.update(version_id=primary['version_id'],sample_only=True,curated_manifest_sha256=digest,secondary_objects=[])
                for field in ['mate','index','dictionary','reference']:
                    value=meta.get(field)
                    if value in published:
                        f=published[value];meta[field]=f['uri'];meta[field+'_sha256']=f['sha256'];meta[field+'_version_id']=f['version_id']
                        meta['secondary_objects'].append({'uri':f['uri'],'sha256':f['sha256'],'version_id':f['version_id'],'bytes':f['bytes']})
                    elif field=='reference' and value=='engineering_contig':
                        meta['reference_contig']=meta.pop('reference')
                    elif value:
                        raise ValueError('Curated companion is absent from the exact release')
    now=time.time()
    with db.tx() as c:
        for f in published.values():
            meta={'format':data.file_type(f['path_on_host']) or 'DATABASE','sha256':f['sha256'],'version_id':f['version_id'],'curated_manifest_sha256':digest}
            c.execute("INSERT INTO datasets VALUES(?,?,?,?,?,?,'Ready',?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                (f['id'],uid,None,f['path_on_host'].name,f['bytes'],f['sha256'],f['key'],f['version_id'],db.encode(meta),now,now))
        record={'manifest_sha256':digest,'outputs':result,'bytes':total,'objects':len(prepared),'state':'Ready'}
        c.execute('INSERT INTO controls VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',('curated-cloud:'+uid,db.encode(record)))
        db.audit(c,actor['id'],'curated.cloud.published',uid,{'manifest_sha256':digest,'bytes':total,'objects':len(prepared),'scientific_substitution':False})
    budget.reconcile(operation,uid,'storage')
    return {'state':'Ready','bytes':total,'objects':len(prepared),'sources':sorted(result),'manifest_sha256':digest}
