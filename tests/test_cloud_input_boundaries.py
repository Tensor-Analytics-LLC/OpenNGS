"""The API never turns host files into uploads or broad worker path grants."""
from types import SimpleNamespace

import pytest

from backend import cloud, config, data, registry, retention
from scripts import load_cloud


@pytest.mark.parametrize('local_companion', [False, True])
def test_local_input_never_uploads_or_submits(monkeypatch, tmp_path, local_companion):
    image = 'registry/reviewed@sha256:' + '1' * 64
    reads = []
    def forbidden(*args, **kwargs):
        raise AssertionError('A rejected local input must not write to AWS or launch a worker')
    def head(**kwargs):
        reads.append(kwargs)
        return {'Metadata': {'sha256': 'a' * 64}, 'VersionId': 'version-one', 'ContentLength': 10}
    batch = SimpleNamespace(describe_job_definitions=lambda **kwargs: {'jobDefinitions': [{
        'status': 'ACTIVE', 'jobDefinitionArn': 'pinned:1', 'tags': {'NGSWorkerContract': '1'},
        'platformCapabilities': ['FARGATE'], 'containerProperties': {'image': image, 'readonlyRootFilesystem': True},
    }]}, submit_job=forbidden)
    s3 = SimpleNamespace(head_object=head, upload_file=forbidden, put_object=forbidden, create_multipart_upload=forbidden)
    session = SimpleNamespace(client=lambda name, **kwargs: {'batch': batch, 's3': s3}[name])
    monkeypatch.setattr(cloud, 'aws_session', lambda: session)
    monkeypatch.setattr(data, 'settings', lambda uid: {'s3_bucket': 'private', 'kms_key_arn': 'key', 'batch_job_definition': 'pinned:1'})
    monkeypatch.setattr(retention, 'lease', lambda *args, **kwargs: None)
    item = {'path': 's3://private/tenants/owner/data.bam' if local_companion else str(tmp_path/'host-private.bam'),
            'metadata': {'index': str(tmp_path/'host-private.bai')} if local_companion else {}}
    node = {'tool': 'bam.stats', 'params': {}, 'resources': {'threads': 1, 'memory_mb': 2048, 'timeout_seconds': 30},
            '_approved_image': image}
    with pytest.raises(ValueError, match='verified cloud objects'):
        cloud.execute_batch('owner', 'run', 'attempt', registry.BASE['bam.stats'], node,
                            {'alignment': [item]}, tmp_path, lambda _: False)
    assert len(reads) == int(local_companion)


def test_worker_allowlist_is_exact_and_rejects_symlink_escape(monkeypatch, tmp_path):
    work = tmp_path/'isolated'
    work.mkdir()
    approved = work/'approved.vcf'
    approved.write_text('approved')
    sibling = work/'other.vcf'
    sibling.write_text('not granted')
    outside = tmp_path/'host-file'
    outside.write_text('private')
    link = work/'escape.vcf'
    link.symlink_to(outside)
    monkeypatch.setattr(config, 'CLOUD', False)
    monkeypatch.setattr(config, 'DATABASE_URL', '')
    monkeypatch.setattr(config, 'STATE', work/'state')
    monkeypatch.setattr(data, 'WORKER_INPUTS', frozenset())
    data.bind_worker_inputs([str(approved)], work)
    assert data.permitted_path('isolated', approved) == approved.resolve()
    for path in [sibling, outside, link]:
        with pytest.raises(ValueError, match='working directory'):
            data.permitted_path('isolated', path)
    with pytest.raises(ValueError, match='outside'):
        data.bind_worker_inputs([link], work)
    # A failed replacement grant cannot widen the previous successful allowlist.
    assert data.WORKER_INPUTS == frozenset({approved.resolve()})


@pytest.mark.parametrize('cloud_mode,database,state_outside', [(True, '', False), (False, 'postgresql://server/app', False), (False, '', True)])
def test_worker_bindings_never_apply_to_application_state(monkeypatch, tmp_path, cloud_mode, database, state_outside):
    work = tmp_path/'worker'
    work.mkdir()
    source = work/'source.vcf'
    source.write_text('fixture')
    monkeypatch.setattr(config, 'CLOUD', cloud_mode)
    monkeypatch.setattr(config, 'DATABASE_URL', database)
    monkeypatch.setattr(config, 'STATE', tmp_path/'app' if state_outside else work/'state')
    with pytest.raises(ValueError, match='isolated worker state'):
        data.bind_worker_inputs([source], work)


def test_load_script_is_inert_without_explicit_account_creation_opt_in(monkeypatch):
    def no_aws():
        raise AssertionError('Default validation invocation must never contact AWS')
    monkeypatch.setattr(load_cloud, 'aws_session', no_aws)
    for authorization in [None, False, True, 0, 101, '100']:
        with pytest.raises(ValueError, match='Explicit authorization'):
            load_cloud.prepare(allow_synthetic_users=authorization)
    with pytest.raises(SystemExit) as stopped:
        load_cloud.main([])
    assert stopped.value.code == 2
