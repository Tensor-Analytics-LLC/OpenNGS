"""Recover an immutable image and its real execution evidence after AWS build."""
import hashlib
import json
from pathlib import Path
from backend.config import aws_session
from backend.runtime_catalog import validate_release

def main():
    record=json.loads(Path('evidence/deployment/extended-build.json').read_text())
    session=aws_session();build=session.client('codebuild').batch_get_builds(ids=[record['build_id']])['builds'][0]
    if build['buildStatus']!='SUCCEEDED':raise ValueError('Extended runtime build has not succeeded')
    s3=session.client('s3')
    data=s3.get_object(Bucket=record['bucket'],Key=record['qualification_key'])
    if data['ContentLength']>1024**2:raise ValueError('Qualification evidence exceeds its bounded format')
    raw=data['Body'].read();qualification=json.loads(raw)
    image=session.client('ecr').describe_images(repositoryName='openngs-demo',imageIds=[{'imageTag':record['tag']}])['imageDetails'][0]
    repository=record['core_image'].split('@')[0]
    release={'family':record['family'],'image':repository+'@'+image['imageDigest'],
             'source_sha256':record['source_sha256'],'core_image':record['core_image'],
             'packages':record['packages'],'qualification':qualification,
             'tools':sorted(c['tool'] for c in qualification['cases'] if c['status']=='passed'),
             'qualification_sha256':hashlib.sha256(raw).hexdigest(),'qualification_version':data.get('VersionId'),
             'build_id':record['build_id'],'build_seconds':(build['endTime']-build['startTime']).total_seconds(),
             'build_maximum_usd':record['maximum_build_minutes']*.005,
             'validation_scope':'Real small-data execution in AWS build sandbox; tenant worker and representative assays require separate evidence'}
    validate_release(release)
    Path('evidence/deployment/extended-release.json').write_text(json.dumps(release,indent=2))
    print(json.dumps({'image':release['image'],'tools':release['tools'],'build_seconds':release['build_seconds'],
                      'failed':[c['tool'] for c in qualification['cases'] if c['status']=='failed']}))

if __name__=='__main__':main()
