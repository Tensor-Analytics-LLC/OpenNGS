"""Recover exact execution evidence and incremental layer footprint without publishing."""
import hashlib
import json
from pathlib import Path
from backend.config import aws_session
from backend.secondary_runtime import validate_release

def main():
    record=json.loads(Path('evidence/deployment/secondary-build.json').read_text());s=aws_session()
    build=s.client('codebuild').batch_get_builds(ids=[record['build_id']])['builds'][0]
    if build['buildStatus']!='SUCCEEDED':raise ValueError('Secondary build has not completed successfully')
    obj=s.client('s3').get_object(Bucket=record['bucket'],Key=record['qualification_key'])
    if obj['ContentLength']>1024**2:raise ValueError('Qualification exceeds its size bound')
    raw=obj['Body'].read();proof=json.loads(raw)
    ecr=s.client('ecr');image=ecr.describe_images(repositoryName='openngs-demo',imageIds=[{'imageTag':record['tag']}])['imageDetails'][0]
    def layers(digest):
        result=ecr.batch_get_image(repositoryName='openngs-demo',imageIds=[{'imageDigest':digest}])['images'][0]
        return {x['digest']:x['size'] for x in json.loads(result['imageManifest'])['layers']}
    before=layers(record['base_image'].split('@')[1]);after=layers(image['imageDigest'])
    extra=sum(size for digest,size in after.items() if digest not in before)
    release={**record,'image':record['base_image'].split('@')[0]+'@'+image['imageDigest'],
        'qualification':proof,'tools':sorted(c['tool'] for c in proof['cases'] if c['status']=='passed'),
        'qualification_sha256':hashlib.sha256(raw).hexdigest(),'qualification_version':obj.get('VersionId'),
        'build_seconds':(build['endTime']-build['startTime']).total_seconds(),
        'incremental_compressed_bytes':extra,'monthly_ecr_storage_estimate_usd':round(extra/1024**3*.10,4),
        'validation_scope':'AWS command execution on tiny engineering controls only; not clinical or full-scale validation'}
    validate_release(release)
    Path('evidence/deployment/secondary-release.json').write_text(json.dumps(release,indent=2))
    print(json.dumps({'image':release['image'],'passed':release['tools'],'incremental_bytes':extra,'monthly_ecr_usd':release['monthly_ecr_storage_estimate_usd'],
        'failed':[{k:c.get(k) for k in ['tool','error']} for c in proof['cases'] if c['status']!='passed']}))

if __name__=='__main__':main()
