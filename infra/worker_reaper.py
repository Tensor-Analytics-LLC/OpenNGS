"""Independent deadline enforcement when the application or a guest is unavailable."""
import json

HANDLER = '''import json,os,re,time
import boto3

def expired(instance,now,vpc):
    tags={x['Key']:x['Value'] for x in instance.get('Tags',[])}
    if instance.get('VpcId')!=vpc or tags.get('Project')!='OpenNGS-worker':return False
    if not re.fullmatch(r'u_[0-9a-f]{32}',tags.get('TenantId','')):return False
    if not re.fullmatch(r'run_[0-9a-f]{32}',tags.get('RunId','')):return False
    try:deadline=int(tags.get('Deadline',''))
    except (ValueError,TypeError):return False
    return 0<deadline<=now and instance.get('State',{}).get('Name') in ['pending','running','stopping','stopped']

def handler(event,context):
    ec2=boto3.client('ec2');now=int(time.time());vpc=os.environ['WORKER_VPC'];terminated=[]
    pages=ec2.get_paginator('describe_instances').paginate(Filters=[{'Name':'vpc-id','Values':[vpc]},{'Name':'tag:Project','Values':['OpenNGS-worker']},{'Name':'instance-state-name','Values':['pending','running','stopping','stopped']}])
    for page in pages:
        for reservation in page['Reservations']:
            for instance in reservation['Instances']:
                if expired(instance,now,vpc):
                    ec2.terminate_instances(InstanceIds=[instance['InstanceId']]);terminated.append(instance['InstanceId'])
    print(json.dumps({'expired_workers_terminated':terminated,'checked_at':now}))
    return {'terminated':terminated}
'''

def template(vpc):
    ref=lambda n:{'Ref':n};att=lambda n,k:{'Fn::GetAtt':[n,k]};sub=lambda v:{'Fn::Sub':v}
    tags=[{'Key':'Project','Value':'OpenNGS'}]
    resources={
      'Role':{'Type':'AWS::IAM::Role','Properties':{'AssumeRolePolicyDocument':{'Version':'2012-10-17','Statement':[{'Effect':'Allow','Principal':{'Service':'lambda.amazonaws.com'},'Action':'sts:AssumeRole'}]},'Tags':tags,'Policies':[{'PolicyName':'OnlyExpiredOpenNGSWorkers','PolicyDocument':{'Version':'2012-10-17','Statement':[
        {'Effect':'Allow','Action':'ec2:DescribeInstances','Resource':'*'},
        {'Effect':'Allow','Action':'ec2:TerminateInstances','Resource':sub('arn:aws:ec2:${AWS::Region}:${AWS::AccountId}:instance/*'),'Condition':{'StringEquals':{'ec2:ResourceTag/Project':'OpenNGS-worker'}}},
        {'Effect':'Allow','Action':['logs:CreateLogStream','logs:PutLogEvents'],'Resource':sub('arn:aws:logs:${AWS::Region}:${AWS::AccountId}:log-group:/aws/lambda/openngs-worker-reaper:*')}
      ]}}]}},
      'Logs':{'Type':'AWS::Logs::LogGroup','Properties':{'LogGroupName':'/aws/lambda/openngs-worker-reaper','RetentionInDays':7,'Tags':tags}},
      'Function':{'Type':'AWS::Lambda::Function','DependsOn':['Logs'],'Properties':{'FunctionName':'openngs-worker-reaper','Runtime':'python3.13','Handler':'index.handler','Role':att('Role','Arn'),'Timeout':30,'MemorySize':128,'ReservedConcurrentExecutions':1,'Environment':{'Variables':{'WORKER_VPC':vpc}},'Code':{'ZipFile':HANDLER},'Tags':tags}},
      'Schedule':{'Type':'AWS::Events::Rule','Properties':{'Name':'openngs-worker-deadlines','ScheduleExpression':'rate(1 minute)','State':'ENABLED','Targets':[{'Id':'DeadlineReaper','Arn':att('Function','Arn')}] }},
      'Invoke':{'Type':'AWS::Lambda::Permission','Properties':{'FunctionName':ref('Function'),'Action':'lambda:InvokeFunction','Principal':'events.amazonaws.com','SourceArn':att('Schedule','Arn'),'SourceAccount':ref('AWS::AccountId')}}
    }
    return {'AWSTemplateFormatVersion':'2010-09-09','Description':'Independent OpenNGS worker deadline reaper; no dataset or secret access','Resources':resources,'Outputs':{'Function':{'Value':ref('Function')}}}
