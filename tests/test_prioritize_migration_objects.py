import base64
import hashlib
import io
import json

import pytest
from botocore.exceptions import ClientError
from scripts import prioritize_migration_objects as priority


def error(code, operation):
    return ClientError({'Error': {'Code': code}}, operation)


class S3:
    def __init__(self):
        self.objects = {}
        self.bodies = {}
        self.puts = []
        self.race = False
        self.uploads = {}
        self.part_calls = []
        self.create_calls = 0
        self.complete_calls = 0
        self.fail_part = None
        self.ambiguous_create = False
        self.ambiguous_complete = False

    def head_object(self, **args):
        if args['Key'] not in self.objects:
            raise error('404', 'HeadObject')
        return dict(self.objects[args['Key']])

    def put_object(self, **args):
        self.puts.append(args)
        assert args['IfNoneMatch'] == '*'
        assert args['ServerSideEncryption'] == 'aws:kms'
        assert args['BucketKeyEnabled'] is True
        if args['Key'] in self.objects:
            raise error('PreconditionFailed', 'PutObject')
        raw = args['Body'].read() if hasattr(args['Body'], 'read') else args['Body']
        checksum = base64.b64encode(hashlib.sha256(raw).digest()).decode()
        if 'ChecksumSHA256' in args:
            assert checksum == args['ChecksumSHA256']
        version = 'immutable-' + str(len(self.objects) + 1)
        self.objects[args['Key']] = {'ContentLength': len(raw), 'VersionId': version,
                                    'ChecksumSHA256': checksum, 'ChecksumType': 'FULL_OBJECT',
                                    'Metadata': args['Metadata']}
        self.bodies[args['Key']] = raw
        if self.race and 'ChecksumSHA256' in args:
            self.race = False
            raise error('PreconditionFailed', 'PutObject')
        return {'VersionId': version}

    def get_object(self, **args):
        head = self.head_object(**args)
        return {**head, 'Body': io.BytesIO(self.bodies[args['Key']])}

    def create_multipart_upload(self, **args):
        self.create_calls += 1
        upload = 'private-upload-' + str(self.create_calls)
        self.uploads[upload] = {'args': args, 'parts': {}}
        if self.ambiguous_create:
            raise ConnectionError('lost creation response')
        return {'UploadId': upload}

    def list_parts(self, **args):
        parts = self.uploads[args['UploadId']]['parts']
        return {'IsTruncated': False, 'Parts': [
            {'PartNumber': n, 'Size': len(v['raw']), 'ETag': v['etag'], 'ChecksumSHA256': v['checksum']}
            for n, v in sorted(parts.items())]}

    def upload_part(self, **args):
        self.part_calls.append(args['PartNumber'])
        if self.fail_part == args['PartNumber']:
            self.fail_part = None
            raise ConnectionError('interrupted part')
        raw = args['Body'];checksum = base64.b64encode(hashlib.sha256(raw).digest()).decode()
        assert checksum == args['ChecksumSHA256']
        etag = 'part-' + str(args['PartNumber'])
        self.uploads[args['UploadId']]['parts'][args['PartNumber']] = {'raw': raw, 'etag': etag, 'checksum': checksum}
        return {'ETag': etag, 'ChecksumSHA256': checksum}

    def complete_multipart_upload(self, **args):
        self.complete_calls += 1
        assert args['IfNoneMatch'] == '*'
        if args['Key'] in self.objects:
            raise error('PreconditionFailed', 'CompleteMultipartUpload')
        upload = self.uploads[args['UploadId']];parts = [upload['parts'][p['PartNumber']] for p in args['MultipartUpload']['Parts']]
        raw = b''.join(p['raw'] for p in parts)
        checksum = base64.b64encode(hashlib.sha256(b''.join(base64.b64decode(p['checksum']) for p in parts)).digest()).decode() + '-' + str(len(parts))
        version = 'immutable-' + str(len(self.objects) + 1)
        self.objects[args['Key']] = {'ContentLength': len(raw), 'VersionId': version,
                                    'ChecksumSHA256': checksum, 'ChecksumType': 'COMPOSITE',
                                    'Metadata': upload['args']['Metadata']}
        self.bodies[args['Key']] = raw
        del self.uploads[args['UploadId']]
        if self.ambiguous_complete:
            raise ConnectionError('lost completion response')
        return {'VersionId': version}


def fixture(tmp_path):
    raw = b'original reference data\n'
    source = tmp_path / 'original-source.bin'
    source.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    plan = {'plan_id': priority.APPROVED_PLAN, 'owner_id': priority.APPROVED_OWNER,
            'bucket': priority.APPROVED_BUCKET, 'kms': 'arn:test:key'}
    obj = {'name': 'hp.obo', 'sha256': digest, 'bytes': len(raw), 'dataset_id': 'ds_original',
           'key': 'tenants/owner/datasets/ds_original/hp.obo',
           'aliases': [{'path': str(source), 'mtime_ns': source.stat().st_mtime_ns}]}
    return plan, obj, source


def test_dry_run_checks_source_without_cloud_or_ledger_writes(tmp_path):
    plan, obj, source = fixture(tmp_path)
    client = S3()
    result = priority.prioritize(client, plan, [obj])
    assert result['aws_mutations'] is False and result['shared_ledger_modified'] is False
    assert result['leases_reset'] is False and client.puts == []
    assert list(tmp_path.iterdir()) == [source]
    assert str(source) not in json.dumps(result)


def test_source_checksum_mismatch_stops_before_upload(tmp_path):
    plan, obj, source = fixture(tmp_path)
    obj['sha256'] = '0' * 64
    client = S3()
    with pytest.raises(ValueError, match='inventoried SHA-256'):
        priority.prioritize(client, plan, [obj], execute=True)
    assert not client.puts


def test_conditional_original_upload_and_normal_receipt_are_idempotent(tmp_path):
    plan, obj, source = fixture(tmp_path)
    before = source.stat()
    client = S3()
    first = priority.prioritize(client, plan, [obj], execute=True)
    assert len(client.puts) == 2
    primary = client.puts[0]
    assert primary['ChecksumSHA256'] == priority.expected_checksum(obj)
    assert primary['Metadata'] == priority.metadata(plan, obj)
    receipt_key = client.puts[1]['Key']
    receipt = json.loads(client.bodies[receipt_key])
    assert receipt['schema'] == 1 and receipt['version_id'] == first['objects'][0]['version_id']
    assert receipt['sha256'] == obj['sha256'] and receipt['s3_checksum_type'] == 'FULL_OBJECT'
    second = priority.prioritize(client, plan, [obj], execute=True)
    assert second['objects'][0]['version_id'] == first['objects'][0]['version_id']
    assert len(client.objects) == 2
    assert source.stat().st_mtime_ns == before.st_mtime_ns
    assert source.read_bytes() == b'original reference data\n'


def test_conditional_race_verifies_winning_original_version(tmp_path):
    plan, obj, _ = fixture(tmp_path)
    client = S3()
    client.race = True
    result = priority.prioritize(client, plan, [obj], execute=True)
    assert result['objects'][0]['version_id'] == 'immutable-1'
    assert len(client.objects) == 2


@pytest.mark.parametrize('alteration', [
    {'ChecksumSHA256': 'wrong'}, {'ChecksumType': 'COMPOSITE'}, {'VersionId': 'null'},
    {'Metadata': {'sha256': 'wrong'}}, {'ContentLength': 999},
])
def test_conflicting_existing_object_is_preserved(tmp_path, alteration):
    plan, obj, _ = fixture(tmp_path)
    client = S3()
    head = {'ContentLength': obj['bytes'], 'VersionId': 'existing',
            'ChecksumSHA256': priority.expected_checksum(obj), 'Metadata': priority.metadata(plan, obj)}
    client.objects[obj['key']] = {**head, **alteration}
    client.bodies[obj['key']] = b'corrupt data'
    with pytest.raises(ValueError, match='Existing cloud'):
        priority.prioritize(client, plan, [obj], execute=True)
    assert client.puts == []
    assert client.objects[obj['key']] == {**head, **alteration}


def test_bounded_multipart_uses_confirmed_part_checksums_and_private_checkpoint(tmp_path, monkeypatch):
    plan, obj, _ = fixture(tmp_path)
    monkeypatch.setattr(priority, 'PART_BYTES', 8)
    monkeypatch.setattr(priority, 'MULTIPART_THRESHOLD', 16)
    client = S3();folder = tmp_path / 'private-priority'
    first = priority.prioritize(client, plan, [obj], execute=True, checkpoint_directory=folder)
    checkpoint = json.loads((folder / (obj['sha256'] + '.json')).read_bytes())
    assert client.create_calls == 1 and client.complete_calls == 1
    assert checkpoint['state'] == 'completed' and len(checkpoint['parts']) == 3
    assert client.objects[obj['key']]['ChecksumType'] == 'COMPOSITE'
    assert client.objects[obj['key']]['ChecksumSHA256'] == checkpoint['composite_sha256']
    second = priority.prioritize(client, plan, [obj], execute=True, checkpoint_directory=folder)
    assert second['objects'][0]['version_id'] == first['objects'][0]['version_id']
    assert client.create_calls == 1 and client.complete_calls == 1


def test_interrupted_part_resumes_same_owned_upload_without_reuploading_verified_parts(tmp_path, monkeypatch):
    plan, obj, _ = fixture(tmp_path)
    monkeypatch.setattr(priority, 'PART_BYTES', 8)
    monkeypatch.setattr(priority, 'MULTIPART_THRESHOLD', 16)
    client = S3();client.fail_part = 2;folder = tmp_path / 'private-priority'
    with pytest.raises(ConnectionError):
        priority.prioritize(client, plan, [obj], execute=True, checkpoint_directory=folder)
    checkpoint = json.loads((folder / (obj['sha256'] + '.json')).read_bytes())
    assert checkpoint['state'] == 'uploading' and checkpoint['upload_id'] == 'private-upload-1'
    priority.prioritize(client, plan, [obj], execute=True, checkpoint_directory=folder)
    assert client.create_calls == 1 and client.complete_calls == 1
    assert client.part_calls.count(1) == 1 and client.part_calls.count(3) == 1
    assert client.part_calls.count(2) == 2


def test_ambiguous_creation_is_not_blindly_retried(tmp_path, monkeypatch):
    plan, obj, _ = fixture(tmp_path)
    monkeypatch.setattr(priority, 'PART_BYTES', 8)
    monkeypatch.setattr(priority, 'MULTIPART_THRESHOLD', 16)
    client = S3();client.ambiguous_create = True;folder = tmp_path / 'private-priority'
    with pytest.raises(ConnectionError):
        priority.prioritize(client, plan, [obj], execute=True, checkpoint_directory=folder)
    with pytest.raises(ValueError, match='Ambiguous priority multipart'):
        priority.prioritize(client, plan, [obj], execute=True, checkpoint_directory=folder)
    assert client.create_calls == 1 and client.complete_calls == 0


def test_lost_part_response_retries_identical_bytes_in_same_upload(tmp_path, monkeypatch):
    plan, obj, _ = fixture(tmp_path)
    monkeypatch.setattr(priority, 'PART_BYTES', 8)
    monkeypatch.setattr(priority, 'MULTIPART_THRESHOLD', 16)
    monkeypatch.setattr(priority.time, 'sleep', lambda seconds: None)
    class LostResponse(S3):
        lost = False
        def upload_part(self, **request):
            result = super().upload_part(**request)
            if request['PartNumber'] == 2 and not self.lost:
                self.lost = True
                raise priority.ConnectionClosedError(endpoint_url='https://test.invalid')
            return result
    client = LostResponse()
    priority.prioritize(client, plan, [obj], execute=True, checkpoint_directory=tmp_path / 'priority')
    assert client.part_calls.count(2) == 2
    assert client.create_calls == 1 and client.complete_calls == 1
    assert client.bodies[obj['key']] == b'original reference data\n'


def test_transient_part_retries_are_bounded_without_completing_upload(tmp_path, monkeypatch):
    plan, obj, _ = fixture(tmp_path)
    monkeypatch.setattr(priority, 'PART_BYTES', 8)
    monkeypatch.setattr(priority, 'MULTIPART_THRESHOLD', 16)
    monkeypatch.setattr(priority.time, 'sleep', lambda seconds: None)
    class Offline(S3):
        def upload_part(self, **request):
            self.part_calls.append(request['PartNumber'])
            raise priority.ConnectionClosedError(endpoint_url='https://test.invalid')
    client = Offline();folder = tmp_path / 'priority'
    with pytest.raises(priority.ConnectionClosedError):
        priority.prioritize(client, plan, [obj], execute=True, checkpoint_directory=folder)
    assert client.create_calls == 1 and client.complete_calls == 0
    assert all(client.part_calls.count(number) <= 3 for number in set(client.part_calls))
    assert json.loads((folder / (obj['sha256'] + '.json')).read_text())['state'] == 'uploading'


def test_ambiguous_completion_recovers_existing_version_before_any_new_mutation(tmp_path, monkeypatch):
    plan, obj, _ = fixture(tmp_path)
    monkeypatch.setattr(priority, 'PART_BYTES', 8)
    monkeypatch.setattr(priority, 'MULTIPART_THRESHOLD', 16)
    client = S3();client.ambiguous_complete = True;folder = tmp_path / 'private-priority'
    with pytest.raises(ConnectionError):
        priority.prioritize(client, plan, [obj], execute=True, checkpoint_directory=folder)
    priority.prioritize(client, plan, [obj], execute=True, checkpoint_directory=folder)
    assert client.create_calls == 1 and client.complete_calls == 1
    assert len(client.objects) == 2


def test_conflicting_normal_receipt_cannot_be_replaced(tmp_path):
    plan, obj, _ = fixture(tmp_path)
    client = S3()
    priority.prioritize(client, plan, [obj], execute=True)
    receipt_key = client.puts[-1]['Key']
    previous = json.loads(client.bodies[receipt_key])
    previous['version_id'] = 'different-version'
    client.bodies[receipt_key] = json.dumps(previous).encode()
    with pytest.raises(ValueError, match='another immutable version'):
        priority.prioritize(client, plan, [obj], execute=True)
    assert json.loads(client.bodies[receipt_key])['version_id'] == 'different-version'


def reviewed_plan():
    plan = {'plan_id': priority.APPROVED_PLAN, 'owner_id': priority.APPROVED_OWNER,
            'bucket': priority.APPROVED_BUCKET, 'objects': []}
    for digest, (name, family, size) in priority.REVIEWED.items():
        dataset = 'ds_migration_' + hashlib.sha256((priority.APPROVED_OWNER + ':' + digest).encode()).hexdigest()[:40]
        plan['objects'].append({'name': name, 'sha256': digest, 'bytes': size, 'dataset_id': dataset,
                               'key': f'tenants/{priority.APPROVED_OWNER}/datasets/{dataset}/{name}',
                               'aliases': [{'name': name, 'family': family, 'bytes': size, 'user_id': priority.APPROVED_OWNER}]})
    return plan


def test_allowlist_selects_exact_hpo_release_and_rejects_family_drift():
    plan = reviewed_plan()
    plan['objects'].append({'sha256': 'f' * 64, 'name': 'hp.obo', 'bytes': 1})
    assert {o['sha256'] for o in priority.selected_objects(plan)} == set(priority.REVIEWED)
    plan['objects'][0]['aliases'][0]['family'] = 'unreviewed'
    with pytest.raises(ValueError, match='family and owner'):
        priority.selected_objects(plan)


def test_cloud_manifest_version_must_match_registered_immutable_plan(tmp_path, monkeypatch):
    raw = priority.encode(reviewed_plan())
    digest = hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(priority, 'APPROVED_PLAN_SHA256', digest)
    monkeypatch.setattr(priority, 'APPROVED_PLAN_VERSION', 'expected-version')
    registration = {'plan_id': priority.APPROVED_PLAN, 'owner_id': priority.APPROVED_OWNER,
                    'bucket': priority.APPROVED_BUCKET, 'sha256': digest, 'version_id': 'expected-version',
                    'key': f'operations/checkpoints/migration/{priority.APPROVED_PLAN}/plan.json'}
    (tmp_path / 'registration.json').write_bytes(priority.encode(registration))
    (tmp_path / 'plan.json').write_bytes(raw)
    client = S3()
    client.objects[registration['key']] = {'ContentLength': len(raw), 'VersionId': 'other-version'}
    client.bodies[registration['key']] = raw
    with pytest.raises(ValueError, match='Cloud manifest version'):
        priority.load_reviewed_plan(tmp_path, client)
    assert not client.puts
