import copy
import json
from pathlib import Path
import pytest
from backend import compiler,registry
from backend.secondary_runtime import ENVIRONMENTS,TOOLS,validate_release
from backend.runtime_catalog import PORTABLE_TOOLS,validate_release as validate_extension
from tests.test_runtime_catalog import release

def secondary():
    return {'family':'secondary-cpu-v1','image':'123456789012.dkr.ecr.us-east-1.amazonaws.com/openngs-demo@sha256:'+'1'*64,
        'environments':ENVIRONMENTS,'source_sha256':'2'*64,'tools':sorted(TOOLS),
        'qualification':{'environments':{name:{'status':'installed'} for name in ENVIRONMENTS},
            'locks':{name:'3'*64 for name in ENVIRONMENTS},
            'cases':[{'tool':tool,'status':'passed','assertions':{'output_verified':True},'outputs':[{'name':'scientific-output'}]} for tool in TOOLS]}}

def test_secondary_requires_actual_outputs_and_frozen_transitive_environment():
    assert validate_release(secondary())['tools']==sorted(TOOLS)
    for change in [lambda r:r['qualification']['cases'][0].pop('assertions'),
                   lambda r:r['qualification']['environments']['analysis'].update(status='unavailable'),
                   lambda r:r['qualification']['locks'].pop('happy'),
                   lambda r:r['tools'].append('deepvariant')]:
        bad=secondary();change(bad)
        with pytest.raises(ValueError):validate_release(bad)

def test_optional_environment_recipe_is_exactly_reviewed():
    assert json.loads(Path('infra/secondary-environments.json').read_text())==ENVIRONMENTS

def test_legacy_workflow_does_not_use_core_python_or_silently_ignore_interval(tmp_path):
    inputs={'alignment':[{'path':'/tmp/reads.bam','format':'INDEXED_BAM'}],'reference':[{'path':'/tmp/ref.fa','format':'FASTA'}]}
    for tool in ['manta','strelka2']:
        plan=compiler.compile_tool(registry.BASE[tool],{'params':{'exome':True}},inputs,tmp_path)
        assert plan['steps'][1]['argv'][0]=='/opt/legacy/bin/python'
        assert '--exome' in plan['steps'][0]['argv']
        with pytest.raises(ValueError,match='interval strings'):compiler.compile_tool(registry.BASE[tool],{'params':{'interval':'chr1:1-100'}},inputs,tmp_path)

def test_scanpy_uses_qualified_interpreter_and_declared_parameters(tmp_path):
    plan=compiler.compile_tool(registry.BASE['scanpy'],{'params':{'n_neighbors':20}}, {'counts':[{'path':'/tmp/counts.h5ad','format':'H5AD'}]},tmp_path)
    argv=plan['steps'][0]['argv'];assert argv[:4]==['/opt/analysis/bin/python','-m','backend.secondary_tools','scanpy']
    assert argv[argv.index('--neighbors')+1]=='20'

def test_portable_refresh_needs_every_operator_outcome_bound_to_exact_image():
    record=release();record['portable_tools']=sorted(PORTABLE_TOOLS)
    record['portable_qualification']={'image':record['image'],'source_sha256':'4'*64,'cases':[
        {'tool':tool,'status':'passed','outputs':[{'name':'result'}],'assertions':{'round_trip':True}} for tool in PORTABLE_TOOLS]}
    assert validate_extension(record)['portable_tools']==sorted(PORTABLE_TOOLS)
    record['portable_qualification']['image']='different'
    with pytest.raises(ValueError,match='exact image'):validate_extension(record)

def test_named_resources_bind_existing_families_without_inventing_readiness():
    families={'dbnsfp','alphamissense','spliceai','cadd','revel','exomiser','hpo','ensembl','mane','clingen','vep'}
    for family in families:
        node=registry.BASE['resource.'+family]
        assert node['asset_family']==family and node['builtin']
        assert node['outputs'][0]['type']=='DATABASE'
        assert any(p['key']=='asset_id' for p in node['parameters'])
        assert 'does not provision' in node['rights_note']
    assert len([n for n in registry.BASE.values() if not n.get('hidden')])==138

def test_core_runtime_versions_cover_every_published_route_and_differ_from_registry_reference():
    from backend.runtime_catalog import CORE_PACKAGE_TOOLS,CORE_PACKAGES
    from backend.provisioning import CORE_TOOLS
    assert set(CORE_PACKAGE_TOOLS)==CORE_TOOLS
    assert CORE_PACKAGES['picard']=='3.4.0'
    assert CORE_PACKAGES['gatk4']=='4.6.2.0'
    recipe=Path('infra/research.Dockerfile').read_text()
    assert all(name+'='+version in recipe for name,version in CORE_PACKAGES.items())
