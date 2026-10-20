import copy
from pathlib import Path
import pytest
from backend import compiler,registry
from backend.runtime_catalog import EXTENDED_PACKAGES,validate_release
from infra.research_stack import template

def release():
    return {'family':'extended-cpu-v1','image':'123456789012.dkr.ecr.us-east-1.amazonaws.com/openngs-demo@sha256:'+'a'*64,
        'packages':EXTENDED_PACKAGES,'source_sha256':'b'*64,'tools':['bowtie2'],
        'qualification':{'cases':[{'tool':'bowtie2','status':'passed','outputs':[{'name':'aligned.sam','bytes':200}],
                                  'assertions':{'mapped_reads':20}}]}}

def test_publication_requires_executed_outputs_not_installed_tools():
    record=release()
    assert validate_release(record)['tools']==['bowtie2']
    for mutate in [lambda r:r['tools'].append('salmon'),
                   lambda r:r['qualification']['cases'][0].pop('assertions'),
                   lambda r:r.update(image='quay.io/example/mutable:latest'),
                   lambda r:r['qualification']['cases'][0].update(status='failed')]:
        invalid=copy.deepcopy(record);mutate(invalid)
        with pytest.raises(ValueError):validate_release(invalid)

def test_extension_keeps_tenant_boundary_and_core_image():
    core='123456789012.dkr.ecr.us-east-1.amazonaws.com/openngs-demo@sha256:'+'c'*64
    result=template('subnet','worker-sg','execution-role',core,'repo','cache','key','boundary','https://example.test','topic',release())
    resources=result['Resources']
    original=resources['Definition']['Properties']['ContainerProperties']
    extra=resources['ExtendedDefinition']['Properties']['ContainerProperties']
    assert original['Image']==core and extra['Image']==release()['image']
    assert original['JobRoleArn']==extra['JobRoleArn']=={'Fn::GetAtt':['TaskRole','Arn']}
    assert extra['ReadonlyRootFilesystem'] and extra['NetworkConfiguration']['AssignPublicIp']=='DISABLED'
    assert 'Compute' in resources and len([x for x in resources.values() if x['Type']=='AWS::Batch::ComputeEnvironment'])==1

def fixture_inputs():
    return {'reads':[{'path':'/tmp/r1.fastq.gz','format':'FASTQ_PAIR','metadata':{'mate':'/tmp/r2.fastq.gz'}}],
            'reference':[{'path':'/tmp/transcripts.fa','format':'FASTA','metadata':{}}]}

def test_salmon_builds_explicit_fasta_or_requires_complete_index(tmp_path):
    data=fixture_inputs();spec=registry.BASE['salmon']
    plan=compiler.compile_tool(spec,{'params':{},'resources':{'threads':2}},data,tmp_path)
    assert plan['steps'][0]['argv'][:2]==['salmon','index']
    assert plan['steps'][1]['argv'][plan['steps'][1]['argv'].index('-i')+1]=='/work/salmon-index'
    data['reference'][0]['format']='INDEX'
    with pytest.raises(ValueError,match='complete index'):
        compiler.compile_tool(spec,{'params':{}},data,tmp_path)
    data['reference'][0]['metadata']['index_paths']=['/tmp/hash.bin']
    plan=compiler.compile_tool(spec,{'params':{}},data,tmp_path)
    assert len(plan['steps'])==1 and plan['steps'][0]['argv'][3]=='/inputs/reference/0'
    with pytest.raises(ValueError,match='odd'):
        compiler.compile_tool(spec,{'params':{'index_kmer':20}},fixture_inputs(),tmp_path)

def test_star_creates_index_on_worker_and_fastqc_exposes_machine_report(tmp_path):
    plan=compiler.compile_tool(registry.BASE['star'],{'params':{}},fixture_inputs(),tmp_path)
    assert plan['steps'][0]['argv']==['mkdir','-p','/work/star-index']
    assert not (tmp_path/'star-index').exists()
    qc=compiler.compile_tool(registry.BASE['fastqc'],{'params':{}},fixture_inputs(),tmp_path)
    assert {x['file'] for x in qc['outputs']['report']}=={'*_fastqc.html','*_fastqc.zip'}

def test_recipe_and_manifest_package_pins_agree():
    from backend.config import ROOT
    pairs=dict(line.split('=') for line in (ROOT/'infra/extended-packages.txt').read_text().splitlines() if line.strip())
    assert pairs==EXTENDED_PACKAGES

def test_native_alignment_and_trimming_flags_match_tool_contracts(tmp_path):
    bowtie=compiler.compile_tool(registry.BASE['bowtie2'],{'params':{}},fixture_inputs(),tmp_path)
    assert bowtie['steps'][0]['argv'][1]=='--threads'
    trim=compiler.compile_tool(registry.BASE['trimmomatic'],{'params':{}},fixture_inputs(),tmp_path)
    assert '-phred33' in trim['steps'][0]['argv']
    legacy=compiler.compile_tool(registry.BASE['trimmomatic'],{'params':{'quality_encoding':'phred64'}},fixture_inputs(),tmp_path)
    assert '-phred64' in legacy['steps'][0]['argv']
    with pytest.raises(ValueError):
        compiler.compile_tool(registry.BASE['trimmomatic'],{'params':{'quality_encoding':'automatic'}},fixture_inputs(),tmp_path)

def test_core_refresh_requires_all_regression_outcomes_and_exact_digest():
    from backend.provisioning import CORE_TOOLS
    record=release()
    record['core_qualification']={'image':record['image'],'source_sha256':'d'*64,'cases':[
        {'tool':tool,'status':'passed','outputs':[{'name':'output.vcf'}],'assertions':{'actual_records':1}}
        for tool in CORE_TOOLS]}
    record['refreshed_core_tools']=sorted(CORE_TOOLS)
    assert validate_release(record)['refreshed_core_tools']==sorted(CORE_TOOLS)
    record['core_qualification']['image']='different'
    with pytest.raises(ValueError,match='exact worker image'):validate_release(record)

def test_adjunct_routes_cannot_exceed_passing_real_command_cases():
    from backend.runtime_catalog import ADJUNCT_TOOLS
    record=release()
    record['adjunct_qualification']={'image':record['image'],'source_sha256':'e'*64,'cases':[
        {'tool':tool,'status':'passed','outputs':[{'name':'output.vcf'}],'assertions':{'records':2}}
        for tool in ADJUNCT_TOOLS]}
    record['adjunct_tools']=sorted(ADJUNCT_TOOLS)
    assert validate_release(record)['adjunct_tools']==sorted(ADJUNCT_TOOLS)
    record['adjunct_tools'].append('vep')
    with pytest.raises(ValueError,match='passing command'):validate_release(record)
