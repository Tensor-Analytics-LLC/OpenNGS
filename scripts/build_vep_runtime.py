"""Package the isolated VEP worker; explicit --start requires a cost reservation.

Default invocation performs no AWS operations. A launched build has a hard
10-minute MEDIUM/$0.10 cap and cannot publish an application runtime route.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile

from backend.config import aws_session
from backend.vep_runtime import (ASSEMBLY, CACHE_MANIFEST_SHA256, CACHE_VERSION, FAMILY,
                                  FIXTURE_MANIFEST_SHA256, UPSTREAM_IMAGE, VERSION, WORKER_APT_LOCK_SHA256)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def package(directory):
    recipe = json.loads(Path('infra/vep-runtime.json').read_text())
    if (recipe['upstream_image'], recipe['version'], recipe['cache_version']) != (UPSTREAM_IMAGE, VERSION, CACHE_VERSION):
        raise ValueError('Unreviewed VEP recipe')
    if sha('infra/vep-worker-apt.lock') != WORKER_APT_LOCK_SHA256 or recipe.get('worker_apt_lock_sha256') != WORKER_APT_LOCK_SHA256:
        raise ValueError('Unreviewed VEP Python worker apt lock')
    if sha(directory / 'fixture-manifest.json') != FIXTURE_MANIFEST_SHA256 or sha(directory / 'production-cache-members.json') != CACHE_MANIFEST_SHA256:
        raise ValueError('Run prepare_vep_runtime.py; exact fixture and complete-cache manifests are required')
    fixture = json.loads((directory / 'fixture-manifest.json').read_bytes())
    for row in fixture['files']:
        path = directory / 'fixtures' / row['path']
        if path.stat().st_size != row['bytes'] or sha(path) != row['sha256']:
            raise ValueError('VEP fixture bytes differ')
    required = {line.rsplit('sha256:', 1)[1] for line in Path('infra/vep-requirements.lock').read_text().splitlines() if line.strip()}
    wheels = list((directory / 'wheels').glob('*.whl'))
    if len(wheels) != len(required) or {sha(path) for path in wheels} != required:
        raise ValueError('Exact hash-locked worker dependency wheels are required')
    archive = directory / 'source.zip'
    sources = ['backend/__init__.py', 'backend/vep_runtime.py', 'backend/vep_tools.py', 'infra/vep.Dockerfile',
               'infra/vep-runtime.json', 'infra/vep-requirements.lock', 'infra/vep-worker-apt.lock', 'infra/cloud_worker.py', 'scripts/qualify_vep_runtime.py']
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as target:
        for name in sources:
            target.write(name)
        for name in ['fixture-manifest.json', 'production-cache-members.json']:
            target.write(directory / name, name)
        for path in sorted((directory / 'fixtures').rglob('*')):
            if path.is_file() and not path.is_symlink():
                target.write(path, 'fixtures/' + str(path.relative_to(directory / 'fixtures')))
        for path in sorted(wheels):
            target.write(path, 'wheels/' + path.name)
    return archive, recipe


def main():
    parser = argparse.ArgumentParser(description=__doc__);parser.add_argument('--directory', default='.state/vep-build')
    parser.add_argument('--start', action='store_true');parser.add_argument('--reservation');args = parser.parse_args()
    if args.start and not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', args.reservation or ''):
        raise ValueError('--start requires the operator funded cost reservation identifier')
    root = Path(args.directory);archive, recipe = package(root);source_sha = sha(archive);tag = 'vep-' + source_sha[:20]
    prefix = 'operations/builds/' + tag
    record = {'family': FAMILY, 'version': VERSION, 'cache_version': CACHE_VERSION, 'assembly': ASSEMBLY,
              'upstream_image': UPSTREAM_IMAGE, 'source_sha256': source_sha, 'requirements_sha256': sha('infra/vep-requirements.lock'),
              'worker_apt_lock_sha256': sha('infra/vep-worker-apt.lock'),
              'fixture_manifest_sha256': FIXTURE_MANIFEST_SHA256, 'cache_manifest_sha256': CACHE_MANIFEST_SHA256,
              'tag': tag, 'archive_bytes': archive.stat().st_size, 'maximum_build_minutes': 10,
              'compute': 'BUILD_GENERAL1_MEDIUM', 'maximum_build_usd': .10,
              'maximum_image_compressed_bytes': 1024**3, 'maximum_monthly_ecr_usd': .10,
              'upstream_compressed_bytes': recipe['upstream_compressed_bytes']}
    if not args.start:
        print(json.dumps({**record, 'status': 'packaged-locally', 'aws_mutations': False}, indent=2));return
    deployment = json.loads(Path('evidence/deployment/release.json').read_text());session = aws_session();bucket = deployment['bucket']
    source_key = prefix + '/source.zip';report_key = prefix + '/qualification.json';artifacts_key = prefix + '/outputs.tar.gz'
    session.client('s3').upload_file(str(archive), bucket, source_key, ExtraArgs={
        'ServerSideEncryption': 'aws:kms', 'SSEKMSKeyId': deployment['kms'],
        'Tagging': 'RuntimeFamily=' + FAMILY + '&CostReservation=' + args.reservation,
        'Metadata': {'sha256': source_sha}})
    project = session.client('codebuild').batch_get_projects(names=['openngs-demo-build'])['projects'][0]
    role = project['serviceRole'].rsplit('/', 1)[-1]
    session.client('iam').put_role_policy(RoleName=role, PolicyName='OpenNGSVEPBuild', PolicyDocument=json.dumps({
        'Version': '2012-10-17', 'Statement': [
            {'Effect': 'Allow', 'Action': ['s3:GetObject', 's3:GetObjectVersion'], 'Resource': 'arn:aws:s3:::' + bucket + '/' + source_key},
            {'Effect': 'Allow', 'Action': ['s3:PutObject'], 'Resource': ['arn:aws:s3:::' + bucket + '/' + report_key, 'arn:aws:s3:::' + bucket + '/' + artifacts_key]},
            {'Effect': 'Allow', 'Action': ['kms:GenerateDataKey', 'kms:Decrypt'], 'Resource': deployment['kms'],
             'Condition': {'StringEquals': {'kms:ViaService': 's3.us-east-1.amazonaws.com'}}}]}))
    buildspec = {'version': '0.2', 'phases': {
        'pre_build': {'commands': ['aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin "$REPOSITORY"']},
        'build': {'commands': [
            'docker build --platform linux/amd64 --build-arg "VEP_IMAGE=$VEP_IMAGE" -f infra/vep.Dockerfile -t "$REPOSITORY:$IMAGE_TAG" .',
            'mkdir -p /tmp/vep-work && chmod 777 /tmp/vep-work',
            'docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges --user 1000:1000 --cpus 2 --memory 2500m --tmpfs /tmp:rw,exec,uid=1000,gid=1000,size=128m -v /tmp/vep-work:/work:rw -v "$CODEBUILD_SRC_DIR/fixtures:/inputs/fixtures:ro" "$REPOSITORY:$IMAGE_TAG" /usr/bin/python3 /opt/studio/qualify_vep_runtime.py /work/qualification.json',
            'python3 -c "import json,subprocess; image=json.loads(subprocess.check_output([\'docker\',\'image\',\'inspect\',\'$REPOSITORY:$IMAGE_TAG\']))[0]; assert image[\'Size\'] <= 1073741824, \'VEP image storage cap exceeded\'"']},
        'post_build': {'commands': [
            'if test -d /tmp/vep-work; then tar -czf /tmp/vep-outputs.tar.gz -C /tmp/vep-work .; aws s3 cp /tmp/vep-outputs.tar.gz "$OUTPUT_URI" --sse aws:kms --sse-kms-key-id "$KMS_KEY"; fi',
            'if test -f /tmp/vep-work/qualification.json; then aws s3 cp /tmp/vep-work/qualification.json "$QUALIFICATION_URI" --sse aws:kms --sse-kms-key-id "$KMS_KEY"; fi',
            'test "$CODEBUILD_BUILD_SUCCEEDING" = 1', 'docker push "$REPOSITORY:$IMAGE_TAG"']}}}
    variables = {'VEP_IMAGE': UPSTREAM_IMAGE, 'IMAGE_TAG': tag, 'QUALIFICATION_URI': 's3://' + bucket + '/' + report_key,
                 'OUTPUT_URI': 's3://' + bucket + '/' + artifacts_key, 'KMS_KEY': deployment['kms']}
    started = session.client('codebuild').start_build(projectName=project['name'], sourceTypeOverride='S3',
        sourceLocationOverride=bucket + '/' + source_key, buildspecOverride=json.dumps(buildspec),
        computeTypeOverride='BUILD_GENERAL1_MEDIUM', timeoutInMinutesOverride=10,
        environmentVariablesOverride=[{'name': k, 'value': v, 'type': 'PLAINTEXT'} for k, v in variables.items()])
    record.update(build_id=started['build']['id'], bucket=bucket, source_key=source_key,
                  qualification_key=report_key, artifacts_key=artifacts_key, reservation=args.reservation)
    Path('evidence/deployment/vep-build.json').write_text(json.dumps(record, indent=2));print(json.dumps(record, indent=2))


if __name__ == '__main__':
    main()
