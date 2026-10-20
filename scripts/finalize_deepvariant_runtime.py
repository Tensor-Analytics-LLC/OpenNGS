"""Recover actual immutable evidence; never publish a runtime or tenant route."""
import hashlib
import io
import json
from pathlib import Path
import tarfile
from backend.config import aws_session
from backend.deepvariant_runtime import validate_release


def main():
    record = json.loads(Path('evidence/deployment/deepvariant-build.json').read_text())
    session = aws_session()
    build = session.client('codebuild').batch_get_builds(ids=[record['build_id']])['builds'][0]
    if build['buildStatus'] != 'SUCCEEDED':
        raise ValueError('DeepVariant build did not complete successfully')
    s3 = session.client('s3')
    proof_object = s3.get_object(Bucket=record['bucket'], Key=record['qualification_key'])
    if proof_object['ContentLength'] > 2 * 1024**2:
        raise ValueError('Qualification proof exceeds size bound')
    raw = proof_object['Body'].read()
    proof = json.loads(raw)
    artifacts = s3.get_object(Bucket=record['bucket'], Key=record['artifacts_key'])
    if artifacts['ContentLength'] > 8 * 1024**2:
        raise ValueError('Small-data qualification outputs exceed size bound')
    bundle = artifacts['Body'].read()
    positive = next(row for row in proof['cases'] if row['id'] == 'wgs-truth-control')
    with tarfile.open(fileobj=io.BytesIO(bundle), mode='r:gz') as archive:
        members = {Path(item.name).name: item for item in archive.getmembers() if item.isfile()}
        for output in positive.get('outputs', []):
            member = members.get(output['name'])
            if not member or member.size != output['bytes']:
                raise ValueError('Qualification artifact missing or truncated')
            content = archive.extractfile(member).read()
            if hashlib.sha256(content).hexdigest() != output['sha256']:
                raise ValueError('Qualification artifact digest mismatch')
    image = session.client('ecr').describe_images(repositoryName='openngs-demo', imageIds=[{'imageTag': record['tag']}])['imageDetails'][0]
    account = image['registryId']
    release = {**record, 'image': account + '.dkr.ecr.us-east-1.amazonaws.com/openngs-demo@' + image['imageDigest'],
        'tools': ['deepvariant'], 'qualification': proof, 'qualification_sha256': hashlib.sha256(raw).hexdigest(),
        'qualification_version': proof_object.get('VersionId'), 'artifacts_sha256': hashlib.sha256(bundle).hexdigest(),
        'artifacts_version': artifacts.get('VersionId'), 'compressed_image_bytes': image['imageSizeInBytes'],
        'monthly_ecr_storage_estimate_usd': round(image['imageSizeInBytes'] / 1024**3 * .10, 4),
        'build_seconds': (build['endTime'] - build['startTime']).total_seconds(),
        'validation_scope': 'Public NA12878 10kb CPU inference and explicit rejection controls; not clinical validation'}
    validate_release(release,require_isolation=False)
    Path('evidence/deployment/deepvariant-candidate.json').write_text(json.dumps(release, indent=2))
    print(json.dumps({'image': release['image'], 'passed': release['tools'],
        'state':'candidate-awaiting-readonly-isolation',
        'models': release['models'], 'build_seconds': release['build_seconds'],
        'monthly_ecr_usd': release['monthly_ecr_storage_estimate_usd'], 'qualification': positive['assertions']}))


if __name__ == '__main__':
    main()
