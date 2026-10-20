"""Attach exact-image, restricted-execution evidence; never infer it from a build."""
import hashlib
import json
from pathlib import Path
from backend.config import aws_session
from backend.secondary_runtime import validate_release

def main():
    record=json.loads(Path('evidence/deployment/secondary-audit-build.json').read_text())
    release=validate_release(json.loads(Path('evidence/deployment/secondary-release.json').read_text()))
    if record['image']!=release['image']:raise ValueError('Isolation audit tested another image')
    s=aws_session();build=s.client('codebuild').batch_get_builds(ids=[record['build_id']])['builds'][0]
    if build['buildStatus']!='SUCCEEDED':raise ValueError('Isolation audit has not succeeded')
    obj=s.client('s3').get_object(Bucket=record['bucket'],Key=record['qualification_key'])
    if obj['ContentLength']>1024**2:raise ValueError('Oversize evidence')
    raw=obj['Body'].read();proof=json.loads(raw)
    proof.update(image=record['image'],build_id=record['build_id'],source_sha256=record['source_sha256'],evidence_sha256=hashlib.sha256(raw).hexdigest(),version_id=obj.get('VersionId'),
        restrictions={'network':'none','root_filesystem':'read-only','capabilities':'all dropped','uid':1000,'cpu_limit':2,'memory_mb':2500,'work_tmpfs_mb':512,'inputs_tmpfs_mb':128,'tmp_tmpfs_mb':512})
    release['isolation_qualification']=proof
    passed={c['tool'] for c in proof['cases'] if c['status']=='passed'}
    release['tools']=sorted(set(release['tools'])&passed)
    validate_release(release);Path('evidence/deployment/secondary-release.json').write_text(json.dumps(release,indent=2))
    print(json.dumps({'tools':release['tools'],'failed':[{k:c.get(k) for k in ['tool','error']} for c in proof['cases'] if c['status']!='passed']}))

if __name__=='__main__':main()
