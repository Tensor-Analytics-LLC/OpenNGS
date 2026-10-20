import copy
import hashlib
import io
import json
from pathlib import Path
import tarfile
import zipfile

import pytest

from backend.format_contract import ASSERTIONS,BASE_IMAGE,FAMILY,ISOLATION,SOURCE_FILES,TOOLS,VERSION
from backend.runtime_catalog import validate_format_release
from scripts import build_portable_formats as builder,finalize_portable_formats as finalizer
from scripts.qualify_portable_formats import fixture_files,qualify


def release():
    manifest=[{'path':p,'sha256':hashlib.sha256(Path(p).read_bytes()).hexdigest(),'bytes':Path(p).stat().st_size} for p in SOURCE_FILES]
    verification={'actual_output_files':8,'full_vcf_records':6,'breakend_orientations':4,'bed12_exons':2,'dictionary_sequence_checksums_verified':True}
    formats={'vcf.bcf':'BCF','bcf.vcf':'VCF','contig.rename':'VCF','contig.rename.bed':'BED',
             'gtf.bed':'BED','bed.gtf':'GTF','intervals.bed':'BED','reference.dictionary':'REFERENCE_DICTIONARY'}
    record={'family':FAMILY,'version':VERSION,'base_image':BASE_IMAGE,'image':BASE_IMAGE.rsplit('@',1)[0]+'@sha256:'+'a'*64,
        'tools':sorted(TOOLS),'source_sha256':'b'*64,'source_files':manifest,'build_id':'openngs-demo-build:test',
        'compute':'BUILD_GENERAL1_MEDIUM','maximum_build_minutes':10,'maximum_build_usd':.10,'new_layer_compressed_bytes':1024,
        'qualification':{'schema':1,'family':FAMILY,'version':VERSION,'isolation':ISOLATION.copy(),
            'implementation_sha256':{r['path']:r['sha256'] for r in manifest if r['path'].startswith('backend/')},
            'versions':{'python':'test','pysam':'test','samtools':'test'},'artifact_verification':verification,
            'cases':[{'tool':tool,'status':'passed','assertions':copy.deepcopy(ASSERTIONS[tool]),'outputs':[
                {'name':tool+'/output','format':formats[tool],'bytes':10,'sha256':'c'*64}]} for tool in sorted(TOOLS)]},
        'artifact_verification':verification,'evidence':{name:{'bucket':'private','key':'operations/builds/formats-test/'+name,
            'version_id':'immutable','bytes':10,'sha256':'b'*64 if name=='source' else 'c'*64} for name in ['source','qualification','outputs']}}
    return record


def test_formats_release_does_not_reuse_inherited_execution_proofs():
    assert validate_format_release(release())['tools']==sorted(TOOLS)
    mutations=[lambda r:r.update(image=BASE_IMAGE),lambda r:r.update(base_image='mutable:latest'),
        lambda r:r['tools'].append('sam.bam'),lambda r:r['qualification']['cases'].pop(),
        lambda r:r['qualification']['cases'][0].update(status='failed'),
        lambda r:r['qualification']['cases'][0]['assertions'].clear(),
        lambda r:r['qualification']['implementation_sha256'].clear(),
        lambda r:r['qualification']['isolation'].update(root_write_denied=False),
        lambda r:r.pop('artifact_verification'),lambda r:r['evidence']['outputs'].update(version_id='null'),
        lambda r:r.update(maximum_build_minutes=11),lambda r:r.update(new_layer_compressed_bytes=17*1024**2)]
    for mutate in mutations:
        value=release();mutate(value)
        with pytest.raises(ValueError):validate_format_release(value)


def test_real_eight_control_outputs_are_independently_verified(tmp_path):
    fixture_files(tmp_path/'fixtures');proof=qualify(tmp_path/'work',tmp_path/'fixtures',check_isolation=False)
    assert {case['tool'] for case in proof['cases']}==TOOLS
    assert all(case['status']=='passed' and case['assertions']==ASSERTIONS[case['tool']] for case in proof['cases'])
    raw=io.BytesIO()
    with tarfile.open(fileobj=raw,mode='w:gz') as archive:
        for path in sorted((tmp_path/'work').rglob('*')):
            if path.is_file():archive.add(path,arcname='./'+str(path.relative_to(tmp_path/'work')),recursive=False)
    assert finalizer.verify_outputs(proof,raw.getvalue())==proof['artifact_verification']
    modified=copy.deepcopy(proof);modified['cases'][0]['outputs'][0]['sha256']='f'*64
    with pytest.raises(ValueError,match='SHA-256'):finalizer.verify_outputs(modified,raw.getvalue())
    missing=copy.deepcopy(proof);missing['cases'].pop()
    with pytest.raises(ValueError,match='eight'):finalizer.verify_outputs(missing,raw.getvalue())


def test_artifact_finalizer_rechecks_content_even_if_digest_is_rewritten(tmp_path):
    fixture_files(tmp_path/'fixtures');proof=qualify(tmp_path/'work',tmp_path/'fixtures',check_isolation=False)
    target=tmp_path/'work/intervals.bed/regions.bed';target.write_text('1\t1\t2\n')
    for case in proof['cases']:
        if case['tool']=='intervals.bed':case['outputs'][0].update(bytes=target.stat().st_size,sha256=hashlib.sha256(target.read_bytes()).hexdigest())
    raw=io.BytesIO()
    with tarfile.open(fileobj=raw,mode='w:gz') as archive:
        for path in (tmp_path/'work').rglob('*'):
            if path.is_file():archive.add(path,arcname=str(path.relative_to(tmp_path/'work')),recursive=False)
    with pytest.raises(ValueError,match='semantic'):finalizer.verify_outputs(proof,raw.getvalue())


def test_source_package_is_small_deterministic_and_uses_no_downloads(tmp_path):
    first,record=builder.package(tmp_path/'first');second,again=builder.package(tmp_path/'second')
    assert first.read_bytes()==second.read_bytes() and record==again and record['archive_bytes']<1024**2
    finalizer.verify_source(record,first.read_bytes())
    with zipfile.ZipFile(first) as archive:
        assert set(archive.namelist())==set(SOURCE_FILES)|{'source-manifest.json','buildspec.yml'}|{'fixtures/'+r['name'] for r in record['fixture_files']}
    spec=builder.spec_bytes().decode()
    assert '--network none --read-only --cap-drop ALL' in spec and '--user 1000:1000' in spec
    assert '--security-opt no-new-privileges' in spec and 'fixtures:/inputs/fixtures:ro' in spec
    assert 'curl ' not in spec and 'pip install' not in spec
    docker=Path('infra/portable-formats.Dockerfile').read_text()
    assert 'FROM ${FORMAT_BASE_IMAGE}' in docker and '\nRUN ' not in docker


def test_builder_refuses_concurrent_project_work():
    class CB:
        def list_builds_for_project(self,**kw):return {'ids':['one']}
        def batch_get_builds(self,**kw):return {'builds':[{'buildStatus':'IN_PROGRESS'}]}
    with pytest.raises(ValueError,match='busy'):builder.require_idle(CB())


def test_new_image_storage_bound_requires_exact_ordered_parent_layers():
    base=[{'digest':'sha256:a','size':900},{'digest':'sha256:b','size':800}]
    assert finalizer.added_layer_bytes(base,base+[{'digest':'sha256:c','size':100}])==100
    for child in [base,list(reversed(base))+[{'size':1}],base+[{'size':17*1024**2}],base+[{'size':0}]]:
        with pytest.raises(ValueError):finalizer.added_layer_bytes(base,child)


def test_evidence_archive_rejects_links_and_parent_traversal():
    proof=release()['qualification']
    for name,kind in [('../escape',tarfile.REGTYPE),('link',tarfile.SYMTYPE),('hard',tarfile.LNKTYPE)]:
        raw=io.BytesIO()
        with tarfile.open(fileobj=raw,mode='w:gz') as archive:
            member=tarfile.TarInfo(name);member.type=kind;member.linkname='other';archive.addfile(member)
        with pytest.raises(ValueError,match='Unsafe'):finalizer.verify_outputs(proof,raw.getvalue())


def test_published_pro_format_proof_never_approves_the_unchanged_explore_worker(monkeypatch):
    from backend import catalogue_view,config,demo,registry
    monkeypatch.setattr(config,'CLOUD',True)
    for tool in TOOLS:
        assert tool not in demo.TOOLS
        node=registry.BASE[tool]
        evidence={'runtime_family':FAMILY,'status':'implemented-verified','sample_execution':{'scope':'bounded format controls'},
                  'hosted_runtime_image':release()['image']}
        value=catalogue_view._availability(node,evidence,{},True,set(),{'dependencies':[]})
        assert value['state']!='Ready' and value['tier']=='Research Pro'
        blockers={row['code']:row['message'] for row in value['blockers']}
        assert 'EXPLORE_FORMATS_UNVERIFIED' in blockers and 'separate cloud execution check' in blockers['EXPLORE_FORMATS_UNVERIFIED']


def test_narrow_concurrency_allowance_checks_actual_source_quota_and_privilege(tmp_path,monkeypatch):
    import base64
    role='arn:aws:iam::509505638328:role/existing';source={'bucket':'private','key':'operations/builds/exact-vep-test/source.zip',
        'version_id':'exact-version','bytes':40,'sha256':'a'*64,'buildspec_sha256':hashlib.sha256(b'exact-spec').hexdigest()}
    record={'build_id':'openngs-demo-build:acquisition','reservation':'existing-hold','privileged':False,
            'maximum_build_minutes':45,'service_role':role,'source':source}
    folder=tmp_path/'evidence/deployment';folder.mkdir(parents=True)
    (folder/'vep-direct-acquisition-build.json').write_text(json.dumps(record));monkeypatch.chdir(tmp_path)
    observed={'id':record['build_id'],'buildStatus':'IN_PROGRESS','timeoutInMinutes':45,'serviceRole':role,
        'sourceVersion':source['version_id'],'source':{'type':'S3','location':'private/'+source['key'],'buildspec':'exact-spec'},
        'environment':{'privilegedMode':False,'computeType':'BUILD_GENERAL1_MEDIUM'}}
    class Provider:
        quota=60;active=[observed]
        def client(self,name):return self
        def list_builds_for_project(self,**kw):return {'ids':['acquisition']}
        def batch_get_builds(self,**kw):return {'builds':self.active}
        def get_service_quota(self,**kw):return {'Quota':{'Value':self.quota}}
        def head_object(self,**kw):
            assert kw['VersionId']=='exact-version'
            return {'VersionId':'exact-version','ContentLength':40,'Metadata':{'sha256':'a'*64},
                    'ChecksumSHA256':base64.b64encode(bytes.fromhex('a'*64)).decode()}
    provider=Provider();project={'name':'openngs-demo-build','serviceRole':role}
    result=builder.allow_exact_acquisition(provider,project,provider,record['build_id'],'existing-hold')
    assert result['maximum_project_builds']==2 and result['regional_medium_quota']==60
    provider.quota=1
    with pytest.raises(ValueError,match='quota'):builder.allow_exact_acquisition(provider,project,provider,record['build_id'],'existing-hold')
    provider.quota=60;observed['environment']['privilegedMode']=True
    with pytest.raises(ValueError,match='nonprivileged'):builder.allow_exact_acquisition(provider,project,provider,record['build_id'],'existing-hold')
    observed['environment']['privilegedMode']=False;provider.active=[observed,{**observed,'id':'unreviewed'}]
    with pytest.raises(ValueError,match='one explicitly'):builder.allow_exact_acquisition(provider,project,provider,record['build_id'],'existing-hold')


def launch_fixture():
    return ({'projectName':'openngs-demo-build','buildspecOverride':'exact-spec','environmentVariablesOverride':[]},
            {'source_version':'version-1','source_sha256':'a'*64,'reservation':'same-funded-hold',
             'bucket':'private','source_key':'operations/builds/formats-test/source.zip'})


def test_launch_intent_precedes_start_and_reconciles_ambiguous_response(tmp_path):
    path=tmp_path/'intent.json';request,record=launch_fixture()
    class CB:
        calls=0;observed=None
        def start_build(self,**kw):
            assert path.exists() and json.loads(path.read_text())['source_version']=='version-1'
            assert len(kw['idempotencyToken'])==64
            self.calls+=1
            self.observed={'id':'openngs-demo-build:once','sourceVersion':'version-1','timeoutInMinutes':10,
                'source':{'type':'S3','location':record['bucket']+'/'+record['source_key'],'buildspec':'exact-spec'},
                'environment':{'computeType':'BUILD_GENERAL1_MEDIUM','environmentVariables':kw['environmentVariablesOverride']}}
            raise ConnectionError('ambiguous provider response')
        def list_builds_for_project(self,**kw):return {'ids':['one']}
        def batch_get_builds(self,**kw):return {'builds':[self.observed]}
    cb=CB()
    with pytest.raises(ConnectionError):builder.launch_once(cb,request,record,path,now=lambda:100)
    assert json.loads(path.read_text())['state']=='launch-outcome-unknown'
    result=builder.launch_once(cb,request,record,path,now=lambda:800)
    assert cb.calls==1 and result['build_id']=='openngs-demo-build:once' and result['reconciled'] is True
    assert builder.launch_once(cb,request,record,path,now=lambda:1000)==result and cb.calls==1
    with pytest.raises(ValueError,match='different persistent'):builder.launch_once(cb,request,{**record,'reservation':'different'},path)


def test_unresolved_old_launch_never_blindly_starts_again(tmp_path):
    request,record=launch_fixture();path=tmp_path/'intent.json'
    class CB:
        calls=0;tokens=[]
        def start_build(self,**kw):self.calls+=1;self.tokens.append(kw['idempotencyToken']);raise ConnectionError('unknown')
        def list_builds_for_project(self,**kw):return {'ids':[]}
    cb=CB()
    with pytest.raises(ConnectionError):builder.launch_once(cb,request,record,path,now=lambda:100)
    with pytest.raises(ConnectionError):builder.launch_once(cb,request,record,path,now=lambda:200)
    assert cb.calls==2 and len(set(cb.tokens))==1
    with pytest.raises(ValueError,match='safe token retry window'):builder.launch_once(cb,request,record,path,now=lambda:341)
    assert cb.calls==2


def test_local_launch_controller_lock_prevents_concurrent_operator(tmp_path):
    path=tmp_path/'lock'
    with builder.operator_lock(path):
        with pytest.raises(ValueError,match='Another local'): 
            with builder.operator_lock(path):pass
