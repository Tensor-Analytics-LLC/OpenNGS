"""Evidence for orchestration is identity preservation, not an assay benchmark."""
import copy
import hashlib

import pytest

from backend import capabilities, catalogue_view, config, db, entitlements, registry
from backend.portable import execute


def _artifacts():
    return [
        {'path': 's3://tenant-private/tenants/u_owner/runs/run/outputs/calls.vcf',
         'name': 'calls.vcf', 'format': 'INDEXED_VCF', 'bytes': 10, 'digest': 'sha256:'+'a'*64,
         'metadata': {'version_id': 'immutable-v1', 'assembly': 'GRCh38',
                      'index': 's3://tenant-private/tenants/u_owner/runs/run/outputs/calls.vcf.tbi',
                      'index_sha256': 'b'*64, 'secondary_objects': [
                          {'uri': 's3://tenant-private/tenants/u_owner/runs/run/outputs/calls.vcf.tbi',
                           'version_id': 'immutable-index-v1', 'sha256': 'b'*64, 'bytes': 7}],
                      'data_rights': [{'family': 'restricted-data', 'release': 'exact-v1'}]}},
        {'path': 's3://tenant-private/tenants/u_owner/runs/run/outputs/metrics.json',
         'name': 'metrics.json', 'format': 'REPORT', 'bytes': 4, 'digest': 'sha256:'+'c'*64,
         'metadata': {'version_id': 'metrics-v1'}},
    ]


@pytest.mark.parametrize('tool,port', [('utility.export', 'published'), ('utility.gather', 'collection')])
def test_route_preserves_all_artifact_identities_without_reading_scientific_bytes(tmp_path, tool, port):
    artifacts = _artifacts()
    inputs = {'first': artifacts, 'second': [artifacts[0]]}
    before = copy.deepcopy(inputs)
    output = execute({'tool': tool, 'inputs': inputs, 'workdir': str(tmp_path)})
    assert output == {port: [before['first'][0], before['first'][1], before['second'][0]]}
    assert inputs == before
    assert list(tmp_path.iterdir()) == []  # No copying, index construction or genomic output creation.


@pytest.mark.parametrize('minimum,maximum,accepted', [(0, 9, False), (10, 10, True), (11, 20, False), (0, 20, True)])
def test_condition_routes_by_inclusive_declared_byte_bounds(tmp_path, minimum, maximum, accepted):
    item = _artifacts()[0]
    output = execute({'tool': 'utility.condition', 'inputs': {'data': [item]},
                      'params': {'minimum_bytes': minimum, 'maximum_bytes': maximum}, 'workdir': str(tmp_path)})
    assert output == {'accepted': [item] if accepted else []}
    assert list(tmp_path.iterdir()) == []


def test_metadata_contract_is_not_a_scientific_worker_or_blanket_ready(client, owner, monkeypatch):
    monkeypatch.setattr(config, 'CLOUD', True)
    proof = {r['id']: r for r in capabilities.matrix()['capabilities']}
    for ident in capabilities.VERIFIED_ARTIFACT_ROUTING:
        evidence = proof[ident]
        assert evidence['status'] == 'implemented-verified'
        assert evidence['execution_kind'] == 'artifact-routing'
        assert evidence['hosted_runtime_image'] is None
        assert 'scientific' in evidence['validation_scope']
        available = catalogue_view._availability(
            registry.BASE[ident], evidence, {'rights_approved': [], 'research_profile': {}}, True, set(), {})
        assert available['state'] == 'Ready'
        quarantined = {**registry.BASE[ident], 'quarantined': 'Operator disabled this contract'}
        denied = catalogue_view._availability(quarantined, evidence, {'rights_approved': []}, True, set(), {})
        assert denied['state'] == 'Unavailable'
        assert any(b['code'] == 'QUARANTINED' for b in denied['blockers'])
        unverified = {**evidence, 'orchestration_contract': {}}
        denied = catalogue_view._availability(registry.BASE[ident], unverified, {'rights_approved': []}, True, set(), {})
        assert denied['state'] == 'Unavailable'
        assert any(b['code'] == 'ROUTING_UNVERIFIED' for b in denied['blockers'])


def test_actual_catalogue_quickstart_ready_after_verified_sample_publication(client, owner, monkeypatch):
    image = 'registry/curated@sha256:' + 'd' * 64
    monkeypatch.setenv('NGS_DEMO_IMAGE', image)
    monkeypatch.setattr(config, 'CLOUD', True)
    monkeypatch.setattr(entitlements, 'DEMO', True)
    manifest = config.ROOT/'demo-data/manifest.json'
    with db.tx() as c:
        c.execute('INSERT INTO controls(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                  ('reference-bundle:curated-samples-v1', db.encode({
                      'image': image, 'manifest_sha256': hashlib.sha256(manifest.read_bytes()).hexdigest(),
                      'scope': 'Test-only immutable publication record'})))
    catalogue_view.invalidate()
    try:
        response = client.get('/api/catalog')
        assert response.status_code == 200
        templates = {t['id']: t for t in response.json()['templates']}
        assert templates['quickstart']['ready_to_run'], templates['quickstart']['availability']
        # Fixing an orchestration classification cannot approve a scientific runtime.
        assert not templates['deepvariant']['ready_to_run']
    finally:
        catalogue_view.invalidate()
