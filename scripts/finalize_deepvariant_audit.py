"""Verify isolated model outputs before producing the publishable release."""
import hashlib
import io
import json
from pathlib import Path
import tarfile
from backend.config import aws_session
from backend.deepvariant_runtime import ISOLATION,validate_release


def main():
    candidate=validate_release(json.loads(Path('evidence/deployment/deepvariant-candidate.json').read_text()),require_isolation=False)
    audit=json.loads(Path('evidence/deployment/deepvariant-audit-build.json').read_text())
    if candidate['image']!=audit['candidate_image']:raise ValueError('Audit refreshed another candidate image')
    session=aws_session()
    build=session.client('codebuild').batch_get_builds(ids=[audit['build_id']])['builds'][0]
    if build['buildStatus']!='SUCCEEDED':raise ValueError('DeepVariant isolation execution did not succeed')
    s3=session.client('s3')
    obj=s3.get_object(Bucket=audit['bucket'],Key=audit['qualification_key'])
    if obj['ContentLength']>2*1024**2:raise ValueError('Oversize qualification proof')
    raw=obj['Body'].read();proof=json.loads(raw)
    artifact=s3.get_object(Bucket=audit['bucket'],Key=audit['artifacts_key'])
    if artifact['ContentLength']>8*1024**2:raise ValueError('Oversize small-data qualification outputs')
    bundle=artifact['Body'].read()
    positive=next(case for case in proof['cases'] if case['id']=='wgs-truth-control')
    with tarfile.open(fileobj=io.BytesIO(bundle),mode='r:gz') as archive:
        members={Path(member.name).name:member for member in archive.getmembers() if member.isfile()}
        for output in positive.get('outputs',[]):
            member=members.get(output['name'])
            if not member or member.size!=output['bytes']:raise ValueError('Actual isolated output missing or truncated')
            if hashlib.sha256(archive.extractfile(member).read()).hexdigest()!=output['sha256']:
                raise ValueError('Actual isolated output digest mismatch')
    image=session.client('ecr').describe_images(repositoryName='openngs-demo',imageIds=[{'imageTag':audit['tag']}])['imageDetails'][0]
    digest=candidate['image'].split('@')[0]+'@'+image['imageDigest']
    isolated={'image':digest,'source_sha256':audit['source_sha256'],'build_id':audit['build_id'],
              'restrictions':ISOLATION,'cases':proof['cases']}
    release={**candidate,'candidate_image':candidate['image'],'image':digest,'tag':audit['tag'],
        'build_id':audit['build_id'],'candidate_build_id':candidate['build_id'],'source_sha256':audit['source_sha256'],
        'qualification':proof,'isolation_qualification':isolated,'qualification_sha256':hashlib.sha256(raw).hexdigest(),
        'qualification_version':obj.get('VersionId'),'qualification_key':audit['qualification_key'],
        'artifacts_key':audit['artifacts_key'],'artifacts_sha256':hashlib.sha256(bundle).hexdigest(),
        'artifacts_version':artifact.get('VersionId'),'compressed_image_bytes':image['imageSizeInBytes'],
        'monthly_ecr_storage_estimate_usd':round(image['imageSizeInBytes']/1024**3*.10,4),
        'audit_build_seconds':(build['endTime']-build['startTime']).total_seconds()}
    validate_release(release)
    Path('evidence/deployment/deepvariant-release.json').write_text(json.dumps(release,indent=2))
    print(json.dumps({'image':digest,'tools':release['tools'],'models':release['models'],
        'qualification':positive['assertions'],'restrictions':ISOLATION,'audit_seconds':release['audit_build_seconds']}))


if __name__=='__main__':main()
