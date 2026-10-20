"""Synthetic-only acquisition tests; no provider, AWS or genomic file access."""
import base64
import copy
import hashlib
import io
import json
import stat
import sys

import pytest

from scripts import acquire_exact_vep_archive as acquisition
from test_prioritize_migration_objects import S3, error


RESERVATION = 'cost_' + 'a' * 32


class MemoryProvider:
    def __init__(self, raw):
        self.raw = raw
        self.head_calls = 0
        self.ranges = []

    def head(self):
        self.head_calls += 1
        return {'bytes': len(self.raw)}

    def read_range(self, offset, length):
        self.ranges.append((offset, length))
        return self.raw[offset:offset + length]


class MemoryS3(S3):
    def __init__(self):
        super().__init__()
        self.aborts = []
        self.completions = []
        self.complete_hook = None
        self.gets = []

    def get_object(self, **request):
        self.gets.append(request)
        return super().get_object(**request)

    def complete_multipart_upload(self, **request):
        self.completions.append(request)
        if self.complete_hook:
            self.complete_hook(request)
        return super().complete_multipart_upload(**request)

    def abort_multipart_upload(self, **request):
        self.aborts.append(request)
        del self.uploads[request['UploadId']]
        return {}


@pytest.fixture
def original(monkeypatch):
    raw = b'synthetic original archive bytes\n'
    digest = hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(acquisition, 'PART_BYTES', 8)
    monkeypatch.setattr(acquisition, 'ARCHIVE_BYTES', len(raw))
    monkeypatch.setattr(acquisition, 'ARCHIVE_SHA256', digest)
    owner = acquisition.migration.APPROVED_OWNER
    dataset = 'ds_migration_' + hashlib.sha256((owner + ':' + digest).encode()).hexdigest()[:40]
    obj = {'name': acquisition.ARCHIVE_NAME, 'sha256': digest, 'bytes': len(raw),
           'dataset_id': dataset,
           'key': f'tenants/{owner}/datasets/{dataset}/{acquisition.ARCHIVE_NAME}',
           'aliases': [{'name': acquisition.ARCHIVE_NAME, 'user_id': owner, 'bytes': len(raw)}]}
    plan = {'plan_id': acquisition.migration.APPROVED_PLAN, 'owner_id': owner,
            'bucket': acquisition.migration.APPROVED_BUCKET, 'kms': 'arn:test:synthetic-key',
            'objects': [obj], 'total_bytes': len(raw)}
    return plan, obj, raw


def saved(folder, obj):
    return json.loads((folder / ('exact-vep-' + obj['sha256'] + '.json')).read_bytes())


def receipt_key(plan, obj):
    return f"operations/checkpoints/migration/{plan['plan_id']}/uploaded/{obj['sha256']}.json"


def existing(client, plan, obj, raw, *, composite=False):
    checksum = base64.b64encode(hashlib.sha256(raw).digest()).decode()
    client.objects[obj['key']] = {'ContentLength': len(raw), 'VersionId': 'original-version',
        'ChecksumSHA256': checksum + '-1' if composite else checksum,
        'ChecksumType': 'COMPOSITE' if composite else 'FULL_OBJECT',
        'Metadata': acquisition.migration.metadata(plan, obj)}
    client.bodies[obj['key']] = raw


def test_full_original_sha256_is_saved_before_conditional_commit(original, tmp_path):
    plan, obj, raw = original
    client = MemoryS3();provider = MemoryProvider(raw);folder = tmp_path / 'private'

    def before_commit(request):
        state = saved(folder, obj)
        assert state['state'] == 'completing'
        assert state['verified_original_sha256'] == hashlib.sha256(raw).hexdigest()
        assert state['provider_bytes'] == len(raw)
        assert request['IfNoneMatch'] == '*'
        assert sorted(provider.ranges) == [(offset, min(8, len(raw) - offset)) for offset in range(0, len(raw), 8)]
        assert not client.puts

    client.complete_hook = before_commit
    result = acquisition.acquire_archive(client, plan, obj, provider, folder, RESERVATION)
    checkpoint = saved(folder, obj)
    assert result['sha256'] == obj['sha256'] and result['provider_bytes'] == len(raw)
    assert client.bodies[obj['key']] == raw
    assert checkpoint['state'] == 'completed'
    assert client.objects[obj['key']]['ChecksumSHA256'] == checkpoint['composite_sha256']
    receipt = json.loads(client.bodies[receipt_key(plan, obj)])
    assert receipt['version_id'] == result['version_id'] and receipt['sha256'] == obj['sha256']
    assert receipt['s3_checksum_type'] == 'COMPOSITE'
    assert client.create_calls == client.complete_calls == 1
    assert {path.suffix for path in folder.iterdir()} == {'.json', '.lock'}
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in folder.iterdir())


def test_corrupt_provider_never_commits_or_publishes_normal_receipt(original, tmp_path):
    plan, obj, raw = original
    client = MemoryS3();folder = tmp_path / 'private'
    provider = MemoryProvider(b'X' + raw[1:])
    with pytest.raises(ValueError, match='inventoried SHA-256'):
        acquisition.acquire_archive(client, plan, obj, provider, folder, RESERVATION)
    state = saved(folder, obj)
    assert state['state'] == 'failed-sha256' and state['observed_bytes'] == len(raw)
    assert state['observed_sha256'] != obj['sha256']
    assert client.complete_calls == 0 and client.puts == [] and client.objects == {}
    with pytest.raises(ValueError, match='operator reconciliation'):
        acquisition.acquire_archive(client, plan, obj, MemoryProvider(raw), folder, RESERVATION)
    assert client.create_calls == 1 and client.complete_calls == 0


def test_interrupted_upload_rehashes_whole_prefix_and_reuses_confirmed_parts(original, tmp_path):
    plan, obj, raw = original
    client = MemoryS3();client.fail_part = 2;folder = tmp_path / 'private'
    first = MemoryProvider(raw)
    with pytest.raises(ConnectionError, match='interrupted part'):
        acquisition.acquire_archive(client, plan, obj, first, folder, RESERVATION)
    assert {(0,8),(8,8)}<=set(first.ranges) and len(first.ranges)<=8
    assert saved(folder, obj)['upload_id'] == 'private-upload-1'
    assert not client.puts and client.complete_calls == 0
    resumed = MemoryProvider(raw)
    acquisition.acquire_archive(client, plan, obj, resumed, folder, RESERVATION)
    assert sorted(resumed.ranges) == [(offset, min(8, len(raw) - offset)) for offset in range(0, len(raw), 8)]
    assert client.create_calls == client.complete_calls == 1
    assert client.part_calls.count(1) == 1 and client.part_calls.count(2) == 2
    assert client.completions[0]['UploadId'] == 'private-upload-1'
    assert client.bodies[obj['key']] == raw


def test_lost_part_response_retries_only_identical_part_in_same_upload(original, tmp_path):
    plan, obj, raw = original

    class LostPart(MemoryS3):
        lost = False

        def upload_part(self, **request):
            response = super().upload_part(**request)
            if request['PartNumber'] == 2 and not self.lost:
                self.lost = True
                raise acquisition.ConnectionClosedError(endpoint_url='https://synthetic.invalid')
            return response

    client = LostPart()
    acquisition.acquire_archive(client, plan, obj, MemoryProvider(raw), tmp_path / 'private', RESERVATION)
    assert client.part_calls.count(2) == 2
    assert client.create_calls == client.complete_calls == 1
    assert client.bodies[obj['key']] == raw


def test_ambiguous_create_is_never_repeated(original, tmp_path):
    plan, obj, raw = original
    client = MemoryS3();client.ambiguous_create = True;folder = tmp_path / 'private'
    with pytest.raises(ConnectionError, match='creation response'):
        acquisition.acquire_archive(client, plan, obj, MemoryProvider(raw), folder, RESERVATION)
    assert saved(folder, obj)['state'] == 'creating'
    with pytest.raises(ValueError, match='operator reconciliation'):
        acquisition.acquire_archive(client, plan, obj, MemoryProvider(raw), folder, RESERVATION)
    assert client.create_calls == 1 and client.complete_calls == 0 and not client.puts


def test_lost_completed_response_recovers_exact_version_without_provider_retry(original, tmp_path):
    plan, obj, raw = original
    client = MemoryS3();client.ambiguous_complete = True;folder = tmp_path / 'private'
    with pytest.raises(ConnectionError, match='completion response'):
        acquisition.acquire_archive(client, plan, obj, MemoryProvider(raw), folder, RESERVATION)
    assert saved(folder, obj)['state'] == 'completing' and not client.puts
    retry = MemoryProvider(b'')
    result = acquisition.acquire_archive(client, plan, obj, retry, folder, RESERVATION)
    assert result['provider_bytes'] == 0 and retry.head_calls == 0 and retry.ranges == []
    assert client.create_calls == client.complete_calls == 1
    assert client.gets[0]['VersionId'] == result['version_id']
    assert receipt_key(plan, obj) in client.objects


def test_unresolved_ambiguous_completion_is_not_repeated(original, tmp_path):
    plan, obj, raw = original
    client = MemoryS3();folder = tmp_path / 'private'

    def lost_before_commit(request):
        raise ConnectionError('completion outcome unknown')

    client.complete_hook = lost_before_commit
    with pytest.raises(ConnectionError, match='outcome unknown'):
        acquisition.acquire_archive(client, plan, obj, MemoryProvider(raw), folder, RESERVATION)
    with pytest.raises(ValueError, match='operator reconciliation'):
        acquisition.acquire_archive(client, plan, obj, MemoryProvider(raw), folder, RESERVATION)
    assert len(client.completions) == 1 and client.create_calls == 1 and not client.puts


@pytest.mark.parametrize('composite', [False, True])
def test_existing_exact_object_recovers_without_source_reads(original, tmp_path, composite):
    plan, obj, raw = original
    client = MemoryS3();existing(client, plan, obj, raw, composite=composite)
    provider = MemoryProvider(b'')
    result = acquisition.acquire_archive(client, plan, obj, provider, tmp_path / 'private', RESERVATION)
    assert result['version_id'] == 'original-version' and result['provider_bytes'] == 0
    assert provider.head_calls == 0 and provider.ranges == []
    assert client.create_calls == client.complete_calls == 0
    assert len(client.gets) == int(composite)
    assert receipt_key(plan, obj) in client.objects


@pytest.mark.parametrize('alteration', [
    {'ContentLength': 1}, {'VersionId': 'null'}, {'ChecksumSHA256': 'wrong'},
    {'Metadata': {'sha256': 'wrong'}},
])
def test_conflicting_existing_object_is_preserved_without_upload(original, tmp_path, alteration):
    plan, obj, raw = original
    client = MemoryS3();existing(client, plan, obj, raw)
    client.objects[obj['key']].update(alteration)
    before = copy.deepcopy(client.objects)
    with pytest.raises(ValueError, match='Existing cloud'):
        acquisition.acquire_archive(client, plan, obj, MemoryProvider(raw), tmp_path / 'private', RESERVATION)
    assert client.objects == before and not client.puts
    assert client.create_calls == client.complete_calls == 0


def test_conditional_winner_is_verified_and_only_owned_upload_aborted(original, tmp_path):
    plan, obj, raw = original
    client = MemoryS3();client.uploads['ordinary-migration-upload'] = {'untouched': True}

    def winner(request):
        existing(client, plan, obj, raw)

    client.complete_hook = winner
    result = acquisition.acquire_archive(client, plan, obj, MemoryProvider(raw), tmp_path / 'private', RESERVATION)
    assert result['version_id'] == 'original-version' and client.bodies[obj['key']] == raw
    assert client.aborts == [{'Bucket': plan['bucket'], 'Key': obj['key'], 'UploadId': 'private-upload-1'}]
    assert client.uploads == {'ordinary-migration-upload': {'untouched': True}}
    assert receipt_key(plan, obj) in client.objects


def test_conflicting_conditional_winner_is_not_overwritten_or_receipted(original, tmp_path):
    plan, obj, raw = original
    client = MemoryS3()

    def winner(request):
        existing(client, plan, obj, b'X' + raw[1:])

    client.complete_hook = winner
    with pytest.raises(ValueError, match='exact full-object'):
        acquisition.acquire_archive(client, plan, obj, MemoryProvider(raw), tmp_path / 'private', RESERVATION)
    assert client.bodies[obj['key']] == b'X' + raw[1:]
    assert not client.puts and client.aborts == []


def test_409_completion_conflict_requires_reconciliation_without_retry(original, tmp_path):
    plan, obj, raw = original
    client = MemoryS3();folder = tmp_path / 'private'

    def conflict(request):
        raise error('ConditionalRequestConflict', 'CompleteMultipartUpload')

    client.complete_hook = conflict
    with pytest.raises(acquisition.ClientError):
        acquisition.acquire_archive(client, plan, obj, MemoryProvider(raw), folder, RESERVATION)
    with pytest.raises(ValueError, match='operator reconciliation'):
        acquisition.acquire_archive(client, plan, obj, MemoryProvider(raw), folder, RESERVATION)
    assert len(client.completions) == 1 and client.create_calls == 1
    assert not client.puts and not client.aborts


def test_target_is_exact_original_owner_archive_and_aliases(original):
    plan, obj, _ = original
    assert acquisition.target_object(plan) is obj
    for field, bad in [('name', 'different.tar.gz'), ('bytes', obj['bytes'] + 1),
                       ('dataset_id', 'wrong'), ('key', 'tenants/foreign/archive')]:
        changed = copy.deepcopy(plan);changed['objects'][0][field] = bad
        with pytest.raises(ValueError):
            acquisition.target_object(changed)
    changed = copy.deepcopy(plan);changed['objects'][0]['aliases'][0]['user_id'] = 'foreign'
    with pytest.raises(ValueError, match='alias'):
        acquisition.target_object(changed)


def test_admission_only_reads_existing_cost_hold_and_exact_storage(original, monkeypatch):
    from backend import db
    plan, _, _ = original
    queries = []
    cost = {'state': 'reserved', 'reserved_micro': 100000, 'account': 'shared-platform', 'role': 'admin', 'frozen': 0}
    storage = {'user_id': plan['owner_id'], 'plan_sha256': acquisition.migration.APPROVED_PLAN_SHA256, 'bytes': plan['total_bytes']}

    def query(sql, params, one=False):
        queries.append((sql, params))
        assert sql.startswith('SELECT ') and one is True
        return dict(storage if 'baseline_storage_allocations' in sql else cost)

    monkeypatch.setattr(db, 'query', query)
    monkeypatch.setattr(db, 'tx', lambda: pytest.fail('Acquisition must not create funding'))
    result = acquisition.verify_admission(RESERVATION, plan)
    assert result['existing_storage_envelope'] and result['maximum_reserved_pretax_usd'] == .1
    assert len(queries) == 2
    for field, value in [('state', 'settled'), ('reserved_micro', 99999), ('reserved_micro', 2000001),
                         ('account', 'tenant'), ('role', 'user'), ('frozen', 1)]:
        before = dict(cost);cost[field] = value
        with pytest.raises(ValueError, match='reservation'):
            acquisition.verify_admission(RESERVATION, plan)
        cost.clear();cost.update(before)
    storage['bytes'] += 1
    with pytest.raises(ValueError, match='storage envelope'):
        acquisition.verify_admission(RESERVATION, plan)
    with pytest.raises(ValueError, match='existing shared-platform'):
        acquisition.verify_admission('invented-reservation', plan)


def test_dry_run_has_no_host_network_funding_or_cloud_access(original, monkeypatch, capsys):
    plan, obj, _ = original
    monkeypatch.setattr(sys, 'argv', ['acquire-exact-vep'])
    monkeypatch.setattr(acquisition, 'load_local_plan', lambda directory: (plan, obj, {}, 10))
    for name in ['aws_session', 'Provider', 'verify_cloud_runtime', 'verify_admission']:
        monkeypatch.setattr(acquisition, name, lambda *args, **kwargs: pytest.fail('Dry-run must remain local metadata only'))
    acquisition.main()
    result = json.loads(capsys.readouterr().out)
    assert result['aws_mutations'] is False and result['shared_ledger_modified'] is False
    assert result['retained_bytes_already_in_original_storage_envelope'] is True


def test_execute_rejects_non_cloud_host_before_admission_or_storage(original, monkeypatch):
    plan, obj, _ = original
    monkeypatch.setattr(sys, 'argv', ['acquire-exact-vep', '--execute', '--reservation', RESERVATION])
    monkeypatch.setattr(acquisition, 'load_local_plan', lambda directory: (plan, obj, {}, 10))

    def no_host(release):
        raise ValueError('AWS EC2 host required')

    monkeypatch.setattr(acquisition, 'verify_cloud_runtime', no_host)
    for name in ['aws_session', 'Provider', 'verify_admission']:
        monkeypatch.setattr(acquisition, name, lambda *args, **kwargs: pytest.fail('Host rejection must precede funding or cloud access'))
    with pytest.raises(ValueError, match='EC2 host'):
        acquisition.main()


class HTTPResponse:
    def __init__(self, headers, body=b'', status=206):
        self.headers = headers;self.body = body;self.status = status;self.reads = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, length):
        self.reads.append(length)
        return self.body[:length]


def http_headers(**changes):
    return {'ETag': acquisition.SOURCE_ETAG, 'Last-Modified': acquisition.SOURCE_MODIFIED, **changes}


def test_provider_sends_exact_conditionals_and_bounded_range(original):
    _, _, raw = original
    response = HTTPResponse(http_headers(**{'Content-Length': '8', 'Content-Range': f'bytes 8-15/{len(raw)}'}), raw[8:16])
    requests = []

    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            assert timeout == 30
            return response

    provider = acquisition.Provider.__new__(acquisition.Provider);provider.opener = Opener()
    assert provider.read_range(8, 8) == raw[8:16]
    headers = {key.lower(): value for key, value in requests[0].header_items()}
    assert requests[0].full_url == acquisition.SOURCE and requests[0].method == 'GET'
    assert headers['range'] == 'bytes=8-15' and headers['accept-encoding'] == 'identity'
    assert headers['if-match'] == acquisition.SOURCE_ETAG and headers['if-unmodified-since'] == acquisition.SOURCE_MODIFIED
    assert response.reads == [9]


@pytest.mark.parametrize('changes,status,body', [
    ({'ETag': 'different'}, 206, b'abcdefgh'), ({'Last-Modified': 'changed'}, 206, b'abcdefgh'),
    ({'Content-Encoding': 'gzip'}, 206, b'abcdefgh'), ({}, 200, b'abcdefgh'),
    ({'Content-Length': '9'}, 206, b'abcdefgh'), ({'Content-Range': 'bytes 1-8/99'}, 206, b'abcdefgh'),
    ({}, 206, b'abcdefg'), ({}, 206, b'abcdefghi'),
])
def test_provider_rejects_changed_ignored_or_inexact_ranges(original, monkeypatch, changes, status, body):
    _, _, raw = original
    response = HTTPResponse(http_headers(**{'Content-Length': '8', 'Content-Range': f'bytes 0-7/{len(raw)}', **changes}), body, status)
    provider = acquisition.Provider.__new__(acquisition.Provider)
    monkeypatch.setattr(provider, '_open', lambda *args: response)
    with pytest.raises(ValueError):
        provider.read_range(0, 8)


def test_provider_head_requires_pinned_length_identity_and_range_support(original, monkeypatch):
    _, _, raw = original
    provider = acquisition.Provider.__new__(acquisition.Provider)
    headers = http_headers(**{'Content-Length': str(len(raw)), 'Accept-Ranges': 'bytes'})
    monkeypatch.setattr(provider, '_open', lambda method: HTTPResponse(headers, status=200))
    assert provider.head()['bytes'] == len(raw)
    for field, value in [('Content-Length', str(len(raw) + 1)), ('Accept-Ranges', 'none'), ('ETag', 'changed')]:
        before = dict(headers);headers[field] = value
        with pytest.raises(ValueError):
            provider.head()
        headers.clear();headers.update(before)


def test_source_redirects_are_rejected():
    with pytest.raises(ValueError, match='must not redirect'):
        acquisition.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.invalid/archive')
