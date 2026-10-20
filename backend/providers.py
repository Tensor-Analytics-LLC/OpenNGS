import json
import os
import subprocess
import sys
import time
from pathlib import Path
import httpx
from . import db,security
from .config import ROOT

def health(uid,sid):
    row=db.query('SELECT * FROM secrets WHERE id=? AND user_id=?',(sid,uid),one=True)
    if not row or row['revoked']:raise ValueError('Credential is absent or revoked')
    bindings=json.loads(row['binding'])
    if not bindings:raise ValueError('Bind this credential to a node before testing')
    token=security.unwrap(uid,sid,bindings[0])
    if row['provider']=='oncokb':
        r=httpx.get('https://www.oncokb.org/api/v1/info',timeout=10,follow_redirects=False)
        return {'state':'reachable' if r.status_code==200 else 'unavailable','http_status':r.status_code,'authentication_verified':False,'message':'Vault decryption and public provider connectivity checked. The public info endpoint does not validate your token or licence.'}
    if row['provider']=='openai':
        r=httpx.get('https://api.openai.com/v1/models',headers={'Authorization':'Bearer '+token},timeout=10,follow_redirects=False)
        return {'state':'authenticated' if r.status_code==200 else 'rejected','http_status':r.status_code,'authentication_verified':r.status_code==200,'message':'Model-list authentication check; no genomic payload was sent.'}
    return {'state':'encrypted-and-readable','authentication_verified':False,'message':'Credential envelope and node binding verified. Vendor licence validation requires the contracted executable/adapter; no invented endpoint was contacted.'}

def oncokb(uid,node,inputs,work,rid=None):
    from .portable import report
    credential=security.credential_for(uid,'oncokb','oncokb')
    if not credential:raise ValueError('No active OncoKB credential bound to this node')
    if not node['params'].get('allow_external_processing'):raise ValueError('Explicitly allow external provider processing in this node before submitting variants')
    started=time.monotonic()
    job={'tool':'provider.variants','params':{'limit':node['params'].get('max_variants',25)},'inputs':inputs,'workdir':str(work)}
    file=work/'variant-request.json';file.write_text(db.encode(job))
    env={'PATH':os.environ.get('PATH',''),'PYTHONPATH':str(ROOT)}
    process=subprocess.run([sys.executable,'-m','backend.portable',str(file)],env=env,capture_output=True,timeout=30)
    if process.returncode:raise ValueError('Variant extraction failed in isolated parser process')
    rows=json.loads((work/'variants-for-provider.json').read_text());token=security.unwrap(uid,credential['id'],'oncokb');out=[]
    with httpx.Client(timeout=10,follow_redirects=False) as client:
        for row in rows:
            if rid:
                from .runner import cancelled
                if cancelled(rid):raise InterruptedError('Provider run cancelled')
            if time.monotonic()-started>node['resources']['timeout_seconds']:raise TimeoutError('Provider call budget exhausted')
            live=db.query('SELECT frozen FROM users WHERE id=?',(uid,),one=True)
            active=db.query('SELECT revoked FROM secrets WHERE id=?',(credential['id'],),one=True)
            if not live or live['frozen'] or active['revoked']:raise ValueError('Provider access revoked')
            params={'hgvsg':row['hgvsg'],'referenceGenome':node['params']['assembly']}
            if node['params'].get('tumor_type'):params['tumorType']=node['params']['tumor_type']
            origin='germline/' if node['params'].get('origin')=='germline' else ''
            response=client.get('https://www.oncokb.org/api/v1/annotate/'+origin+'mutations/byHGVSg',params=params,headers={'Authorization':'Bearer '+token})
            if response.status_code!=200:raise ValueError('OncoKB rejected the operation (HTTP '+str(response.status_code)+'); verify vendor access, scope and agreement')
            if len(response.content)>2*1024*1024:raise ValueError('Provider response exceeds the permitted envelope')
            out.append({'variant':row,'annotation':response.json()})
    with db.tx() as c:db.audit(c,uid,'provider.oncokb.completed',node['id'],{'calls':len(out),'assembly':node['params']['assembly'],'credential_version':credential['version']})
    return {'annotation':[report(work,'oncokb-annotation.json',{'provider':'OncoKB','reference_genome':node['params']['assembly'],'calls':len(out),'results':out,'clinical_release_approved':False})]}
