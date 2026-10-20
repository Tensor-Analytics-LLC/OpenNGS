"""Verify immutable AWS VEP evidence and save a candidate; never publish routes."""
import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import tarfile
import tempfile

from backend.config import aws_session
from backend import vep_runtime, vep_tools


def immutable_object(client, bucket, key, maximum):
    head = client.head_object(Bucket=bucket, Key=key)
    version = head.get('VersionId')
    if not version or version == 'null' or head['ContentLength'] > maximum:
        raise ValueError('VEP evidence requires a bounded immutable object version')
    response = client.get_object(Bucket=bucket, Key=key, VersionId=version);stream = response['Body']
    try:
        raw = stream.read(maximum + 1)
    finally:
        stream.close()
    if len(raw) != head['ContentLength'] or len(raw) > maximum or response.get('VersionId') != version:
        raise ValueError('VEP evidence object version or length changed')
    return raw, {'bucket': bucket, 'key': key, 'version_id': version, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def verify_outputs(proof, raw_archive):
    expected = {obj['name']: obj for case in proof['cases'] for obj in case.get('outputs', [])}
    if set(expected) != {'human.vcf.gz', 'human.vcf.gz.tbi', 'gff.vcf.gz', 'gff.vcf.gz.tbi'}:
        raise ValueError('VEP evidence must retain both actual VCFs and their indices')
    found = {};seen = set();expanded = 0
    with tarfile.open(fileobj=io.BytesIO(raw_archive), mode='r:gz') as archive:
        for member in archive:
            path = PurePosixPath(member.name)
            if path.is_absolute() or '..' in path.parts or member.issym() or member.islnk():
                raise ValueError('Unsafe VEP evidence archive')
            if member.isdir():
                continue
            if not member.isfile() or str(path) in seen or member.size > 4 * 1024**2:
                raise ValueError('Unexpected or duplicate VEP evidence member')
            seen.add(str(path));expanded += member.size
            if expanded > 16 * 1024**2:
                raise ValueError('VEP evidence archive exceeds its reviewed expansion limit')
            if path.name in expected:
                if len(path.parts) != 1 or path.name in found:
                    raise ValueError('VEP outputs must have unique exact basenames')
                body = archive.extractfile(member).read(member.size + 1);obj = expected[path.name]
                if len(body) != obj['bytes'] or hashlib.sha256(body).hexdigest() != obj['sha256']:
                    raise ValueError('Actual VEP output differs from its qualification SHA-256')
                found[path.name] = body
    if set(found) != set(expected):
        raise ValueError('Actual VEP output artifacts are missing')
    with tempfile.TemporaryDirectory(prefix='openngs-vep-evidence-') as folder:
        for name, body in found.items():
            Path(folder, name).write_bytes(body)
        human = vep_tools.annotation_summary(Path(folder, 'human.vcf.gz'))
        gff = vep_tools.annotation_summary(Path(folder, 'gff.vcf.gz'))
    if human['annotated_records'] != 10 or 'MRPL39' not in human['gene_symbols']:
        raise ValueError('Actual human VEP artifact lacks the expected original-cache annotations')
    expected_consequences = {'stop': 'stop_gained', 'missense': 'missense_variant', 'synonymous': 'synonymous_variant'}
    if gff['annotated_records'] != 3:
        raise ValueError('Actual spliced VEP artifact has the wrong record count')
    for name, consequence in expected_consequences.items():
        rows = [row for row in gff['records'] if row['id'] == name]
        if len(rows) != 1 or not any(consequence in a['Consequence'].split('&') and a.get('SYMBOL') == 'NGS_CONTROL' for a in rows[0]['annotations']):
            raise ValueError('Actual spliced VEP artifact disagrees with known coding changes')
    return {'actual_outputs_verified': len(found), 'human_records': 10, 'synthetic_records': 3}


def main():
    parser = argparse.ArgumentParser(description=__doc__);parser.add_argument('--build', default='evidence/deployment/vep-build.json')
    parser.add_argument('--output', default='evidence/deployment/vep-release-candidate.json');args = parser.parse_args()
    record = json.loads(Path(args.build).read_text());session = aws_session()
    build = session.client('codebuild').batch_get_builds(ids=[record['build_id']])['builds'][0]
    if build['buildStatus'] != 'SUCCEEDED' or build.get('timeoutInMinutes') != 10:
        raise ValueError('VEP requires a successful cost-bounded reviewed build')
    source, source_identity = immutable_object(session.client('s3'), record['bucket'], record['source_key'], 32 * 1024**2)
    if hashlib.sha256(source).hexdigest() != record['source_sha256']:
        raise ValueError('VEP build source archive differs from reviewed source SHA-256')
    raw, report_identity = immutable_object(session.client('s3'), record['bucket'], record['qualification_key'], 1024**2)
    proof = json.loads(raw)
    artifacts, artifacts_identity = immutable_object(session.client('s3'), record['bucket'], record['artifacts_key'], 8 * 1024**2)
    image = session.client('ecr').describe_images(repositoryName='openngs-demo', imageIds=[{'imageTag': record['tag']}])['imageDetails'][0]
    if image['imageSizeInBytes'] > record['maximum_image_compressed_bytes']:
        raise ValueError('VEP image exceeds its reviewed storage budget')
    repository_record = session.client('ecr').describe_repositories(repositoryNames=['openngs-demo'])['repositories'][0]
    if repository_record.get('repositoryName') != 'openngs-demo' or not re.fullmatch('[0-9]{12}', repository_record.get('registryId', '')):
        raise ValueError('VEP must use the exact private OpenNGS repository')
    # The ECR API can return its dual-stack .on.aws alias. Worker contracts use
    # one canonical registry identity independently of the operator API endpoint.
    repository = repository_record['registryId'] + '.dkr.ecr.us-east-1.amazonaws.com/openngs-demo'
    candidate = {**record, 'image': repository + '@' + image['imageDigest'], 'tools': ['vep'], 'qualification': proof,
                 'image_compressed_bytes': image['imageSizeInBytes'], 'image_monthly_ecr_usd_estimate': image['imageSizeInBytes'] / 1024**3 * .10,
                 'evidence': {'source': source_identity, 'qualification': report_identity, 'outputs': artifacts_identity},
                 'publication': 'Candidate only; production use additionally requires the complete verified original archive and matching reference FASTA/FAI'}
    vep_runtime.validate_release(candidate)
    candidate['artifact_verification'] = verify_outputs(proof, artifacts)
    Path(args.output).write_text(json.dumps(candidate, indent=2))
    print(json.dumps({'state': 'Qualified candidate', 'image': candidate['image'], 'aws_mutations': False,
                      'artifact_verification': candidate['artifact_verification'],
                      'production_dependency': candidate['publication']}, indent=2))


if __name__ == '__main__':
    main()
