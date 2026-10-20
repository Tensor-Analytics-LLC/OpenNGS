"""Publication wiring fixtures exercise gates; they are not actual cloud evidence."""
import copy
import json
import sys

import pytest

from backend import capabilities,db,provisioning,runtime_catalog
from backend.format_contract import ASSERTIONS,BASE_IMAGE,FAMILY,ISOLATION,SCOPE,SOURCE_FILES,TOOLS,VERSION
from infra.research_stack import template


def release():
    sources=[{'path':path,'sha256':'a'*64,'bytes':10} for path in SOURCE_FILES]
    formats={'vcf.bcf':'BCF','bcf.vcf':'VCF','contig.rename':'VCF','contig.rename.bed':'BED',
             'gtf.bed':'BED','bed.gtf':'GTF','intervals.bed':'BED','reference.dictionary':'REFERENCE_DICTIONARY'}
    verification={'actual_output_files':8,'full_vcf_records':6,'breakend_orientations':4,
                  'bed12_exons':2,'dictionary_sequence_checksums_verified':True}
    return {'family':FAMILY,'version':VERSION,'image':BASE_IMAGE.split('@')[0]+'@sha256:'+'b'*64,
            'base_image':BASE_IMAGE,'tools':sorted(TOOLS),'source_sha256':'c'*64,'source_files':sources,
            'build_id':'integration-validator-fixture-only','validation_scope':SCOPE,
            'maximum_build_minutes':10,'maximum_build_usd':.10,'compute':'BUILD_GENERAL1_MEDIUM',
            'new_layer_compressed_bytes':1024,'artifact_verification':verification,
            'evidence':{name:{'bucket':'fixture-only','key':'operations/builds/formats-fixture/'+name,
                'version_id':'immutable-fixture-version','bytes':100,'sha256':'c'*64 if name=='source' else 'd'*64}
                for name in ['source','qualification','outputs']},
            'qualification':{'schema':1,'family':FAMILY,'version':VERSION,'isolation':copy.deepcopy(ISOLATION),
                'versions':{'python':'fixture-only','pysam':'fixture-only','samtools':'fixture-only'},
                'implementation_sha256':{row['path']:row['sha256'] for row in sources if row['path'].startswith('backend/')},
                'artifact_verification':copy.deepcopy(verification),
                'cases':[{'tool':tool,'status':'passed','seconds':.01,'assertions':copy.deepcopy(ASSERTIONS[tool]),
                          'outputs':[{'name':tool+'/fixture-output','format':formats[tool],'bytes':10,'sha256':'e'*64}]}
                         for tool in sorted(TOOLS)]}}


def register(value):
    with db.tx() as connection:
        connection.execute('INSERT INTO controls VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                           ('runtime-release:'+FAMILY,db.encode(value)))


def stack_outputs(proof=None):
    proof=proof or release()
    return {'FormatsImage':proof['image'],'FormatsDefinition':'reviewed-format-definition',
            'FormatsTools':json.dumps(proof['tools']),'FormatsSource':proof['source_sha256'],
            'FormatsBaseImage':proof['base_image']}


def test_format_routes_require_a_valid_publication_and_exact_stack_identities(client):
    proof=release();outputs=stack_outputs(proof)
    assert runtime_catalog.validate_format_release(proof)==proof
    assert provisioning.format_routes({})=={}
    with pytest.raises(ValueError,match='exact published image'):
        provisioning.format_routes(outputs)
    register(proof)
    routes=provisioning.format_routes(outputs)
    assert set(routes)==TOOLS
    for route in routes.values():
        assert route=={'image':proof['image'],'definition':'reviewed-format-definition',
                       'family':FAMILY,'source_grants_version':1,'source_sha256':proof['source_sha256'],
                       'base_image':BASE_IMAGE,'version':VERSION}
    for field in outputs:
        bad=dict(outputs);bad.pop(field)
        with pytest.raises(ValueError):provisioning.format_routes(bad)
    for field,value in [('FormatsImage',BASE_IMAGE),('FormatsSource','f'*64),
                        ('FormatsBaseImage',proof['image']),('FormatsTools','[]'),
                        ('FormatsTools','["intervals.bed","unknown"]'),('FormatsTools','null'),
                        ('FormatsTools','malformed JSON'),('FormatsDefinition','')]:
        with pytest.raises(ValueError):provisioning.format_routes({**outputs,field:value})


def test_invalid_format_publication_never_becomes_a_route(client):
    proof=release();proof['qualification']['cases'].pop();register(proof)
    assert runtime_catalog.published_formats() is None
    with pytest.raises(ValueError):provisioning.format_routes(stack_outputs())


def test_optional_format_definition_preserves_existing_roles_capacity_and_routes(monkeypatch):
    args=('subnet','security','execution','registry/core@sha256:'+'f'*64,
          'repo','shared','key','boundary','https://example.test','topic')
    extension={'image':BASE_IMAGE,'source_sha256':'a'*64,'tools':[],
               'portable_tools':sorted(runtime_catalog.PORTABLE_TOOLS)}
    monkeypatch.setattr(runtime_catalog,'validate_release',lambda value:value)
    before=template(*args,extension=extension)
    after=template(*args,extension=extension,formats=release())
    assert set(after['Resources'])-set(before['Resources'])=={'FormatsDefinition'}
    assert all(after['Resources'][key]==value for key,value in before['Resources'].items())
    props=after['Resources']['FormatsDefinition']['Properties']
    core=after['Resources']['Definition']['Properties']
    assert props['Tags']['NGSRuntimeFamily']==FAMILY
    container=props['ContainerProperties']
    assert container['Image']==release()['image']
    for key in set(core['ContainerProperties'])-{'Image'}:
        assert container[key]==core['ContainerProperties'][key]
    assert container['User']=='1000:1000' and container['ReadonlyRootFilesystem'] is True
    assert container['EphemeralStorage']=={'SizeInGiB':200}
    assert container['Command']==['python','/opt/studio/cloud_worker.py']
    assert after['Outputs']['FormatsBaseImage']['Value']==BASE_IMAGE
    assert after['Outputs']['FormatsSource']['Value']==release()['source_sha256']
    assert json.loads(after['Outputs']['FormatsTools']['Value'])==sorted(TOOLS)
    assert after['Outputs']['FormatsDefinition']['Value']=={'Ref':'FormatsDefinition'}
    bad=release();bad['tools'].pop()
    with pytest.raises(ValueError):template(*args,formats=bad)


def test_tenant_reconciliation_replaces_only_overlapping_interval_route(client,monkeypatch):
    register(release())
    outputs={'Bucket':'tenant-bucket','Queue':'tenant-queue','Definition':'core-definition','Image':'core-image',
             'ScratchMiB':'184320','ExtendedImage':BASE_IMAGE,'ExtendedDefinition':'old-extended-definition',
             'ExtendedTools':'[]','PortableTools':json.dumps(sorted(runtime_catalog.PORTABLE_TOOLS)),**stack_outputs()}
    grant={'user_id':'u_'+'a'*32,'version':1,'actor':'admin-fixture','body':'{}'}
    original_query=db.query
    def query(sql,*args,**kwargs):
        if sql.startswith('SELECT * FROM research_allowances'):return [grant]
        return original_query(sql,*args,**kwargs)
    class Provider:
        def client(self,name):
            assert name=='cloudformation';return self
        def describe_stacks(self,**kwargs):
            return {'Stacks':[{'StackStatus':'UPDATE_COMPLETE','Outputs':[
                {'OutputKey':key,'OutputValue':value} for key,value in outputs.items()]}]}
    saved=[]
    monkeypatch.setenv('NGS_RESEARCH_TEMPLATE_URL','immutable-template')
    monkeypatch.setenv('NGS_RESEARCH_CFN_ROLE','fixed-role')
    monkeypatch.setattr(provisioning,'aws_session',lambda:Provider())
    monkeypatch.setattr(provisioning,'profile',lambda uid:{'state':'Provisioning','grant_version':1})
    monkeypatch.setattr(provisioning,'save',lambda uid,value:saved.append(value))
    monkeypatch.setattr(provisioning,'ensure_curated',lambda *args:None)
    monkeypatch.setattr(provisioning.budget,'reserve_storage_capacity',lambda *args:None)
    monkeypatch.setattr(db,'query',query)
    monkeypatch.setattr(db,'audit',lambda *args:None)
    assert provisioning.once() is True
    routes=saved[0]['runtime_profiles']
    assert saved[0]['state']=='Ready' and saved[0]['queue']=='tenant-queue'
    assert set(runtime_catalog.PORTABLE_TOOLS)&TOOLS=={'intervals.bed'}
    for tool in runtime_catalog.PORTABLE_TOOLS-TOOLS:
        assert routes[tool]['image']==BASE_IMAGE and routes[tool]['definition']=='old-extended-definition'
        assert routes[tool]['family']=='portable-refresh-v1'
    assert {tool for tool,route in routes.items() if route['family']==FAMILY}==TOOLS
    assert routes['intervals.bed']['image']==release()['image']
    assert routes['results.prepare']['image']==BASE_IMAGE


def test_capabilities_promote_only_eight_exact_published_format_proofs(client):
    before={row['id']:row for row in capabilities.matrix()['capabilities']}
    register(release())
    after={row['id']:row for row in capabilities.matrix()['capabilities']}
    assert {tool for tool in after if after[tool]!=before[tool]}==TOOLS
    for tool in TOOLS:
        row=after[tool]
        assert row['status']=='implemented-verified' and row['runtime_family']==FAMILY
        assert row['hosted_runtime_image']==release()['image'] and row['runtime_blocker'] is None
        assert row['validation_scope']==SCOPE
        assert row['sample_execution']['assertions']==ASSERTIONS[tool]
        assert row['sample_execution']['source_sha256']==release()['source_sha256']
        assert row['sample_execution']['image']==release()['image']
        assert row['runtime_regression']==row['sample_execution']
        assert row['runtime_provenance']['published_image']==release()['image']
        assert row['migrated_data_runtime']['source_grants_version']==1
    invalid=release();invalid['qualification']['isolation']['root_write_denied']=False;register(invalid)
    assert capabilities.matrix()['capabilities']==list(before.values())


def test_format_capability_overlay_replaces_old_interval_proof_but_preserves_other_portable_rows(client,monkeypatch):
    old={'image':BASE_IMAGE,'tools':[],'qualification':{'cases':[]},
         'portable_tools':['intervals.bed','vcf.stats'],
         'portable_qualification':{'build_id':'old-portable-fixture','scope':'original portable controls',
             'cases':[{'tool':tool,'assertions':{'original_case':tool},'outputs':[], 'seconds':.02}
                      for tool in ['intervals.bed','vcf.stats']]}}
    monkeypatch.setattr(runtime_catalog,'published',lambda:old)
    before={row['id']:row for row in capabilities.matrix()['capabilities']}
    assert before['intervals.bed']['sample_execution']['build_id']=='old-portable-fixture'
    register(release())
    after={row['id']:row for row in capabilities.matrix()['capabilities']}
    assert after['intervals.bed']['runtime_family']==FAMILY
    assert after['intervals.bed']['sample_execution']['build_id']==release()['build_id']
    assert after['intervals.bed']['runtime_regression']['assertions']==ASSERTIONS['intervals.bed']
    assert after['vcf.stats']==before['vcf.stats']


def test_publication_loader_ignores_candidate_and_rejects_incomplete_proof(tmp_path,monkeypatch):
    from scripts.publish_research_capacity import optional_formats_release
    monkeypatch.chdir(tmp_path)
    folder=tmp_path/'evidence/deployment';folder.mkdir(parents=True)
    (folder/'portable-formats-release-candidate.json').write_text(json.dumps(release()))
    assert optional_formats_release() is None
    target=folder/'portable-formats-release.json';target.write_text(json.dumps(release()))
    assert optional_formats_release()==release()
    invalid=release();invalid['qualification']['cases'].pop();target.write_text(json.dumps(invalid))
    with pytest.raises(ValueError):optional_formats_release()


def test_service_reconciliation_validates_formats_after_active_guard_and_preserves_restarts(tmp_path,monkeypatch):
    from scripts import configure_services
    captured=[]
    research={'template_url':'immutable-template','cfn_role':'cfn-role','budget_topic':'topic',
              'runtime_image':'core-image','runtime_build':'core-build','formats':release()}
    folder=tmp_path/'evidence/deployment';folder.mkdir(parents=True)
    (folder/'research-capacity.json').write_text(json.dumps(research))
    class Provider:
        def client(self,*args,**kwargs):return self
        def describe_stacks(self,StackName):
            outputs={'HostId':'host'} if StackName=='openngs-lean' else {'QueueURL':'q','WarningTopic':'w','CriticalTopic':'c'}
            return {'Stacks':[{'Outputs':[{'OutputKey':key,'OutputValue':value} for key,value in outputs.items()]}]}
        def describe_instances(self,**kwargs):
            return {'Reservations':[{'Instances':[{'VpcId':'vpc','SecurityGroups':[]}]}]}
        def describe_security_groups(self,**kwargs):
            return {'SecurityGroups':[{'GroupName':'CloudFront-VPCOrigins-Service-SG','GroupId':'sg'}]}
        def get_caller_identity(self):return {'Account':'509505638328'}
        def send_command(self,**kwargs):captured.append(kwargs);return {'Command':{'CommandId':'fixture-command'}}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(configure_services,'aws_session',lambda:Provider())
    monkeypatch.setattr(sys,'argv',['configure_services.py','--restart-services','openngs','openngs-research','openngs'])
    configure_services.main()
    script=captured[0]['Parameters']['commands'][0]
    assert 'formats=validate_format_release(' in script and 'runtime-release:'+FAMILY in script
    assert script.index("if active['n']:raise RuntimeError")<script.index('formats=validate_format_release(')
    assert script.index('formats=validate_format_release(')<script.index('runtime-release:'+FAMILY)
    assert 'systemctl restart openngs openngs-research\n' in script
    assert 'systemctl restart openngs-migration' not in script
    assert 'reference-bundle:' not in script
