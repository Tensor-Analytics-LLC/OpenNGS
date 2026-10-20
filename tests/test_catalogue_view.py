import time

import pytest

from backend import catalogue_view, config, data, db, demo, entitlements, registry


@pytest.fixture(autouse=True)
def clear_catalogue_cache():
    catalogue_view.invalidate()
    yield
    catalogue_view.invalidate()


def test_explore_discovers_full_catalogue_without_execution_authority(client, owner, monkeypatch):
    monkeypatch.setattr(config, 'CLOUD', True)
    monkeypatch.setattr(entitlements, 'DEMO', True)
    result = catalogue_view.view(owner['id'])
    visible = {n['id']: n for n in result['nodes']}
    public = {n['id'] for n in registry.NODES if not n.get('hidden')}
    assert public <= visible.keys()
    assert {'resource.gnomad', 'resource.clinvar', 'resource.grch38', 'vep', 'deepvariant'} <= visible.keys()
    assert 'results.prepare' not in visible
    assert visible['resource.grch38']['availability']['state'] == 'Requires entitlement'
    assert visible['resource.grch38']['asset_candidates'] == []
    with pytest.raises(ValueError, match='Research Pro|hosted sample tool'):
        demo.validate({'nodes': [{'tool': 'deepvariant'}], 'edges': []}, owner['id'])
    assert all('availability' in n and n['availability']['reason'] for n in visible.values())


def test_asset_candidates_only_include_tenant_verified_cloud_versions(client, owner, monkeypatch):
    monkeypatch.setattr(config, 'CLOUD', True)
    monkeypatch.setattr(entitlements, 'restricted', lambda uid: False)
    monkeypatch.setattr(data, 'settings', lambda uid: {'rights_approved': [], 'research_profile': {'state': 'Ready'}})
    now = time.time()
    uid = owner['id']
    with db.tx() as c:
        for ident, state, path, version, tenant in [
            ('ready', 'Ready', 's3://own/ready', 'v1', uid),
            ('pending', 'Verifying', 's3://own/pending', 'v1', uid),
            ('ssd', 'Ready', '/private/local-reference.fa', 'v1', uid),
            ('unversioned', 'Ready', 's3://own/unversioned', None, uid),
            ('foreign', 'Ready', 's3://foreign/data', 'v1', 'other-user'),
        ]:
            c.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?,?)', (ident, tenant, path, ident+'.fa', 'FASTA', 20, now, 'reference', 'GRCh38', '{}'))
            c.execute('INSERT INTO datasets VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', (ident, tenant, None, ident+'.fa', 20, 'a'*64, state, ident, version, '{}', now, now))
    nodes = {n['id']: n for n in catalogue_view.view(uid)['nodes']}
    assert [a['id'] for a in nodes['resource.grch38']['asset_candidates']] == ['ready']
    assert nodes['resource.grch38']['parameters'][0]['default'] == ''


def test_visibility_does_not_mark_unexecuted_runtime_as_ready(client, owner, monkeypatch):
    monkeypatch.setattr(config, 'CLOUD', True)
    monkeypatch.setattr(entitlements, 'restricted', lambda uid: False)
    monkeypatch.setattr(data, 'settings', lambda uid: {'rights_approved': [], 'research_profile': {'state': 'Ready', 'image': 'registry/core@sha256:'+'a'*64, 'tools': ['deepvariant']}})
    nodes = {n['id']: n for n in catalogue_view.view(owner['id'])['nodes']}
    assert nodes['deepvariant']['availability']['state'] == 'Unavailable'
    assert 'execution case' in nodes['deepvariant']['availability']['reason']
    assert 'Ready' not in nodes['deepvariant']['availability']['validation']


def test_migrated_original_ids_are_discoverable_by_owner_admin_without_execute_rights(client, owner, monkeypatch):
    monkeypatch.setattr(config, 'CLOUD', True)
    monkeypatch.setattr(entitlements, 'restricted', lambda uid: True)
    uid, now = owner['id'], time.time()
    metadata = {'dataset_id': 'dataset1', 'sha256': 'a'*64, 'version_id': 'exact-v1', 'uploaded': True}
    with db.tx() as c:
        for ident, path, meta in [
            ('canonical', 's3://shared-migration/tenants/'+uid+'/data', metadata),
            ('original', '/Volumes/source/reference.fa', {'cloud_dataset_id': 'dataset1'}),
            ('uploaded-original', 's3://own/uploaded.fa', {}),
        ]:
            c.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?,?)', (ident, uid, path, ident+'.fa', 'FASTA', 20, now, 'reference', 'GRCh38', db.encode(meta)))
        c.execute('INSERT INTO datasets VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', ('dataset1', uid, 'canonical', 'exact.fa', 20, 'a'*64, 'Ready', 'key', 'exact-v1', '{}', now, now))
        c.execute('INSERT INTO datasets VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', ('dataset2', uid, 'uploaded-original', 'upload.fa', 20, 'b'*64, 'Ready', 'uploaded', 'exact-v2', '{}', now, now))
        c.execute('INSERT INTO migration_assets VALUES(?,?,?,?,?,?)', ('original', uid, 'dataset1', 'canonical', '/Volumes/source/reference.fa', '{}'))
    nodes = {n['id']: n for n in catalogue_view.view(uid)['nodes']}
    candidates = nodes['resource.grch38']['asset_candidates']
    assert {a['id'] for a in candidates} == {'canonical', 'original', 'uploaded-original'}
    assert all('path' not in a and 'metadata' not in a for a in candidates)
    assert nodes['resource.grch38']['availability']['state'] == 'Requires entitlement'
    with db.tx() as c:
        c.execute("UPDATE users SET role='user' WHERE id=?", (uid,))
    assert next(n for n in catalogue_view.view(uid)['nodes'] if n['id'] == 'resource.grch38')['asset_candidates'] == []


def test_shared_evidence_is_cached_but_tenant_entitlements_are_live(client, owner, monkeypatch):
    monkeypatch.setattr(entitlements, 'restricted', lambda uid: True)
    first = catalogue_view.view(owner['id'])
    observed = first['readiness_updated_at']
    monkeypatch.setattr(catalogue_view.capabilities, 'matrix', lambda uid: pytest.fail('Evidence reparsed within 20 second cache'))
    second = catalogue_view.view(owner['id'])
    assert second['readiness_updated_at'] == observed
    assert first['nodes'] == second['nodes']
    assert all('manifest' not in row for row in second['dependencies'])


def test_large_alias_inventory_uses_one_listing_query(client, owner, monkeypatch):
    from backend import asset_listing, migration
    uid, now = owner['id'], time.time()
    with db.tx() as c:
        c.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?,?)', ('bulk-canonical',uid,'s3://private/data','bulk.fa','FASTA',20,now,'reference','GRCh38','{}'))
        c.execute('INSERT INTO datasets VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', ('bulk-dataset',uid,'bulk-canonical','bulk.fa',20,'a'*64,'Ready','key','v1','{}',now,now))
        for i in range(1000):
            ident='bulk-alias-'+str(i)
            c.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?,?)', (ident,uid,'/source/'+ident,ident+'.fa','FASTA',20,now,'reference','GRCh38',db.encode({'cloud_dataset_id':'bulk-dataset'})))
            c.execute('INSERT INTO migration_assets VALUES(?,?,?,?,?,?)', (ident,uid,'bulk-dataset','bulk-canonical','/source/'+ident,'{}'))
    calls=[]
    query=db.query
    def counted(*args,**kwargs):
        calls.append(args[0])
        return query(*args,**kwargs)
    monkeypatch.setattr(db,'query',counted)
    monkeypatch.setattr(migration,'resolve_asset',lambda *a:pytest.fail('Per-alias runtime resolution on discovery path'))
    rows=asset_listing.list_assets(uid,cloud=True)
    assert len(rows)==1001
    assert len(calls)==4  # Asset CTE, grants, known digests and original migration records; independent of alias count.
    assert all(row['path']=='s3://private/data' for row in rows)
    assert asset_listing.list_assets('foreign-user',cloud=True)==[]
    assert len(asset_listing.list_assets(uid,cloud=True,query='bulk-alias',limit=300))==300


def test_static_resolution_route_precedes_asset_id_route(client, owner, monkeypatch):
    monkeypatch.setattr(data,'resolve',lambda uid,family,assembly,backend,source_id=None,asset_id=None:[{'id':'resolved','family':family}])
    response=client.get('/api/assets/resolve?family=reference')
    assert response.status_code==200
    assert response.json()['candidates'][0]['id']=='resolved'


def test_discovery_separates_allowance_key_and_runtime_requirements(client, owner, monkeypatch):
    monkeypatch.setattr(config, 'CLOUD', True)
    monkeypatch.setattr(entitlements, 'restricted', lambda uid: True)
    rows = {n['id']: n for n in catalogue_view.view(owner['id'])['nodes']}
    sentieon = rows['sentieon']['availability']['blockers']
    assert {'licence', 'credential', 'allowance', 'runtime'} <= {item['kind'] for item in sentieon}
    reference = rows['resource.grch38']['availability']['blockers']
    assert {'dependency', 'allowance'} <= {item['kind'] for item in reference}
    assert 'credential' not in {item['kind'] for item in reference}
    assert all(item['code'] and item['message'] for item in sentieon + reference)


def test_primary_filter_runs_before_limit_and_public_metadata_omits_host_paths(client, owner):
    from backend.asset_listing import list_assets, public_metadata
    uid, now = owner['id'], time.time()
    with db.tx() as c:
        for ident,name,fmt in [('index','a.vcf.gz.tbi','VCF'),('primary','b.vcf.gz','BGZIP_VCF')]:
            metadata={'root':'/Volumes/source','sha256':'a'*64,'version_id':'v1',
                      'index_paths':['/Volumes/source/old.tbi','s3://own/index.tbi'],
                      'source_identity':{'path':'/private/old','release':'release-2026.10'}}
            c.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?,?)',
                      (ident,uid,'s3://own/'+name,name,fmt,10,now,'clinvar','GRCh38',db.encode(metadata)))
            c.execute('INSERT INTO datasets VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                      (ident,uid,None,name,10,'a'*64,'Ready',ident,'v1','{}',now,now))
    rows=list_assets(uid,cloud=True,primary_only=True,limit=1)
    assert [row['id'] for row in rows] == ['primary']
    assert rows[0]['metadata']['sha256']=='a'*64
    assert rows[0]['metadata']['index_paths']==['s3://own/index.tbi']
    assert rows[0]['metadata']['source_identity']=={'release':'release-2026.10'}
    assert 'root' not in rows[0]['metadata']
    assert public_metadata({'path':r'C:\data\db.vcf','url':'https://provider/data'}) == {'url':'https://provider/data'}
    assert [row['id'] for row in list_assets(uid,cloud=True,primary_only=True,query='grch38')] == ['primary']
    assert [row['id'] for row in list_assets(uid,cloud=True,primary_only=True,query='release-2026.10')] == ['primary']
    assert list_assets('foreign-user',cloud=True,primary_only=True,query='release-2026.10') == []
