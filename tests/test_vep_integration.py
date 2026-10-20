"""Production wiring gates; tiny records here are validator fixtures, not cloud proof."""
import copy
import json
import sys
from pathlib import Path

import pytest

from backend import capabilities, catalogue_view, cloud, compiler, config, db, provisioning, registry
from backend import vep_runtime as runtime
from infra.research_stack import template
from test_vep_runtime import release


OWNER = 'u_' + 'a' * 32


def cloud_object(name, digest, size):
    return {'uri': 's3://shared/tenants/' + OWNER + '/datasets/cache/' + name,
            'version_id': 'immutable-' + name, 'sha256': digest, 'bytes': size}


def bundle():
    return {'state': 'Ready', 'verification': 'complete-vep-cache-cloud-v1', 'owner_id': OWNER,
            'image': release()['image'], 'cache_version': 115, 'assembly': 'GRCh38',
            'archive': cloud_object(runtime.CACHE_ARCHIVE_NAME, runtime.CACHE_ARCHIVE_SHA256, runtime.CACHE_ARCHIVE_BYTES),
            'reference': cloud_object('reference.fa', '1' * 64, 3200000000),
            'index': cloud_object('reference.fa.fai', '2' * 64, 10000),
            'complete_cache': {'members': runtime.CACHE_MEMBER_COUNT, 'extracted_bytes': runtime.CACHE_EXTRACTED_BYTES,
                               'member_manifest_sha256': runtime.CACHE_MANIFEST_SHA256, 'info_sha256': runtime.CACHE_INFO_SHA256},
            'reference_verification': {'fasta_sha256': '1' * 64, 'fai_sha256': '2' * 64,
                                       'all_fai_fields_validated': True, 'contigs': 25},
            'execution': {'status': 'SUCCEEDED', 'job_id': 'validator-fixture-only', 'scratch_bytes': 80 * 1024**3,
                          'all_members_verified': True, 'artifact': cloud_object('proof.json', '3' * 64, 1000)}}


def register(key, value):
    with db.tx() as connection:
        connection.execute('INSERT INTO controls VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, db.encode(value)))


def inputs():
    proof = bundle()
    result = {}
    for port, key, name, fmt in [('database', 'archive', runtime.CACHE_ARCHIVE_NAME, 'ARCHIVE'),
                                 ('reference', 'reference', 'reference.fa', 'FASTA')]:
        obj = proof[key]
        result[port] = [{'name': name, 'path': obj['uri'], 'bytes': obj['bytes'], 'format': fmt,
                         'digest': 'sha256:' + obj['sha256'], 'assembly': 'GRCh38',
                         'metadata': {'version_id': obj['version_id'], 'assembly': 'GRCh38'}}]
    index = proof['index']
    result['reference'][0]['metadata'].update(index=index['uri'], index_sha256=index['sha256'],
                                              secondary_objects=[index])
    result['variants'] = [{'path': 's3://shared/tenants/' + OWNER + '/input.vcf', 'format': 'VCF', 'metadata': {'assembly': 'GRCh38'}}]
    return result


def test_optional_vep_definition_adds_no_roles_or_capacity_and_preserves_existing_definitions():
    from test_deepvariant_runtime import release as deepvariant_release
    args = ('subnet', 'security', 'execution', 'registry/core@sha256:' + 'd' * 64,
            'repo', 'shared', 'key', 'boundary', 'https://example.test', 'topic')
    original = template(*args, deepvariant=deepvariant_release())
    result = template(*args, deepvariant=deepvariant_release(), vep=release())
    assert set(result['Resources']) - set(original['Resources']) == {'VEPDefinition'}
    assert all(result['Resources'][key] == value for key, value in original['Resources'].items())
    props = result['Resources']['VEPDefinition']['Properties']['ContainerProperties']
    core = result['Resources']['Definition']['Properties']['ContainerProperties']
    assert props['Image'] == release()['image']
    assert props['Command'] == ['/usr/bin/python3', '/opt/studio/cloud_worker.py']
    for key in set(core) - {'Image', 'Command'}:
        assert props[key] == core[key]
    assert props['EphemeralStorage']['SizeInGiB'] == 200 and props['ReadonlyRootFilesystem']
    assert result['Outputs']['VEPCacheVersion']['Value'] == '115'
    assert result['Outputs']['VEPCacheManifest']['Value'] == runtime.CACHE_MANIFEST_SHA256
    bad = release();bad['qualification']['cases'].pop()
    with pytest.raises(ValueError, match='Every VEP'):
        template(*args, vep=bad)


def test_vep_route_requires_all_published_runtime_and_cache_identities(client):
    proof = release()
    outputs = {'VEPImage': proof['image'], 'VEPDefinition': 'reviewed-vep-definition', 'VEPTools': '["vep"]',
               'VEPSource': proof['source_sha256'], 'VEPCacheVersion': '115', 'VEPAssembly': 'GRCh38',
               'VEPCacheManifest': runtime.CACHE_MANIFEST_SHA256}
    assert provisioning.vep_routes({}) == {} and 'vep' not in provisioning.CORE_TOOLS
    with pytest.raises(ValueError, match='exact published image'):
        provisioning.vep_routes(outputs)
    register('runtime-release:' + runtime.FAMILY, proof)
    route = provisioning.vep_routes(outputs)['vep']
    assert route['source_grants_version'] == 1 and route['cache_version'] == 115
    assert route['image'] == proof['image'] and route['definition'] == 'reviewed-vep-definition'
    for field in outputs:
        bad = dict(outputs);bad.pop(field)
        with pytest.raises(ValueError):
            provisioning.vep_routes(bad)
    for field, value in [('VEPCacheVersion', '116'), ('VEPAssembly', 'GRCh37'), ('VEPSource', '0' * 64), ('VEPTools', '["vep","other"]')]:
        with pytest.raises(ValueError):
            provisioning.vep_routes({**outputs, field: value})


def test_complete_cloud_bundle_rejects_shards_mutable_versions_and_incomplete_reference(client):
    register('runtime-release:' + runtime.FAMILY, release())
    assert provisioning.validate_vep_bundle(bundle())['complete_cache']['members'] == 13922
    mutations = [('archive', 'bytes', 563872), ('archive', 'sha256', '0' * 64), ('archive', 'version_id', 'null'),
                 ('complete_cache', 'members', 1), ('complete_cache', 'extracted_bytes', 563872),
                 ('complete_cache', 'member_manifest_sha256', '0' * 64),
                 ('reference_verification', 'all_fai_fields_validated', False),
                 ('reference_verification', 'fai_sha256', '0' * 64),
                 ('execution', 'all_members_verified', False), ('execution', 'scratch_bytes', 79 * 1024**3)]
    for section, key, value in mutations:
        bad = bundle();bad[section][key] = value
        with pytest.raises(ValueError):
            provisioning.validate_vep_bundle(bad)
    bad = bundle();bad['image'] = bad['image'][:-1] + 'f'
    with pytest.raises(ValueError):
        provisioning.validate_vep_bundle(bad)


def test_compiler_requires_exact_verified_archive_and_reference_then_uses_fixed_wrapper(client, tmp_path):
    spec = registry.BASE['vep'];node = {'params': {}, 'resources': copy.deepcopy(spec['resources'])}
    source = inputs()
    register('runtime-release:' + runtime.FAMILY, release())
    with pytest.raises(ValueError, match='awaiting cloud bundle verification'):
        compiler.compile_tool(spec, node, source, tmp_path)
    register('reference-bundle:vep-cache', bundle())
    plan = compiler.compile_tool(spec, node, source, tmp_path)
    argv = plan['steps'][0]['argv']
    assert argv[:3] == ['/usr/bin/python3', '-m', 'backend.vep_tools']
    assert argv[argv.index('--cache-archive') + 1].endswith(runtime.CACHE_ARCHIVE_NAME)
    assert argv[argv.index('--fai') + 1] == '/inputs/reference/0/reference.fa.fai'
    assert not {'--plugin', '--extra', '--everything', '--gff'}.intersection(argv)
    assert plan['outputs']['variants'][0]['format'] == 'INDEXED_VCF'
    assert plan['outputs']['report'][0]['file'] == 'annotated.vcf.gz.runtime.json'
    assert (bundle()['index']['uri'], '/inputs/reference/0/reference.fa.fai') in plan['bindings']
    # database.asset routes a verified archive in a DATABASE envelope.
    routed = copy.deepcopy(source);routed['database'][0]['format'] = 'DATABASE'
    routed['database'][0]['metadata']['underlying_format'] = 'ARCHIVE'
    assert compiler.compile_tool(spec, node, routed, tmp_path)['steps'] == plan['steps']
    assert registry.compatible('DATABASE', spec['inputs'][1]['type'])
    for mutate in [lambda i: i['database'][0]['metadata'].update(version_id='other-version'),
                   lambda i: i['reference'][0]['metadata'].update(secondary_objects=[]),
                   lambda i: i['reference'][0]['metadata'].update(index_sha256='0' * 64),
                   lambda i: i['database'][0].update(name='25000001-26000000.gz'),
                   lambda i: i['database'][0].update(format='DATABASE')]:
        bad = copy.deepcopy(source);mutate(bad)
        with pytest.raises(ValueError):
            compiler.compile_tool(spec, node, bad, tmp_path)
    for bad in [{'params': {'assembly': 'GRCh37'}}, {'params': {'cache_version': 116}},
                {'params': {'species': 'mus_musculus'}}, {'params': {'plugin': 'Any'}},
                {'advanced': '--plugin Any'}, {'resources': {'scratch_mb': 81919}}]:
        with pytest.raises(ValueError):
            compiler.compile_tool(spec, {**node, **bad}, source, tmp_path)


def test_qualified_vep_runtime_never_promotes_unverified_production_cache(client, monkeypatch):
    register('runtime-release:' + runtime.FAMILY, release())
    register('reference-bundle:vep-cache', {'state': 'Ready', 'reason': 'A single transcript shard passed'})
    monkeypatch.setattr(config, 'CLOUD', True)
    evidence = next(row for row in capabilities.matrix()['capabilities'] if row['id'] == 'vep')
    dependency = next(row for row in capabilities.dependencies() if row['id'] == 'vep-cache')
    assert '10 human' in evidence['runtime_status'] and evidence['sample_execution']['assertions']['annotated_records'] == 10
    assert evidence['migrated_data_runtime']['source_grants_version'] == 1
    assert dependency['state'] == 'Unavailable' and dependency['manifest'] is None
    settings = {'_workspace_id': OWNER, 'rights_approved': [], 'research_profile': {'state': 'Ready',
                'runtime_profiles': {'vep': {'image': release()['image']}}}}
    def available(dep):
        return catalogue_view._availability(registry.BASE['vep'], evidence, settings, False, set(), {'dependencies': [dep]})
    assert available(dependency)['state'] == 'Unavailable'
    assert any(row['code'] == 'VEP_COMPLETE_BUNDLE' for row in available(dependency)['blockers'])
    register('reference-bundle:vep-cache', bundle())
    dependency = next(row for row in capabilities.dependencies() if row['id'] == 'vep-cache')
    assert dependency['state'] == 'Ready' and available(dependency)['state'] == 'Ready'
    settings['_workspace_id'] = 'u_' + 'f' * 32
    assert available(dependency)['state'] == 'Unavailable'


def test_only_verified_vep_route_uses_python3_and_other_routes_keep_existing_command(client):
    definition = {'tags': {'NGSRuntimeFamily': runtime.FAMILY},
                  'containerProperties': {'command': ['/usr/bin/python3', '/opt/studio/cloud_worker.py']}}
    with pytest.raises(ValueError):
        cloud.worker_command(registry.BASE['vep'], definition, release()['image'])
    register('runtime-release:' + runtime.FAMILY, release())
    assert cloud.worker_command(registry.BASE['vep'], definition, release()['image'])[0] == '/usr/bin/python3'
    for bad in [{}, {**definition, 'tags': {}}, {**definition, 'containerProperties': {'command': ['python', '/opt/studio/cloud_worker.py']}}]:
        with pytest.raises(ValueError):
            cloud.worker_command(registry.BASE['vep'], bad, release()['image'])
    with pytest.raises(ValueError):
        cloud.worker_command(registry.BASE['vep'], definition, release()['image'][:-1] + 'f')
    for name in ['deepvariant', 'fastqc', 'qc.summary']:
        assert cloud.worker_command(registry.BASE[name], {}, 'unchanged') == ['python', '/opt/studio/cloud_worker.py']


def test_publication_loader_ignores_candidate_and_rejects_incomplete_proof_before_aws(tmp_path, monkeypatch):
    from scripts.publish_research_capacity import optional_vep_release
    monkeypatch.chdir(tmp_path)
    folder = tmp_path / 'evidence/deployment';folder.mkdir(parents=True)
    (folder / 'vep-release-candidate.json').write_text(json.dumps(release()))
    assert optional_vep_release() is None
    (folder / 'vep-release.json').write_text(json.dumps(release()))
    assert optional_vep_release()['tools'] == ['vep']
    invalid = release();invalid['qualification']['cases'].pop()
    (folder / 'vep-release.json').write_text(json.dumps(invalid))
    with pytest.raises(ValueError):
        optional_vep_release()


def test_service_reconciliation_adds_only_runtime_control_and_preserves_restart_selection(tmp_path, monkeypatch):
    from scripts import configure_services
    captured = []
    research = {'template_url': 'immutable-template', 'cfn_role': 'cfn-role', 'budget_topic': 'topic',
                'runtime_image': 'core-image', 'runtime_build': 'core-build', 'vep': release()}
    folder = tmp_path / 'evidence/deployment';folder.mkdir(parents=True)
    (folder / 'research-capacity.json').write_text(json.dumps(research))
    class Provider:
        def client(self, *args, **kwargs):return self
        def describe_stacks(self, StackName):
            outputs = {'HostId': 'host'} if StackName == 'openngs-lean' else {'QueueURL': 'q', 'WarningTopic': 'w', 'CriticalTopic': 'c'}
            return {'Stacks': [{'Outputs': [{'OutputKey': key, 'OutputValue': value} for key, value in outputs.items()]}]}
        def describe_instances(self, **kwargs):
            return {'Reservations': [{'Instances': [{'VpcId': 'vpc', 'SecurityGroups': []}]}]}
        def describe_security_groups(self, **kwargs):
            return {'SecurityGroups': [{'GroupName': 'CloudFront-VPCOrigins-Service-SG', 'GroupId': 'sg'}]}
        def get_caller_identity(self):return {'Account': '509505638328'}
        def send_command(self, **kwargs):captured.append(kwargs);return {'Command': {'CommandId': 'fixture-command'}}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(configure_services, 'aws_session', lambda: Provider())
    monkeypatch.setattr(sys, 'argv', ['configure_services.py', '--restart-services', 'openngs', 'openngs-research', 'openngs'])
    configure_services.main()
    script = captured[0]['Parameters']['commands'][0]
    assert 'runtime-release:ensembl-vep-offline-v1' in script and 'validate_vep(' in script
    assert 'reference-bundle:vep-cache' not in script
    assert 'systemctl restart openngs openngs-research\n' in script
    assert "if active['n']:raise RuntimeError" in script
    assert 'systemctl restart openngs-migration' not in script
