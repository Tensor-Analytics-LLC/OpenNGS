"""Operator-only full-cache qualification; preparation never changes readiness.

prepare emits a worker manifest and a blocked Batch request locally. On the
deployed EC2 host, verify-reference streams the immutable full FASTA in 4 MiB
chunks and unlocks the request. The operator uploads/submits it separately.
finalize reads the actual job and immutable small outputs, then writes a local
receipt accepted by provisioning.validate_vep_bundle. This controller never
submits jobs, writes S3, changes the database, or publishes a Ready control.
"""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import tempfile
import time
import uuid

from backend import vep_runtime as vep
from scripts.acquire_references import CHUNK_BYTES, derive_dictionary, verify_cloud_runtime

GIB = 1024**3
SCRATCH_BYTES = 80 * GIB
MAX_OUTPUT_BYTES = 8 * 1024**2
MAXIMUM_PRETAX_USD = .15
REFERENCE_SHA256 = '93157a161863464c9435062fd67c173fdaf99cb8b32f1455018361387ffa5564'
INDEX_SHA256 = 'edefd93c489dc1baefad312f40388089f8db5cf6dcc3ba0955669ead274e8b6b'
CONTROL_SHA256 = '9a36111e900fdf039fe0be1750df09f115e9f0310b8143eab276df34bdfbe0e0'
ARCHIVE_DATASET = 'ds_migration_b8ac16c0fa8043ee256faa697245703f6573083b'
REFERENCE_CONTIGS = 3366
COMMAND = ['/usr/bin/python3', '/opt/studio/cloud_worker.py']


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def save(path, value):
    """Never overwrite different evidence from an earlier operation."""
    path = Path(path);raw = encoded(value) + b'\n'
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError('Evidence already exists; preserve it and use a fresh directory: ' + str(path))
    else:
        with path.open('xb') as output:
            output.write(raw)
        path.chmod(0o600)


def cloud_object(value):
    from backend.provisioning import _vep_object
    return _vep_object(value)


def s3_args(obj):
    bucket, key = obj['uri'][5:].split('/', 1)
    return {'Bucket': bucket, 'Key': key, 'VersionId': obj['version_id']}


def check_head(client, obj):
    head = client.head_object(**s3_args(obj))
    if head.get('VersionId') != obj['version_id'] or head.get('ContentLength') != obj['bytes']:
        raise ValueError('Exact immutable source version or byte length differs')
    return head


def read_small(client, obj, maximum=MAX_OUTPUT_BYTES):
    cloud_object(obj)
    if obj['bytes'] > maximum:
        raise ValueError('Small qualification evidence exceeds its retained-byte bound')
    check_head(client, obj)
    response = client.get_object(**s3_args(obj))
    try:
        raw = response['Body'].read(maximum + 1)
    finally:
        response['Body'].close()
    if len(raw) != obj['bytes'] or sha256(raw) != obj['sha256']:
        raise ValueError('Immutable qualification evidence bytes or SHA-256 differ')
    return raw


def validate_config(config):
    owner = config.get('owner_id', '')
    if not re.fullmatch(r'u_[a-f0-9]{32}', owner) or not re.fullmatch(r'cost_[a-f0-9]{32}', config.get('reservation_id', '')):
        raise ValueError('An exact existing owner and funded shared reservation are required')
    if config.get('archive_dataset_id') != ARCHIVE_DATASET:
        raise ValueError('Use the original registered cache dataset; never fabricate a replacement row')
    kms = re.fullmatch(r'arn:aws:kms:us-east-1:([0-9]{12}):key/[A-Za-z0-9-]+', config.get('kms_key_arn', ''))
    if not kms or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', config.get('bucket', '')):
        raise ValueError('The private us-east-1 deployment bucket and KMS key are required')
    account = kms[1]
    for field, resource in [('job_definition', r'job-definition/[^/:]+:[1-9][0-9]*'), ('job_queue', r'job-queue/[^/]+')]:
        if not re.fullmatch('arn:aws:batch:us-east-1:' + account + ':' + resource, config.get(field, '')):
            raise ValueError('Exact private Batch queue and revision-pinned definition are required')
    if not re.fullmatch('arn:aws:iam::' + account + r':role/[A-Za-z0-9+=,.@_/-]+', config.get('job_role_arn', '')):
        raise ValueError('Supply the exact existing private owner task role')
    expected = {'archive': (vep.CACHE_ARCHIVE_SHA256, vep.CACHE_ARCHIVE_BYTES),
                'reference': (REFERENCE_SHA256, 3249912778), 'index': (INDEX_SHA256, 160928),
                'variants': (CONTROL_SHA256, 384)}
    for name, identity in expected.items():
        obj = cloud_object(config.get(name))
        if (obj['sha256'], obj['bytes']) != identity:
            raise ValueError('Input differs from the exact complete cache, Broad reference, FAI or official ten-record control: ' + name)
        if not obj['uri'][5:].split('/', 1)[1].startswith('tenants/' + owner + '/'):
            raise ValueError('The existing worker requires every admitted source under the original owner tenant prefix')
    return config


def prepare(config, release, *, run_id=None, attempt_id=None):
    validate_config(config);vep.validate_release(release)
    if release['image'].split('.')[0] != config['kms_key_arn'].split(':')[4]:
        raise ValueError('Published image and deployment account differ')
    return prepared_plan(config, release['image'], release['source_sha256'],
                         run_id or 'run_' + uuid.uuid4().hex, attempt_id or 'a_' + uuid.uuid4().hex)


def prepared_plan(config, image, source_sha256, rid, aid):
    """Deterministic contract, also reconstructed before trusting saved plans."""
    if not re.fullmatch('run_[a-f0-9]{32}', rid) or not re.fullmatch('a_[a-f0-9]{32}', aid):
        raise ValueError('Qualification identifiers must be fresh bounded run and attempt IDs')
    prefix = f"tenants/{config['owner_id']}/runs/{rid}/{aid}/"
    targets = {'archive': '/inputs/database/0/' + vep.CACHE_ARCHIVE_NAME,
               'reference': '/inputs/reference/0/reference.fa', 'index': '/inputs/reference/0/reference.fa.fai',
               'variants': '/inputs/variants/0/cache-control.vcf'}
    inputs, grants = [], []
    for name, target in targets.items():
        obj = config[name];source = s3_args(obj)
        inputs.append({'bucket': source['Bucket'], 'key': source['Key'], 'version_id': source['VersionId'],
                       'target': target, 'sha256': obj['sha256'], 'bytes': obj['bytes']})
        if source['Bucket'] != config['bucket']:
            grants.append({**source, 'sha256': obj['sha256'], 'bytes': obj['bytes']})
    required = sum(i['bytes'] for i in inputs) + vep.CACHE_EXTRACTED_BYTES + 2 * GIB
    if required > SCRATCH_BYTES:
        raise ValueError('Complete cache plus inputs exceed the declared 80 GiB scratch envelope')
    argv = ['/usr/bin/python3', '-m', 'backend.vep_tools', '--variants', targets['variants'],
            '--cache-archive', targets['archive'], '--fasta', targets['reference'], '--fai', targets['index'],
            '--output', '/work/annotated.vcf.gz']
    manifest = {'schema_version': 1, 'tenant': config['owner_id'], 'run': rid, 'attempt': aid,
                'bucket': config['bucket'], 'prefix': prefix, 'kms_key_arn': config['kms_key_arn'],
                'inputs': inputs, 'source_grants': grants, 'wall_seconds': 3600,
                'max_output_bytes': MAX_OUTPUT_BYTES, 'max_scratch_bytes': SCRATCH_BYTES,
                'plan': {'steps': [{'argv': argv, 'stdout': None}], 'outputs': {
                    'variants': [{'file': 'annotated.vcf.gz', 'format': 'INDEXED_VCF',
                                  'metadata': {'index': '/work/annotated.vcf.gz.tbi', 'assembly': 'GRCh38'}}],
                    'report': [{'file': 'annotated.vcf.gz.runtime.json', 'format': 'REPORT', 'metadata': {}}]}}}
    request = {'jobName': 'vep-complete-' + aid, 'jobQueue': config['job_queue'],
               'jobDefinition': config['job_definition'], 'containerOverrides': {
                   'command': COMMAND + [config['bucket'], prefix + 'manifest.json'],
                   'resourceRequirements': [{'type': 'VCPU', 'value': '1'}, {'type': 'MEMORY', 'value': '4096'}]},
               'timeout': {'attemptDurationSeconds': 3600}, 'retryStrategy': {'attempts': 1},
               'tags': {'Project': 'OpenNGS-Shared-Qualification', 'TenantId': config['owner_id'],
                        'CostReservation': config['reservation_id'], 'RuntimeFamily': vep.FAMILY}}
    return {'state': 'prepared-unready', 'config': config, 'image': image,
            'runtime_source_sha256': source_sha256, 'manifest': manifest, 'submit_request': request,
            'maximum_pretax_usd': MAXIMUM_PRETAX_USD, 'scratch_estimate_bytes': required,
            'scope': 'Operator full-cache engineering qualification; no clinical claim or dataset readiness change'}


def validate_operator(prepared):
    """Read live authorization and existing funding without creating any grant."""
    from backend import db, entitlements, provisioning
    config = prepared['config'];owner = config['owner_id']
    user = db.query('SELECT role,frozen FROM users WHERE id=?', (owner,), one=True)
    hold = db.query('SELECT r.*,a.account FROM cost_reservations r JOIN cost_allocations a ON a.reservation_id=r.id WHERE r.id=?',
                    (config['reservation_id'],), one=True)
    if not user or user['role'] != 'admin' or user['frozen'] or not entitlements.research(owner):
        raise ValueError('The exact existing owner must remain an active administrator with funded Research Pro')
    if (not hold or hold['user_id'] != owner or hold['account'] != 'shared-platform' or hold['state'] != 'reserved'
            or hold['reserved_micro'] - hold['charged_micro'] < round(MAXIMUM_PRETAX_USD * 1000000)):
        raise ValueError('An existing shared-platform hold must cover the bounded $0.15 qualification')
    live = vep.published();profile = provisioning.profile(owner);route = profile.get('runtime_profiles', {}).get('vep', {})
    if (not live or live['image'] != prepared['image'] or live['source_sha256'] != prepared['runtime_source_sha256']
            or profile.get('state') != 'Ready' or profile.get('bucket') != config['bucket']
            or profile.get('queue') != config['job_queue'] or route.get('image') != prepared['image']
            or route.get('definition') != config['job_definition']):
        raise ValueError('The live owner route must match the exact published VEP image and private definition')


def validate_prepared(prepared):
    config = validate_config(prepared['config'])
    reconstructed = prepared_plan(config, prepared['image'], prepared['runtime_source_sha256'],
                                  prepared['manifest']['run'], prepared['manifest']['attempt'])
    if prepared != reconstructed:
        raise ValueError('Saved preparation differs from the fixed reviewed CLI and resource contract')


def validate_definition(prepared, definition):
    config = prepared['config'];props = definition.get('containerProperties', {})
    mounts = {r.get('containerPath'): r for r in props.get('mountPoints', [])}
    if (definition.get('jobDefinitionArn') != config['job_definition'] or definition.get('status') != 'ACTIVE'
            or definition.get('platformCapabilities') != ['FARGATE'] or props.get('image') != prepared['image']
            or props.get('command') != COMMAND or props.get('jobRoleArn') != config['job_role_arn']
            or not props.get('readonlyRootFilesystem') or props.get('privileged')
            or props.get('user') != '1000:1000' or props.get('ephemeralStorage', {}).get('sizeInGiB') != 200
            or any(name not in mounts or mounts[name].get('readOnly') is not False for name in ['/work', '/inputs', '/tmp'])
            or definition.get('tags', {}).get('TenantId') != config['owner_id']
            or definition.get('tags', {}).get('NGSRuntimeFamily') != vep.FAMILY
            or definition.get('tags', {}).get('NGSWorkerContract') != '1'):
        raise ValueError('Private owner definition differs from the published immutable Fargate isolation contract')


def verify_reference(client, reference, index, runtime):
    """Full raw-byte digest and every FAI field; no full local FASTA copy."""
    cloud_object(reference);cloud_object(index);check_head(client, reference)
    fai = read_small(client, index, 1024**2)
    response = client.get_object(**s3_args(reference))
    try:
        dictionary = derive_dictionary(response['Body'].iter_chunks(CHUNK_BYTES), fai, reference['bytes'],
            base64.b64encode(bytes.fromhex(reference['sha256'])).decode(),
            max_contigs=10000, dictionary_max_bytes=2 * 1024**2)
    finally:
        response['Body'].close()
    return {'reference': cloud_object(reference), 'index': cloud_object(index),
            'fasta_sha256': reference['sha256'], 'fai_sha256': index['sha256'],
            'all_fai_fields_validated': True, 'contigs': sum(row.startswith(b'@SQ\t') for row in dictionary.splitlines()),
            'dictionary_sha256': sha256(dictionary), 'algorithm': 'scripts.acquire_references.derive_dictionary',
            'chunk_bytes': CHUNK_BYTES, 'max_contigs': 10000, 'dictionary_max_bytes': 2 * 1024**2,
            'runtime': runtime, 'verified_at': time.time()}


def validate_reference_proof(prepared, proof):
    config = prepared['config']
    if (proof.get('reference') != cloud_object(config['reference']) or proof.get('index') != cloud_object(config['index'])
            or proof.get('fasta_sha256') != REFERENCE_SHA256 or proof.get('fai_sha256') != INDEX_SHA256
            or proof.get('all_fai_fields_validated') is not True or proof.get('contigs') != REFERENCE_CONTIGS
            or proof.get('algorithm') != 'scripts.acquire_references.derive_dictionary'
            or proof.get('chunk_bytes') != CHUNK_BYTES or proof.get('max_contigs') != 10000
            or proof.get('dictionary_max_bytes') != 2 * 1024**2
            or not re.fullmatch('[a-f0-9]{64}', proof.get('dictionary_sha256', ''))
            or proof.get('runtime', {}).get('account') != config['kms_key_arn'].split(':')[4]
            or proof.get('runtime', {}).get('region') != 'us-east-1' or not proof.get('runtime', {}).get('instance_id')):
        raise ValueError('Full Broad reference/FAI proof is absent, partial, or bound to different immutable objects')
    return proof


def snapshot_small(client, uri, maximum=MAX_OUTPUT_BYTES):
    bucket, key = uri[5:].split('/', 1)
    head = client.head_object(Bucket=bucket, Key=key)
    # Discover one immutable version, then read only that version and hash bytes.
    obj = {'uri': uri, 'version_id': head.get('VersionId'), 'bytes': head['ContentLength'], 'sha256': '0' * 64}
    cloud_object(obj)
    if obj['bytes'] > maximum:
        raise ValueError('Output exceeds the bounded qualification allowance')
    response = client.get_object(**s3_args(obj))
    try:
        raw = response['Body'].read(maximum + 1)
    finally:
        response['Body'].close()
    if len(raw) != obj['bytes']:
        raise ValueError('Immutable output length differs')
    obj['sha256'] = sha256(raw)
    return obj, raw, head


def finalize(prepared, reference_proof, job, definition, client):
    """Return a receipt only after actual full-cache Batch execution succeeds."""
    validate_prepared(prepared);config = prepared['config'];validate_reference_proof(prepared, reference_proof)
    validate_definition(prepared, definition)
    expected = prepared['submit_request'];container = job.get('container', {})
    if (job.get('status') != 'SUCCEEDED' or not job.get('jobId') or container.get('exitCode') != 0
            or job.get('jobDefinition') != config['job_definition'] or job.get('jobQueue') != config['job_queue']
            or job.get('jobName') != expected['jobName'] or container.get('image') != prepared['image']
            or container.get('jobRoleArn') != config['job_role_arn']
            or container.get('command') != expected['containerOverrides']['command']
            or {r['type']: r['value'] for r in container.get('resourceRequirements', [])} != {'VCPU': '1', 'MEMORY': '4096'}
            or job.get('timeout') != expected['timeout'] or job.get('retryStrategy', {}).get('attempts') != 1
            or any(job.get('tags', {}).get(k) != v for k, v in expected['tags'].items())
            or not job.get('startedAt') or not job.get('stoppedAt') or job['stoppedAt'] < job['startedAt']):
        raise ValueError('Only the actual successful exact-image, owner-role, bounded Batch job can qualify the bundle')
    prefix = prepared['manifest']['prefix'];base = 's3://' + config['bucket'] + '/' + prefix
    manifest_obj, raw, head = snapshot_small(client, base + 'manifest.json', 2 * 1024**2)
    if (json.loads(raw) != prepared['manifest'] or head['LastModified'].timestamp() * 1000 > job['startedAt']):
        raise ValueError('The immutable actual manifest differs or was replaced after job start')
    result_obj, raw, _ = snapshot_small(client, base + 'result.json')
    result = json.loads(raw)
    if set(result) != {'variants', 'report'} or any(len(result[key]) != 1 for key in result):
        raise ValueError('The committed worker result must contain exactly the annotated VCF and runtime report')
    variant, report = result['variants'][0], result['report'][0]
    locations = [variant['path'], variant.get('metadata', {}).get('index', ''), report['path']]
    if any(not uri.startswith(base + 'outputs/') for uri in locations):
        raise ValueError('Qualification outputs must remain in this exact private owner attempt')
    objects, bodies = [], []
    for uri in locations:
        obj, body, _ = snapshot_small(client, uri)
        objects.append(obj);bodies.append(body)
    if sum(obj['bytes'] for obj in objects) > MAX_OUTPUT_BYTES:
        raise ValueError('Combined outputs exceeded the accepted allowance')
    for output, obj in [(variant, objects[0]), (report, objects[2])]:
        if output.get('bytes') != obj['bytes'] or output.get('digest') != 'sha256:' + obj['sha256']:
            raise ValueError('Committed artifact identity differs from its actual immutable bytes')
    if variant['metadata'].get('index_sha256') != objects[1]['sha256']:
        raise ValueError('Committed index identity differs from its actual immutable bytes')
    runtime = json.loads(bodies[2])
    if (runtime.get('version') != vep.VERSION or runtime.get('cache_version') != vep.CACHE_VERSION
            or runtime.get('assembly') != vep.ASSEMBLY or runtime.get('offline') is not True
            or runtime.get('reference_sha256') != REFERENCE_SHA256 or runtime.get('annotated_records') != 10
            or runtime.get('cache') != {'members': vep.CACHE_MEMBER_COUNT, 'bytes': vep.CACHE_EXTRACTED_BYTES,
                'archive_sha256': vep.CACHE_ARCHIVE_SHA256, 'member_manifest_sha256': vep.CACHE_MANIFEST_SHA256}):
        raise ValueError('Actual worker did not attest every complete original cache member and exact reference')
    import pysam
    from backend.vep_tools import annotation_summary, vcf_rows
    control = read_small(client, config['variants'], 4096)
    with tempfile.TemporaryDirectory(prefix='vep-complete-output-') as tmp:
        path = Path(tmp) / 'annotated.vcf.gz';path.write_bytes(bodies[0])
        Path(str(path) + '.tbi').write_bytes(bodies[1]);vcf = Path(tmp) / 'control.vcf';vcf.write_bytes(control)
        summary = annotation_summary(path)
        with pysam.TabixFile(str(path)) as indexed:
            indexed_records = sum(1 for contig in indexed.contigs for _ in indexed.fetch(contig))
        actual = {(r['contig'].removeprefix('chr'), r['position'], r['id'], r['ref'], r['alt']) for r in summary['records']}
        truth = {(r[0].removeprefix('chr'), int(r[1]), r[2], r[3], r[4]) for r in vcf_rows(vcf)}
    if (summary['annotated_records'] != 10 or indexed_records != 10 or actual != truth
            or 'MRPL39' not in summary['gene_symbols']
            or not {'synonymous_variant', 'intron_variant'}.issubset(summary['consequences'])):
        raise ValueError('Actual indexed output must retain all ten official alleles with expected MRPL39 consequences')
    receipt = {'state': 'Ready', 'verification': 'complete-vep-cache-cloud-v1', 'owner_id': config['owner_id'],
        'image': prepared['image'], 'cache_version': vep.CACHE_VERSION, 'assembly': vep.ASSEMBLY,
        **{key: cloud_object(config[key]) for key in ['archive', 'reference', 'index']},
        'complete_cache': {'members': vep.CACHE_MEMBER_COUNT, 'extracted_bytes': vep.CACHE_EXTRACTED_BYTES,
            'member_manifest_sha256': vep.CACHE_MANIFEST_SHA256, 'info_sha256': vep.CACHE_INFO_SHA256},
        'reference_verification': reference_proof,
        'execution': {'status': 'SUCCEEDED', 'job_id': job['jobId'], 'scratch_bytes': SCRATCH_BYTES,
            'all_members_verified': True, 'artifact': objects[2], 'outputs': objects,
            'manifest': manifest_obj, 'result': result_obj, 'indexed_records': indexed_records,
            'image': prepared['image'], 'job_definition': config['job_definition'],
            'reservation_id': config['reservation_id'], 'reference_proof_sha256': sha256(encoded(reference_proof))},
        'scope': 'Full-cache software integration with ten public alleles; no clinical interpretation or accuracy claim'}
    from backend.provisioning import validate_vep_bundle
    validate_vep_bundle(receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['prepare', 'verify-reference', 'finalize'])
    parser.add_argument('--config');parser.add_argument('--runtime-release', default='evidence/deployment/vep-release.json')
    parser.add_argument('--deployment-release', default='evidence/deployment/release.json')
    parser.add_argument('--directory', required=True);parser.add_argument('--job-id');parser.add_argument('--execute', action='store_true')
    args = parser.parse_args();directory = Path(args.directory)
    if args.phase == 'prepare':
        if args.execute or not args.config:
            raise ValueError('prepare requires --config and performs local preparation only')
        prepared = prepare(json.loads(Path(args.config).read_bytes()), json.loads(Path(args.runtime_release).read_bytes()))
        save(directory / 'prepared.json', prepared);save(directory / 'manifest.json', prepared['manifest'])
        save(directory / 'submit-request.draft.json', prepared['submit_request'])
        print(json.dumps({'state': 'prepared-unready', 'aws_mutations': False, 'maximum_pretax_usd': MAXIMUM_PRETAX_USD,
                          'requires': 'AWS-host full reference proof before operator upload/Batch submission'}));return
    if not args.execute:
        raise ValueError('AWS-host evidence reads require explicit --execute; no job submission or publication is performed')
    prepared = json.loads((directory / 'prepared.json').read_bytes());validate_prepared(prepared)
    deployment = json.loads(Path(args.deployment_release).read_bytes())
    if prepared['config']['kms_key_arn'] != deployment.get('kms'):
        raise ValueError('The qualification must use the exact deployed private KMS key')
    runtime = verify_cloud_runtime(deployment);validate_operator(prepared)
    from backend.config import aws_session
    from botocore.config import Config
    session = aws_session();s3 = session.client('s3', config=Config(read_timeout=900, retries={'total_max_attempts': 1}))
    batch = session.client('batch');definitions = batch.describe_job_definitions(jobDefinitions=[prepared['config']['job_definition']])['jobDefinitions']
    if len(definitions) != 1:
        raise ValueError('Exact private definition was not found')
    validate_definition(prepared, definitions[0])
    for name in ['archive', 'reference', 'index', 'variants']:
        check_head(s3, prepared['config'][name])
    if args.phase == 'verify-reference':
        proof_path = directory / 'reference-verification.json'
        proof = (json.loads(proof_path.read_bytes()) if proof_path.exists() else
                 verify_reference(s3, prepared['config']['reference'], prepared['config']['index'], runtime))
        validate_reference_proof(prepared, proof);save(directory / 'reference-verification.json', proof)
        save(directory / 'submit-request.json', prepared['submit_request'])
        print(json.dumps({'state': 'reference-verified-cache-unready', 'contigs': proof['contigs'],
                          'aws_mutations': False, 'next': 'Operator separately uploads manifest.json with KMS and submits submit-request.json'}));return
    if not args.job_id:
        raise ValueError('finalize requires the actual --job-id')
    jobs = batch.describe_jobs(jobs=[args.job_id])['jobs']
    if len(jobs) != 1:
        raise ValueError('Actual qualification job was not found')
    proof = json.loads((directory / 'reference-verification.json').read_bytes())
    receipt = finalize(prepared, proof, jobs[0], definitions[0], s3)
    save(directory / 'actual-job.json', jobs[0]);save(directory / 'complete-bundle.json', receipt)
    print(json.dumps({'state': 'qualified-receipt-local-only', 'job_id': args.job_id, 'members': vep.CACHE_MEMBER_COUNT,
                      'annotated_records': 10, 'aws_mutations': False, 'published': False}))


if __name__ == '__main__':
    main()
