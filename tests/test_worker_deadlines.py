from infra.worker_reaper import HANDLER,template

def test_independent_reaper_never_targets_other_projects_or_active_jobs():
    namespace={};exec(HANDLER,namespace)
    expired=namespace['expired']
    def instance(**tags):
        values={'Project':'OpenNGS-worker','TenantId':'u_'+'a'*32,'RunId':'run_'+'b'*32,'Deadline':'99',**tags}
        return {'VpcId':'vpc-ours','State':{'Name':'running'},'Tags':[{'Key':k,'Value':v} for k,v in values.items()]}
    assert expired(instance(),100,'vpc-ours')
    assert not expired(instance(),100,'vpc-other')
    for tags in [{'Project':'Grepeye'},{'Project':'OpenNGS'},{'Deadline':'101'},{'Deadline':'bad'},{'Deadline':'0'},{'TenantId':'other'},{'RunId':'missing'}]:
        assert not expired(instance(**tags),100,'vpc-ours')
    dead=instance();dead['State']['Name']='terminated'
    assert not expired(dead,100,'vpc-ours')
    document=template('vpc-ours');role=document['Resources']['Role']['Properties']
    actions=[s['Action'] for s in role['Policies'][0]['PolicyDocument']['Statement']]
    assert 's3:GetObject' not in str(actions) and 'kms:Decrypt' not in str(actions)
    assert document['Resources']['Schedule']['Properties']['ScheduleExpression']=='rate(1 minute)'
