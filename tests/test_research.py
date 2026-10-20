import json,time
import pytest
from backend import db,entitlements,budget,cloud_data,provisioning,research_jobs
from test_entitlements import allowance

def test_provisioned_template_is_private_bounded_and_retains_data():
    from infra.research_stack import template
    t=template('subnet-1','sg-1','arn:execution','registry/core@sha256:'+'a'*64,'arn:repo','shared','arn:kms','arn:boundary','https://example.test','arn:topic')
    r=t['Resources'];props=r['Definition']['Properties']['ContainerProperties']
    assert props['ReadonlyRootFilesystem'] and props['NetworkConfiguration']['AssignPublicIp']=='DISABLED'
    assert props['EphemeralStorage']['SizeInGiB']==200
    assert r['Compute']['Properties']['ComputeResources']['Type']=='FARGATE_SPOT'
    assert t['Parameters']['FleetVCPU']['AllowedValues']==[2,4,8,16,32,64]
    assert t['Parameters']['MonthlyBudget']['MaxValue']==500
    assert r['Data']['DeletionPolicy']=='Retain'
    assert r['TaskRole']['Properties']['PermissionsBoundary']=='arn:boundary'
    assert 's3.us-east-1.amazonaws.com' in json.dumps(r['TaskRole'])
    assert '${StorageId}' in r['Data']['Properties']['BucketName']['Fn::Sub']

def test_storage_capacity_is_reserved_once_without_upload_double_count(client,owner):
    p=entitlements.grant(owner,owner['id'],{'reason':'Approved isolated test capacity','funding_confirmed':True,'allowance':allowance()})
    budget.reserve_storage_capacity(owner['id'],p);budget.reserve_storage_capacity(owner['id'],p)
    rows=db.query("SELECT * FROM cost_reservations WHERE kind='retained-capacity'")
    assert len(rows)==1 and rows[0]['reserved_micro']==230000
    p['max_storage_bytes']+=1024**3
    budget.reserve_storage_capacity(owner['id'],p)
    assert sum(x['reserved_micro'] for x in db.query("SELECT * FROM cost_reservations WHERE kind='retained-capacity'"))==253000

def test_output_storage_reservations_are_atomic_and_released(client,owner):
    with db.tx() as c:
        for i in range(4):assert entitlements.reserve_output(c,owner,'out-'+str(i))==64*1024**2
    with pytest.raises(ValueError):
        with db.tx() as c:entitlements.reserve_output(c,owner,'overflow')
    with db.tx() as c:entitlements.settle(c,'out-0',0)
    with db.tx() as c:assert entitlements.reserve_output(c,owner,'next')==64*1024**2

def test_personal_native_parsing_is_not_allowed_on_cloud_control_plane(client,owner,monkeypatch):
    from backend import config,workbench
    monkeypatch.setattr(config,'CLOUD',True)
    with pytest.raises(ValueError,match='isolated workbench'):
        workbench.source(owner['id'],{'path':'s3://anything/private.bam','metadata':{}})

def test_research_budget_alert_does_not_freeze_explore(client,owner,monkeypatch):
    from backend import billing
    monkeypatch.setenv('NGS_PRO_BUDGET_TOPIC','arn:test:pro')
    entitlements.grant(owner,owner['id'],{'reason':'Approved isolated test capacity','funding_confirmed':True,'allowance':allowance()})
    billing.record_alert('pro-test','arn:test:pro','Budget openngs-pro-'+owner['id']+' reached its threshold')
    assert budget.snapshot()['admission_open']
    with pytest.raises(budget.BudgetFull):
        with db.tx() as c:budget.reserve(c,owner['id'],'new-work','scientific',.01,{})

def test_companions_cannot_bind_another_tenants_dataset(client,owner):
    now=time.time()
    with db.tx() as c:
        c.execute('INSERT INTO datasets VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',('primary',owner['id'],None,'R1.fastq',100,'a'*64,'Ready','tenants/'+owner['id']+'/R1.fastq',None,json.dumps({'format':'FASTQ'}),now,now))
        c.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?,?)',('primary',owner['id'],'s3://test/tenants/'+owner['id']+'/R1.fastq','R1.fastq','FASTQ',100,now,'cloud',None,'{}'))
        c.execute('INSERT INTO datasets VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',('foreign','u_foreign',None,'R2.fastq',100,'b'*64,'Ready','tenants/u_foreign/R2.fastq',None,json.dumps({'format':'FASTQ'}),now,now))
    with pytest.raises(ValueError,match='owned'):
        cloud_data.bind(owner['id'],'primary',{'mate_id':'foreign'})

def test_unfunded_retained_storage_is_rejected(client,owner):
    p=allowance();p.update(max_storage_bytes=20000*1024**3)
    with pytest.raises(ValueError,match='retained-storage'):
        entitlements.grant(owner,owner['id'],{'reason':'Insufficient funded storage test','funding_confirmed':True,'allowance':p})

def test_vm_unknown_launch_replays_the_same_provider_token_and_parameters(client,owner,monkeypatch):
    from backend import vm
    from backend.cloud import UncertainCloudState
    rid='test-vm-run';aid='test-vm-attempt';uid=owner['id'];launches=[]
    with db.tx() as c:
        c.execute("INSERT INTO runs(id,user_id,operation_id,state,graph,created,reserved) VALUES(?,?,?,'running','{}',?,?)",(rid,uid,'vm-test-operation',time.time(),2040))
        c.execute("INSERT INTO attempts(id,run_id,node_id,state,started) VALUES(?,?,?,'running',?)",(aid,rid,'node-vm',time.time()))
    class EC2:
        def run_instances(self,**request):
            launches.append(request)
            if len(launches)==1:raise TimeoutError('Unknown provider response')
            return {'Instances':[{'InstanceId':'i-fixture'}]}
        def describe_instances(self,**kwargs):return {'Reservations':[{'Instances':[{'State':{'Name':'terminated'},'Tags':[{'Key':'TenantId','Value':uid},{'Key':'AttemptId','Value':aid}]}]}]}
    class S3:
        def head_object(self,**kwargs):return {'ContentLength':10}
    ec2=EC2()
    class Session:
        def client(self,service):return ec2 if service=='ec2' else S3()
    monkeypatch.setattr(vm,'aws_session',lambda:Session())
    node={'id':'node-vm','_approved_image':'registry/core@sha256:'+'a'*64,'_capacity':{'instance_profile':'arn:fixture-profile'},'_vm_configuration':{'ami':'ami-fixture','image':'registry/core@sha256:'+'a'*64,'subnet':'subnet-fixture','security_group':'sg-fixture'},'resources':{'threads':2,'memory_mb':4096,'scratch_mb':32768,'timeout_seconds':120}}
    settings={'s3_bucket':'test-bucket'};key=f'tenants/{uid}/runs/{rid}/{aid}/manifest.json'
    with pytest.raises(UncertainCloudState):vm.execute(uid,rid,aid,node,settings,key,lambda r:False)
    assert json.loads(db.query('SELECT metrics FROM attempts WHERE id=?',(aid,),one=True)['metrics'])['submission_intent']
    assert vm.execute(uid,rid,aid,node,settings,key,lambda r:False)=='i-fixture'
    assert launches[0]==launches[1] and launches[0]['ClientToken']==aid

def test_worker_metadata_cannot_claim_curated_parser_trust(client,owner,monkeypatch):
    from backend import config,workbench
    monkeypatch.setattr(config,'CLOUD',True)
    with pytest.raises(ValueError,match='isolated workbench'):
        workbench.source(owner['id'],{'path':'s3://anything/forged.bam','metadata':{'sample_only':True}})

def test_refreshed_fargate_core_preserves_only_qualified_vm_inputs():
    from backend import vm
    old='registry/core@sha256:'+'a'*64
    new='registry/extended@sha256:'+'b'*64
    capacity={'image':old,'definition':'old-reviewed-definition'}
    refreshed={'image':new,'definition':'new-reviewed-definition','family':'core-cpu-refresh-v1'}
    assert vm.runtime_route(capacity,refreshed,False,{'image':old})['image']==old
    assert refreshed['image']==new  # Fargate route is never mutated.
    with pytest.raises(ValueError,match='Migrated'):
        vm.runtime_route(capacity,refreshed,True,{'image':old})
    with pytest.raises(ValueError,match='does not match'):
        vm.runtime_route(capacity,refreshed,False,{'image':new})
    with pytest.raises(ValueError,match='not present'):
        vm.runtime_route(capacity,{**refreshed,'family':'extended-cpu-v1'},False,{'image':old})


def test_migrated_input_cannot_launch_an_older_worker(monkeypatch,tmp_path):
    from backend import cloud,registry
    contacted=[]
    def no_provider():
        contacted.append(True)
        raise AssertionError('Unqualified data must be rejected before contacting AWS')
    monkeypatch.setattr(cloud,'aws_session',no_provider)
    node={'_migration_sources':[{'Bucket':'shared','Key':'tenants/owner/input'}]}
    with pytest.raises(ValueError,match='migration-compatible runtime'):
        cloud.execute_batch('owner','run','attempt',registry.BASE['fastqc'],node,{},tmp_path,lambda _:False)
    assert not contacted
    for invalid in [None,{}, {'source_grants_version':True},{'source_grants_version':2}]:
        with pytest.raises(ValueError):cloud.require_migration_runtime(invalid,True)
    cloud.require_migration_runtime(None,False)
    cloud.require_migration_runtime({'source_grants_version':1},True)
