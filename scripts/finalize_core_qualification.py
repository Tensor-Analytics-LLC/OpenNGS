"""Attach AWS execution evidence to the exact immutable extension release."""
import hashlib
import json
import argparse
from pathlib import Path
from backend.config import aws_session
from backend.runtime_catalog import validate_release

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--adjunct',action='store_true');parser.add_argument('--portable',action='store_true');args=parser.parse_args()
    name='portable-qualification' if args.portable else 'core-adjunct' if args.adjunct else 'core-qualification'
    record=json.loads(Path('evidence/deployment/'+name+'-build.json').read_text())
    release=validate_release(json.loads(Path('evidence/deployment/extended-release.json').read_text()))
    if release['image']!=record['image']:raise ValueError('The audited image differs from the active release candidate')
    session=aws_session()
    build=session.client('codebuild').batch_get_builds(ids=[record['build_id']])['builds'][0]
    if build['buildStatus']!='SUCCEEDED':raise ValueError('Inherited core qualification has not completed')
    obj=session.client('s3').get_object(Bucket=record['bucket'],Key=record['qualification_key'])
    if obj['ContentLength']>1024**2:raise ValueError('Oversize qualification evidence')
    raw=obj['Body'].read();proof=json.loads(raw)
    proof.update(image=record['image'],source_sha256=record['source_sha256'],build_id=record['build_id'],
        evidence_sha256=hashlib.sha256(raw).hexdigest(),version_id=obj.get('VersionId'),
        build_seconds=(build['endTime']-build['startTime']).total_seconds())
    if args.portable:
        release.update(portable_qualification=proof,portable_tools=sorted(c['tool'] for c in proof['cases'] if c['status']=='passed'))
    elif args.adjunct:
        release.update(adjunct_qualification=proof,
            adjunct_tools=sorted(c['tool'] for c in proof['cases'] if c['status']=='passed'))
    else:
        release.update(core_qualification=proof,
            refreshed_core_tools=sorted(c['tool'] for c in proof['cases'] if c['status']=='passed'))
    validate_release(release)
    Path('evidence/deployment/extended-release.json').write_text(json.dumps(release,indent=2))
    print(json.dumps({'image':release['image'],'qualified_tools':release['portable_tools'] if args.portable else release['adjunct_tools'] if args.adjunct else release['refreshed_core_tools'],
        'failed':[{k:c.get(k) for k in ['tool','error']} for c in proof['cases'] if c['status']!='passed']}))

if __name__=='__main__':main()
