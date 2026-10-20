"""Controller fixtures are synthetic validator inputs, never execution proof."""
import base64
from datetime import datetime, timezone
import io
from pathlib import Path

import pysam
import pytest

from backend import vep_runtime as runtime
from scripts import qualify_complete_vep_cache as controller
from scripts.acquire_references import CHUNK_BYTES, derive_dictionary
from test_vep_runtime import release

OWNER = 'u_' + 'a' * 32
ACCOUNT = '509505638328'
CONTROL = ('##fileformat=VCFv4.0\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n'
    '21\t25587758\trs116645811\tG\tA\t.\t.\t.\n21\t25592836\trs1135638\tG\tA\t.\t.\t.\n'
    '21\t25592860\trs10576\tT\tC\t.\t.\t.\n21\t25592893\trs1057885\tT\tC\t.\t.\t.\n'
    '21\t25603832\trs116331755\tA\tG\t.\t.\t.\n21\t25603910\trs7278168\tC\tT\t.\t.\t.\n'
    '21\t25603925\trs7278284\tC\tT\t.\t.\t.\n21\t25606478\trs75377686\tT\tC\t.\t.\t.\n'
    '21\t25606638\trs3989369\tA\tG\t.\t.\t.\n21\t25607440\trs61735760\tC\tT\t.\t.\t.\n').encode()


def obj(name, digest, size):
    return {'uri': 's3://private/tenants/' + OWNER + '/datasets/existing/' + name,
            'version_id': 'opaque-immutable-' + name, 'sha256': digest, 'bytes': size}


def config():
    return {'owner_id': OWNER, 'reservation_id': 'cost_' + 'c' * 32, 'archive_dataset_id': controller.ARCHIVE_DATASET,
            'bucket': 'private', 'kms_key_arn': 'arn:aws:kms:us-east-1:' + ACCOUNT + ':key/existing-key',
            'job_queue': 'arn:aws:batch:us-east-1:' + ACCOUNT + ':job-queue/existing-owner',
            'job_definition': 'arn:aws:batch:us-east-1:' + ACCOUNT + ':job-definition/existing-vep:3',
            'job_role_arn': 'arn:aws:iam::' + ACCOUNT + ':role/existing-owner-task',
            'archive': obj(runtime.CACHE_ARCHIVE_NAME, runtime.CACHE_ARCHIVE_SHA256, runtime.CACHE_ARCHIVE_BYTES),
            'reference': obj('reference.fa', controller.REFERENCE_SHA256, 3249912778),
            'index': obj('reference.fa.fai', controller.INDEX_SHA256, 160928),
            'variants': obj('cache-control.vcf', controller.CONTROL_SHA256, 384)}


def prepared():
    return controller.prepare(config(), release(), run_id='run_' + 'd' * 32, attempt_id='a_' + 'e' * 32)


def test_preparation_is_unready_and_exactly_bounds_full_cache_execution():
    plan = prepared();manifest = plan['manifest'];request = plan['submit_request']
    assert plan['state'] == 'prepared-unready' and 'complete_cache' not in plan
    assert manifest['max_scratch_bytes'] == 80 * 1024**3
    assert plan['scratch_estimate_bytes'] < manifest['max_scratch_bytes']
    assert manifest['wall_seconds'] == request['timeout']['attemptDurationSeconds'] == 3600
    assert request['containerOverrides']['resourceRequirements'] == [{'type': 'VCPU', 'value': '1'}, {'type': 'MEMORY', 'value': '4096'}]
    assert len(manifest['inputs']) == 4 and all(item['version_id'] for item in manifest['inputs'])
    argv = manifest['plan']['steps'][0]['argv']
    assert argv[:3] == ['/usr/bin/python3', '-m', 'backend.vep_tools']
    assert '--cache-archive' in argv and '--gff' not in argv and '--plugin' not in argv
    assert request['tags']['CostReservation'] == config()['reservation_id']
    assert controller.sha256(CONTROL) == controller.CONTROL_SHA256 and len(CONTROL) == 384


@pytest.mark.parametrize('change', [
    lambda c: c['archive'].update(bytes=563872), lambda c: c['reference'].update(sha256='0' * 64),
    lambda c: c['index'].update(version_id='null'), lambda c: c.update(archive_dataset_id='fabricated'),
    lambda c: c['reference'].update(uri='s3://private/operations/checkpoints/reference.fa'),
    lambda c: c.update(job_definition=c['job_definition'].rsplit(':', 1)[0]),
])
def test_preparation_rejects_partial_cache_unknown_identity_and_unadmitted_paths(change):
    value = config();change(value)
    with pytest.raises(ValueError):
        controller.prepare(value, release())


def synthetic_reference(count):
    fasta = bytearray();index = []
    for number in range(count):
        name = 'contig_' + str(number)
        fasta.extend(('>' + name + '\n').encode());offset = len(fasta)
        fasta.extend(b'ACGT\nAC\n');index.append(f'{name}\t6\t{offset}\t4\t5\n')
    raw = bytes(fasta)
    return raw, ''.join(index).encode(), base64.b64encode(bytes.fromhex(controller.sha256(raw))).decode()


def test_real_streaming_3366_contigs_requires_explicit_bounds_and_validates_every_fai_field():
    raw, fai, checksum = synthetic_reference(3366)
    chunks = lambda: (raw[i:i+97] for i in range(0, len(raw), 97))
    with pytest.raises(ValueError, match='Invalid exact FASTA index'):
        derive_dictionary(chunks(), fai, len(raw), checksum)
    result = derive_dictionary(chunks(), fai, len(raw), checksum,
                               max_contigs=10000, dictionary_max_bytes=2 * 1024**2)
    assert sum(row.startswith(b'@SQ\t') for row in result.splitlines()) == 3366
    assert b'SN:contig_3365\tLN:6\tM5:' in result
    bad = fai.decode().splitlines();fields = bad[2050].split('\t');fields[2] = str(int(fields[2]) + 1)
    bad[2050] = '\t'.join(fields)
    with pytest.raises(ValueError, match='lengths/order/offsets/line widths'):
        derive_dictionary(chunks(), ('\n'.join(bad) + '\n').encode(), len(raw), checksum,
                          max_contigs=10000, dictionary_max_bytes=2 * 1024**2)
    with pytest.raises(ValueError, match='retained-byte bound'):
        derive_dictionary(chunks(), fai, len(raw), checksum, max_contigs=10000, dictionary_max_bytes=100)
    with pytest.raises(ValueError, match='whole-object SHA-256'):
        derive_dictionary(chunks(), fai, len(raw), base64.b64encode(b'0' * 32).decode(),
                          max_contigs=10000, dictionary_max_bytes=2 * 1024**2)


class Body(io.BytesIO):
    def iter_chunks(self, size):
        assert size == CHUNK_BYTES
        while raw := self.read(size):
            yield raw


class S3:
    def __init__(self):
        self.objects = {};self.gets = []

    def add(self, uri, raw, version='immutable-output', modified=None):
        bucket, key = uri[5:].split('/', 1)
        self.objects[bucket, key] = (raw, version, modified or datetime(2026, 1, 1, tzinfo=timezone.utc))

    def head_object(self, Bucket, Key, VersionId=None):
        raw, version, modified = self.objects[Bucket, Key]
        assert VersionId in [None, version]
        return {'ContentLength': len(raw), 'VersionId': version, 'LastModified': modified}

    def get_object(self, Bucket, Key, VersionId):
        raw, version, _ = self.objects[Bucket, Key]
        assert VersionId == version
        self.gets.append((Bucket, Key, VersionId))
        return {'Body': Body(raw), 'ContentLength': len(raw), 'VersionId': version}


def test_reference_verification_streams_complete_bytes_and_pins_both_versions():
    raw, fai, _ = synthetic_reference(3366);s3 = S3()
    reference = obj('tiny-synthetic.fa', controller.sha256(raw), len(raw))
    index = obj('tiny-synthetic.fa.fai', controller.sha256(fai), len(fai))
    for item, body in [(reference, raw), (index, fai)]:
        s3.add(item['uri'], body, item['version_id'])
    result = controller.verify_reference(s3, reference, index, {'instance_id': 'synthetic-test-only', 'region': 'us-east-1', 'account': ACCOUNT})
    assert result['contigs'] == 3366 and result['all_fai_fields_validated'] is True
    assert result['reference'] == reference and result['index'] == index
    assert len(s3.gets) == 2
    s3.add(reference['uri'], raw.replace(b'ACGT', b'TCGT', 1), reference['version_id'])
    with pytest.raises(ValueError, match='whole-object SHA-256'):
        controller.verify_reference(s3, reference, index, {})


def execution_fixture(tmp_path, monkeypatch):
    """Synthetic annotations exercise verification, not Ensembl inference."""
    monkeypatch.setattr(runtime, 'published', release)
    plan = prepared();cfg = plan['config'];s3 = S3()
    proof = {'reference': cfg['reference'], 'index': cfg['index'],
        'fasta_sha256': controller.REFERENCE_SHA256, 'fai_sha256': controller.INDEX_SHA256,
        'all_fai_fields_validated': True, 'contigs': 3366, 'dictionary_sha256': 'a' * 64,
        'algorithm': 'scripts.acquire_references.derive_dictionary', 'chunk_bytes': CHUNK_BYTES,
        'max_contigs': 10000, 'dictionary_max_bytes': 2 * 1024**2,
        'runtime': {'instance_id': 'synthetic-test-only', 'account': ACCOUNT, 'region': 'us-east-1'}}
    definition = {'jobDefinitionArn': cfg['job_definition'], 'status': 'ACTIVE', 'platformCapabilities': ['FARGATE'],
        'tags': {'TenantId': OWNER, 'NGSRuntimeFamily': runtime.FAMILY, 'NGSWorkerContract': '1'},
        'containerProperties': {'image': plan['image'], 'command': controller.COMMAND,
            'jobRoleArn': cfg['job_role_arn'], 'readonlyRootFilesystem': True, 'user': '1000:1000',
            'ephemeralStorage': {'sizeInGiB': 200},
            'mountPoints': [{'containerPath': '/' + name, 'readOnly': False} for name in ['work', 'inputs', 'tmp']]}}
    expected = plan['submit_request']
    job = {'jobId': 'synthetic-validator-fixture-only', 'status': 'SUCCEEDED',
        **{key: expected[key] for key in ['jobName', 'jobDefinition', 'jobQueue', 'tags', 'timeout', 'retryStrategy']},
        'startedAt': 1792480000000, 'stoppedAt': 1792480100000,
        'container': {**expected['containerOverrides'], 'image': plan['image'], 'jobRoleArn': cfg['job_role_arn'], 'exitCode': 0}}
    source = tmp_path / 'annotated.vcf'
    header = ('##fileformat=VCFv4.2\n##contig=<ID=21,length=46709983>\n'
        '##INFO=<ID=CSQ,Number=.,Type=String,Description="Format: Allele|Consequence|IMPACT|SYMBOL">\n'
        '#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n')
    rows = []
    for number, row in enumerate(CONTROL.decode().splitlines()[2:]):
        fields = row.split('\t');fields[7] = 'CSQ=' + fields[4] + '|' + ('synonymous_variant' if number == 0 else 'intron_variant') + '|LOW|MRPL39'
        rows.append('\t'.join(fields) + '\n')
    source.write_text(header + ''.join(rows));compressed = Path(str(source) + '.gz')
    pysam.tabix_compress(str(source), str(compressed), force=True);pysam.tabix_index(str(compressed), preset='vcf', force=True)
    report = {'version': runtime.VERSION, 'cache_version': runtime.CACHE_VERSION, 'assembly': runtime.ASSEMBLY,
        'offline': True, 'reference_sha256': controller.REFERENCE_SHA256, 'annotated_records': 10,
        'cache': {'members': runtime.CACHE_MEMBER_COUNT, 'bytes': runtime.CACHE_EXTRACTED_BYTES,
                  'archive_sha256': runtime.CACHE_ARCHIVE_SHA256, 'member_manifest_sha256': runtime.CACHE_MANIFEST_SHA256}}
    base = 's3://' + cfg['bucket'] + '/' + plan['manifest']['prefix']
    locations = [base + 'outputs/test/annotated.vcf.gz', base + 'outputs/test/annotated.vcf.gz.tbi',
                 base + 'outputs/test/annotated.vcf.gz.runtime.json']
    bodies = [compressed.read_bytes(), Path(str(compressed) + '.tbi').read_bytes(), controller.encoded(report)]
    for uri, raw in zip(locations, bodies):
        s3.add(uri, raw)
    result = {'variants': [{'path': locations[0], 'bytes': len(bodies[0]), 'digest': 'sha256:' + controller.sha256(bodies[0]),
        'metadata': {'index': locations[1], 'index_sha256': controller.sha256(bodies[1])}}],
        'report': [{'path': locations[2], 'bytes': len(bodies[2]), 'digest': 'sha256:' + controller.sha256(bodies[2])}]}
    s3.add(base + 'manifest.json', controller.encoded(plan['manifest']))
    s3.add(base + 'result.json', controller.encoded(result));s3.add(cfg['variants']['uri'], CONTROL, cfg['variants']['version_id'])
    return plan, proof, job, definition, s3, report, result, base, locations


def test_finalization_verifies_actual_bgzf_and_tabix_and_returns_only_a_local_receipt(tmp_path, monkeypatch):
    plan, proof, job, definition, s3, *_ = execution_fixture(tmp_path, monkeypatch)
    receipt = controller.finalize(plan, proof, job, definition, s3)
    assert receipt['execution']['indexed_records'] == 10
    assert receipt['complete_cache']['members'] == 13922
    assert receipt['reference_verification'] == proof
    assert all(obj['version_id'] for obj in receipt['execution']['outputs'])
    assert not hasattr(controller, 'publish') and not hasattr(controller, 'submit')


@pytest.mark.parametrize('kind', ['failed', 'wrong-image', 'wrong-role', 'small-scratch', 'partial-reference', 'tampered-plan'])
def test_failed_or_mismatched_execution_never_produces_a_ready_receipt(tmp_path, monkeypatch, kind):
    plan, proof, job, definition, s3, *_ = execution_fixture(tmp_path, monkeypatch)
    if kind == 'failed':job['status'] = 'FAILED'
    if kind == 'wrong-image':job['container']['image'] = 'wrong-image'
    if kind == 'wrong-role':job['container']['jobRoleArn'] = 'wrong-role'
    if kind == 'small-scratch':definition['containerProperties']['ephemeralStorage']['sizeInGiB'] = 20
    if kind == 'partial-reference':proof['contigs'] = 1024
    if kind == 'tampered-plan':plan['manifest']['plan']['steps'][0]['argv'] = ['python', '-c', 'print("fake")']
    with pytest.raises(ValueError):
        controller.finalize(plan, proof, job, definition, s3)


def test_full_archive_report_and_output_digests_are_required_even_after_batch_success(tmp_path, monkeypatch):
    plan, proof, job, definition, s3, report, result, base, locations = execution_fixture(tmp_path, monkeypatch)
    report['cache']['members'] = 1;body = controller.encoded(report)
    s3.add(locations[2], body);result['report'][0].update(bytes=len(body), digest='sha256:' + controller.sha256(body))
    s3.add(base + 'result.json', controller.encoded(result))
    with pytest.raises(ValueError, match='every complete original cache member'):
        controller.finalize(plan, proof, job, definition, s3)


@pytest.mark.parametrize('kind', ['corrupt-index', 'changed-output-bytes', 'changed-manifest', 'unversioned-output'])
def test_real_output_or_manifest_corruption_is_not_hidden_by_success_status(tmp_path, monkeypatch, kind):
    plan, proof, job, definition, s3, report, result, base, locations = execution_fixture(tmp_path, monkeypatch)
    if kind == 'corrupt-index':
        raw = b'not a tabix index'
        s3.add(locations[1], raw);result['variants'][0]['metadata']['index_sha256'] = controller.sha256(raw)
        s3.add(base + 'result.json', controller.encoded(result))
    if kind == 'changed-output-bytes':s3.add(locations[0], b'corrupted VCF bytes')
    if kind == 'changed-manifest':s3.add(base + 'manifest.json', b'{"changed":true}')
    if kind == 'unversioned-output':s3.add(locations[0], b'corrupted VCF bytes', 'null')
    with pytest.raises((ValueError, OSError)):
        controller.finalize(plan, proof, job, definition, s3)


def test_evidence_is_never_overwritten(tmp_path):
    path = tmp_path / 'proof.json';controller.save(path, {'failed': True})
    controller.save(path, {'failed': True})
    with pytest.raises(ValueError, match='preserve it'):
        controller.save(path, {'failed': False})
