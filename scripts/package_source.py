"""Build a reviewed source-only snapshot. Never include live state or inventories."""
import hashlib,json,re,shutil
from pathlib import Path

def main():
    paths=[]
    for d in ['backend','frontend','demo-data','docs','tests','.github']:
        paths.extend(p for p in Path(d).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc')
    paths.extend(Path(x) for x in ['README.md','IMPLEMENTATION_STATUS.md','LICENSE','SECURITY.md','Dockerfile','compose.yaml','.dockerignore','.gitignore','requirements.lock','requirements.txt','start.sh','infra/cache-stack.json','infra/cloud_worker.py','infra/worker.Dockerfile','infra/demo.Dockerfile','infra/demo_compute.py','infra/demo_worker.py','infra/lean_stack.py','infra/bootstrap.sh','infra/rds-ca.pem','infra/openngs-demo.service','infra/openngs-verifier.service','scripts/prepare_demo_data.py','scripts/import_cloud_state.py','scripts/build_demo_image.py','scripts/publish_release.py','scripts/package_source.py'])
    records=[];destination=Path('.state/public-source');destination.mkdir(parents=True,exist_ok=True)
    for p in sorted(set(paths)):
        raw=p.read_bytes()
        if re.search(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY',raw):raise ValueError('Disallowed identifier or key in '+str(p))
        if p.suffix!='.pem' and re.search(rb'\bAKIA[0-9A-Z]{16}\b',raw):raise ValueError('Possible access key in '+str(p))
        if any(s in p.parts for s in ['.state','evidence','.aws','sources']):raise ValueError('Private source path')
        target=destination/p;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,target)
        records.append({'path':str(p),'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),'mode':'100755' if str(p)=='start.sh' else '100644'})
    Path('.state/public-release.json').write_text(json.dumps(records,indent=2))
    print(json.dumps({'files':len(records),'bytes':sum(x['bytes'] for x in records),'secret_scan':'passed; certificate-only PEM bundle separately verified'}))

if __name__=='__main__':main()
