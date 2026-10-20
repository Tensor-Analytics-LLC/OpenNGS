"""Package or explicitly start one cost-bounded isolated CPU model build.

Default invocation is local/read-only. --start additionally requires the
operator's funded cost reservation identifier; this script never publishes a
runtime route or changes an existing worker image.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile
from backend.config import aws_session
from backend.deepvariant_runtime import FAMILY, UPSTREAM_IMAGE, VERSION


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', default='.state/deepvariant-build')
    parser.add_argument('--start', action='store_true')
    parser.add_argument('--reservation')
    parser.add_argument('--minutes', type=int, default=10)
    parser.add_argument('--compute', choices=['BUILD_GENERAL1_SMALL', 'BUILD_GENERAL1_MEDIUM'], default='BUILD_GENERAL1_MEDIUM')
    args = parser.parse_args()
    maximum_usd = args.minutes * (.005 if args.compute == 'BUILD_GENERAL1_SMALL' else .01)
    if not 5 <= args.minutes <= 20 or maximum_usd > .10001:
        raise ValueError('Reviewed build cap is $0.10 and at most 20 minutes')
    if args.start and not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', args.reservation or ''):
        raise ValueError('--start requires the operator cost reservation identifier')
    root = Path(args.directory)
    recipe = json.loads(Path('infra/deepvariant-runtime.json').read_text())
    if recipe['upstream_image'] != UPSTREAM_IMAGE or recipe['version'] != VERSION:
        raise ValueError('Unreviewed DeepVariant upstream recipe')
    for item in recipe['fixtures']:
        path = root / 'fixtures' / item['name']
        if path.stat().st_size != item['bytes'] or sha(path) != item['sha256']:
            raise ValueError('Run prepare_deepvariant_runtime.py; pinned fixture missing or changed')
    locks = Path('infra/deepvariant-requirements.lock').read_text().splitlines()
    hashes = {row.rsplit('sha256:', 1)[1] for row in locks if row.strip()}
    wheels = list((root / 'wheels').glob('*.whl'))
    if len(wheels) != len(hashes) or {sha(path) for path in wheels} != hashes:
        raise ValueError('Run prepare_deepvariant_runtime.py; exact binary wheel lock required')
    archive = root / 'source.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as target:
        for path in sorted(Path('backend').glob('*.py')):
            if not path.is_symlink():
                target.write(path)
        for name in ['infra/deepvariant.Dockerfile', 'infra/deepvariant-runtime.json', 'infra/deepvariant-requirements.lock',
                     'infra/cloud_worker.py', 'scripts/qualify_deepvariant_runtime.py']:
            target.write(name)
        for path in sorted((root / 'fixtures').iterdir()):
            if path.name in {r['name'] for r in recipe['fixtures']}:
                target.write(path, 'fixtures/' + path.name)
        for path in sorted(wheels):
            target.write(path, 'wheels/' + path.name)
    source_sha = sha(archive)
    tag = 'deepvariant-' + source_sha[:20]
    prefix = 'operations/builds/' + tag
    record = {'family': FAMILY, 'version': VERSION, 'upstream_image': UPSTREAM_IMAGE,
        'source_sha256': source_sha, 'requirements_sha256': sha('infra/deepvariant-requirements.lock'),
        'fixture_manifest_sha256': sha('infra/deepvariant-runtime.json'), 'models': ['WGS'],
        'tag': tag, 'archive_bytes': archive.stat().st_size, 'maximum_build_minutes': args.minutes,
        'compute': args.compute, 'maximum_build_usd': round(maximum_usd, 3),
        'upstream_compressed_bytes': recipe['upstream_compressed_bytes'],
        'monthly_ecr_base_storage_estimate_usd': round(recipe['upstream_compressed_bytes'] / 1024**3 * .10, 4)}
    if not args.start:
        print(json.dumps({**record, 'status': 'packaged-locally', 'archive': str(archive)}))
        return
    deployment = json.loads(Path('evidence/deployment/release.json').read_text())
    session = aws_session()
    bucket = deployment['bucket']
    source_key = prefix + '/source.zip'
    report_key = prefix + '/qualification.json'
    artifacts_key = prefix + '/outputs.tar.gz'
    session.client('s3').upload_file(str(archive), bucket, source_key, ExtraArgs={
        'ServerSideEncryption': 'aws:kms', 'SSEKMSKeyId': deployment['kms'],
        'Tagging': 'RuntimeFamily=' + FAMILY + '&CostReservation=' + args.reservation,
        'Metadata': {'sha256': source_sha}})
    project = session.client('codebuild').batch_get_projects(names=['openngs-demo-build'])['projects'][0]
    role = project['serviceRole'].rsplit('/', 1)[-1]
    session.client('iam').put_role_policy(RoleName=role, PolicyName='OpenNGSDeepVariantBuild', PolicyDocument=json.dumps({
        'Version': '2012-10-17', 'Statement': [
            {'Effect': 'Allow', 'Action': ['s3:GetObject', 's3:GetObjectVersion'], 'Resource': 'arn:aws:s3:::' + bucket + '/' + source_key},
            {'Effect': 'Allow', 'Action': ['s3:PutObject'], 'Resource': ['arn:aws:s3:::' + bucket + '/' + report_key, 'arn:aws:s3:::' + bucket + '/' + artifacts_key]},
            {'Effect': 'Allow', 'Action': ['kms:GenerateDataKey', 'kms:Decrypt'], 'Resource': deployment['kms'],
             'Condition': {'StringEquals': {'kms:ViaService': 's3.us-east-1.amazonaws.com'}}}]}))
    buildspec = {'version': '0.2', 'phases': {
        'pre_build': {'commands': ['aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin "$REPOSITORY"']},
        'build': {'commands': [
            'docker build --platform linux/amd64 --build-arg "DEEPVARIANT_IMAGE=$DEEPVARIANT_IMAGE" -f infra/deepvariant.Dockerfile -t "$REPOSITORY:$IMAGE_TAG" .',
            'CID=$(docker create "$REPOSITORY:$IMAGE_TAG")',
            'docker cp "$CID:/qualification/deepvariant-report.json" /tmp/qualification.json',
            'mkdir /tmp/deepvariant-outputs',
            'docker cp "$CID:/work/calls.vcf.gz" /tmp/deepvariant-outputs/',
            'docker cp "$CID:/work/calls.vcf.gz.tbi" /tmp/deepvariant-outputs/',
            'docker cp "$CID:/work/calls.runtime.json" /tmp/deepvariant-outputs/',
            'docker cp "$CID:/qualification/positive.log" /tmp/deepvariant-outputs/',
            'tar -czf /tmp/deepvariant-outputs.tar.gz -C /tmp/deepvariant-outputs .',
            'docker rm "$CID"', 'cat /tmp/qualification.json']},
        'post_build': {'commands': ['test "$CODEBUILD_BUILD_SUCCEEDING" = 1', 'docker push "$REPOSITORY:$IMAGE_TAG"',
            'aws s3 cp /tmp/qualification.json "$QUALIFICATION_URI" --sse aws:kms --sse-kms-key-id "$KMS_KEY"',
            'aws s3 cp /tmp/deepvariant-outputs.tar.gz "$OUTPUT_URI" --sse aws:kms --sse-kms-key-id "$KMS_KEY"']}}}
    variables = {'DEEPVARIANT_IMAGE': UPSTREAM_IMAGE, 'IMAGE_TAG': tag,
                 'QUALIFICATION_URI': 's3://' + bucket + '/' + report_key,
                 'OUTPUT_URI': 's3://' + bucket + '/' + artifacts_key, 'KMS_KEY': deployment['kms']}
    result = session.client('codebuild').start_build(projectName=project['name'], sourceTypeOverride='S3',
        sourceLocationOverride=bucket + '/' + source_key, buildspecOverride=json.dumps(buildspec),
        computeTypeOverride=args.compute, timeoutInMinutesOverride=args.minutes,
        environmentVariablesOverride=[{'name': key, 'value': value, 'type': 'PLAINTEXT'} for key, value in variables.items()])
    record.update(build_id=result['build']['id'], bucket=bucket, qualification_key=report_key,
                  artifacts_key=artifacts_key, reservation=args.reservation)
    Path('evidence/deployment/deepvariant-build.json').write_text(json.dumps(record, indent=2))
    print(json.dumps(record))


if __name__ == '__main__':
    main()
