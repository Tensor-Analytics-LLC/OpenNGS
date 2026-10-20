"""Transfer readiness never certifies a conflicting scientific database release."""
import hashlib
import json
import time

import pytest

from backend import asset_listing, catalogue_view, config, data, db, demo, entitlements, graph, migration, runner

GNOMAD = 'c3a336ec1697eda0531b7b07d58432f1370d72a669bf263185aa205a9e544b34'
INDEX = '1b2ebbd0c79f6b97b1408ca76c8e1abb80158d3bc1cba396a1b08479d9d3c69a'


@pytest.fixture
def cloud_source(owner, monkeypatch):
    """Only metadata is synthetic; no cloud service or genomic bytes are read."""
    uid = owner['id']; now = time.time(); bucket = 'qualification-test'
    monkeypatch.setenv('NGS_BUCKET', bucket)
    monkeypatch.setattr(config, 'CLOUD', True)
    monkeypatch.setattr(graph, 'CLOUD', True)
    monkeypatch.setattr(entitlements, 'restricted', lambda ident: False)
    monkeypatch.setattr(demo, 'validate', lambda *args: None)
    monkeypatch.setattr(runner, 'capabilities', lambda: {'portable': True, 'docker': False})
    catalogue_view.invalidate()
    with db.tx() as c:
        c.execute('DELETE FROM migration_assets')
        c.execute('DELETE FROM migration_objects')

    def add(ident='original', digest=GNOMAD, name='gnomad.exomes.v4.1.sites.chr3.vcf.bgz', fmt='INDEXED_VCF', metadata=None):
        key = 'tenants/' + uid + '/datasets/' + ident + '/' + name
        meta = {'sha256': digest, 'version_id': 'original-version-' + ident,
                'dataset_id': ident, 'uploaded': True, 'header_inspected': True,
                'release_qualified': True, 'release': '4.1', **(metadata or {})}
        # A caller-supplied or historical "qualified" metadata flag cannot
        # override the authoritative exact-content restriction.
        manifest = {'verification_method': 'raw-cloud-sha256', 'private_owner_only': True}
        with db.tx() as c:
            c.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?,?)',
                (ident, uid, 's3://' + bucket + '/' + key, name, fmt, 100, now, 'gnomad', 'GRCh38', db.encode(meta)))
            c.execute('INSERT INTO datasets VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (ident, uid, ident, name, 100, digest, 'Ready', key, meta['version_id'], db.encode(manifest), now, now))
            source = {'id': ident, 'name': name, 'format': fmt, 'family': 'gnomad', 'metadata': {}}
            c.execute('INSERT INTO migration_assets VALUES(?,?,?,?,?,?)',
                (ident, uid, ident, ident, '/preserved-source/' + name, db.encode(source)))
        return db.query('SELECT * FROM assets WHERE id=? AND user_id=?', (ident, uid), one=True)

    yield owner, add
    catalogue_view.invalidate()


def source_graph(ident, tool='resource.gnomad'):
    return {'nodes': [{'id': 'database', 'tool': tool, 'params': {'asset_id': ident, 'assembly': 'GRCh38'}}], 'edges': []}


def test_exact_public_conflict_and_companion_cannot_escape_by_renaming_or_tenant(owner):
    for uid in ['original-owner', 'a-different-owner']:
        for digest in [GNOMAD, INDEX, 'sha256:' + GNOMAD]:
            result = migration.scientific_qualification(uid, digest)
            assert result['execution_allowed'] is False
            assert result['code'] == 'DATA_QUALIFICATION_REQUIRED'
            assert 'Release check' in result['reason'] and 'licence' not in result['reason'].lower()
    assert migration.public_qualification_warnings(hashlib.sha256(b'unrelated').hexdigest()) == []


def test_raw_ready_is_preserved_and_download_authorization_still_works(cloud_source):
    owner, add = cloud_source; row = add(); uid = owner['id']
    before = db.query('SELECT * FROM assets WHERE id=?', (row['id'],), one=True)
    proof = migration.authorize_source(uid, row['path'], 'original-version-original', GNOMAD)
    assert proof['sha256'] == GNOMAD and proof['VersionId'] == 'original-version-original'
    with pytest.raises(ValueError, match='Release check'):
        migration.authorize_source(uid, row['path'], scientific=True)
    assert db.query('SELECT * FROM assets WHERE id=?', (row['id'],), one=True) == before
    assert db.query('SELECT state FROM datasets WHERE id=?', (row['id'],), one=True)['state'] == 'Ready'


def test_catalogue_excludes_unqualified_primary_but_retains_raw_discovery_and_reason(cloud_source):
    owner, add = cloud_source; row = add(); uid = owner['id']
    assets = asset_listing.list_assets(uid)
    observed = next(asset for asset in assets if asset['id'] == row['id'])
    assert observed['qualification']['execution_allowed'] is False
    assert observed['presentation']['role'] == 'Release check required'
    assert observed['presentation']['primary_eligible'] is False
    assert asset_listing.list_assets(uid, primary_only=True) == []
    assert asset_listing.list_assets(uid, node_id='resource.gnomad') == []
    node = next(node for node in catalogue_view.view(uid)['nodes'] if node['id'] == 'resource.gnomad')
    assert node['asset_candidates'] == [] and node['asset_candidate_count'] == 0
    assert node['availability']['state'] == 'Unavailable'
    assert any(item['code'] == 'DATA_QUALIFICATION_REQUIRED' for item in node['availability']['blockers'])
    assert 'Release check' in node['availability']['reason']
    assert not any(item['code'] == 'DATA_RIGHTS' for item in node['availability']['blockers'])


@pytest.mark.parametrize('tool', ['resource.gnomad', 'database.asset', 'asset.file'])
def test_modified_graph_and_api_cannot_admit_raw_checksum_only_input(cloud_source, client, tool):
    owner, add = cloud_source; row = add(name='renamed-population.vcf.bgz'); uid = owner['id']
    g = source_graph(row['id'], tool)
    check = client.post('/api/graph/preflight', json={'graph': g})
    assert check.status_code == 200
    assert any(issue['code'] == 'DATA_QUALIFICATION_REQUIRED' for issue in check.json()['issues'])
    response = client.post('/api/runs', json={'graph': g}, headers={'Idempotency-Key': 'qualification-' + tool.replace('.', '_')})
    assert response.status_code == 422 and 'DATA_QUALIFICATION_REQUIRED' in response.text
    assert db.query('SELECT id FROM runs WHERE user_id=?', (uid,)) == []
    with pytest.raises(ValueError, match='Release check'):
        runner.input_asset(uid, row['id'])


def test_unqualified_companion_as_primary_or_bound_index_is_rejected(cloud_source):
    owner, add = cloud_source; uid = owner['id']
    companion = add('index', INDEX, 'renamed-index.tbi', 'INDEX')
    main = add('otherwise-ready', 'd' * 64, metadata={'index_sha256': INDEX})
    for row in [companion, main]:
        with pytest.raises(ValueError, match='Release check'):
            runner.input_asset(uid, row['id'])
    assert asset_listing.list_assets(uid, primary_only=True) == []
    proof = migration.authorize_source(uid, companion['path'])
    assert proof['sha256'] == INDEX
    with pytest.raises(ValueError, match='Release check'):
        migration.source_manifest(uid, {'path': main['path'], 'digest': 'sha256:' + 'd' * 64,
            'metadata': {'version_id': 'original-version-otherwise-ready', 'index': companion['path']}})


def test_unrelated_qualified_input_and_other_tenant_warning_are_unchanged(cloud_source):
    owner, add = cloud_source; uid = owner['id']; digest = 'd' * 64
    row = add('qualified', digest)
    with db.tx() as c:
        c.execute('INSERT INTO controls VALUES(?,?)', ('migration:provenance:other-user:' + digest,
            db.encode([{'code': 'private-conflict', 'source_sha256': digest, 'release_qualified': False}])))
    before = db.query('SELECT * FROM assets WHERE id=?', (row['id'],), one=True)
    assert migration.scientific_qualification(uid, digest)['execution_allowed'] is True
    assert runner.input_asset(uid, row['id'])['digest'] == 'sha256:' + digest
    assert [item['id'] for item in asset_listing.list_assets(uid, primary_only=True)] == [row['id']]
    node = next(node for node in catalogue_view.view(uid)['nodes'] if node['id'] == 'resource.gnomad')
    assert node['availability']['state'] == 'Ready'
    assert [item['id'] for item in node['asset_candidates']] == [row['id']]
    assert db.query('SELECT * FROM assets WHERE id=?', (row['id'],), one=True) == before


def test_owner_provenance_restriction_is_authoritative_without_mutating_rights(cloud_source):
    owner, add = cloud_source; uid = owner['id']; digest = 'e' * 64
    row = add('other-conflict', digest)
    migration.record_provenance_warning('reviewed-operator', uid, digest,
        {'code': 'representative-conflict', 'message': 'Original release needs qualification'})
    assert migration.scientific_qualification(uid, digest)['execution_allowed'] is False
    with pytest.raises(ValueError, match='Release check'):
        runner.input_asset(uid, row['id'])
    assert db.query('SELECT * FROM data_rights_grants WHERE user_id=?', (uid,)) == []
