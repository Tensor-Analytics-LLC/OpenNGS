"""Publish a fixed tenant template and narrowly authorize its deployment.

Run with an infrastructure administrator. The application cannot edit the
published S3 object, choose another template, or pass another service role.
This does not grant any user funding or start scientific workers.
"""
import hashlib,json,sys
from pathlib import Path
from urllib.parse import quote
from backend.config import aws_session
from infra.research_stack import template

def optional_vep_release():
    # A finalized candidate is deliberately not a publication instruction.
    path=Path('evidence/deployment/vep-release.json')
    if not path.exists():return None
    from backend.vep_runtime import validate_release
    return validate_release(json.loads(path.read_text()))

def optional_formats_release():
    # Candidate evidence is reviewable without making it a deployment request.
    path=Path('evidence/deployment/portable-formats-release.json')
    if not path.exists():return None
    from backend.runtime_catalog import validate_format_release
    return validate_format_release(json.loads(path.read_text()))

def main():
    vep=optional_vep_release()
    formats=optional_formats_release()
    s=aws_session();iam=s.client('iam');cfn=s.client('cloudformation');account=s.client('sts').get_caller_identity()['Account']
    stack=cfn.describe_stacks(StackName='openngs-lean')['Stacks'][0];o={x['OutputKey']:x['OutputValue'] for x in stack['Outputs']}
    release=json.loads(Path('evidence/deployment/release.json').read_text());build=json.loads(Path('evidence/deployment/research-build.json').read_text())
    status=s.client('codebuild').batch_get_builds(ids=[build['build_id']])['builds'][0]
    if status['buildStatus']!='SUCCEEDED':raise ValueError('Core runtime build has not succeeded')
    image_record=s.client('ecr').describe_images(repositoryName='openngs-demo',imageIds=[{'imageTag':build['tag']}])['imageDetails'][0]
    repository=f'{account}.dkr.ecr.us-east-1.amazonaws.com/openngs-demo';image=repository+'@'+image_record['imageDigest']
    resources=cfn.describe_stack_resources(StackName='openngs-demo-compute')['StackResources'];execution=next(x['PhysicalResourceId'] for x in resources if x['LogicalResourceId']=='ExecutionRole');execution_arn=iam.get_role(RoleName=execution)['Role']['Arn']
    key=release['kms'];bucket=release['bucket'];pro_bucket='arn:aws:s3:::openngs-pro-'+account+'-*'
    boundary_document={'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':['s3:GetObject','s3:GetObjectVersion','s3:PutObject','s3:AbortMultipartUpload','s3:ListMultipartUploadParts'],'Resource':pro_bucket+'/tenants/u_*/*'},{'Effect':'Allow','Action':['s3:GetObject','s3:GetObjectVersion'],'Resource':'arn:aws:s3:::'+bucket+'/operations/checkpoints/references/*'},{'Effect':'Allow','Action':['kms:Decrypt','kms:GenerateDataKey'],'Resource':key,'Condition':{'StringEquals':{'kms:ViaService':'s3.us-east-1.amazonaws.com'}}}]}
    boundary=f'arn:aws:iam::{account}:policy/openngs/OpenNGSResearchWorkerBoundary'
    boundary_document['Statement'].append({'Effect':'Allow','Action':['s3:GetObject','s3:GetObjectVersion'],
        'Resource':'arn:aws:s3:::'+bucket+'/tenants/u_*/datasets/*'})
    try:iam.create_policy(PolicyName='OpenNGSResearchWorkerBoundary',Path='/openngs/',PolicyDocument=json.dumps(boundary_document),Tags=[{'Key':'Project','Value':'OpenNGS'}])
    except iam.exceptions.EntityAlreadyExistsException:
        current=iam.get_policy(PolicyArn=boundary)['Policy']['DefaultVersionId'];observed=iam.get_policy_version(PolicyArn=boundary,VersionId=current)['PolicyVersion']['Document']
        if observed!=boundary_document:
            if '--update-reviewed-boundary' not in sys.argv:raise ValueError('Existing worker boundary differs; review it before publication')
            iam.create_policy_version(PolicyArn=boundary,PolicyDocument=json.dumps(boundary_document),SetAsDefault=True)
    name='OpenNGSResearchStackRole';role_arn=f'arn:aws:iam::{account}:role/openngs/{name}'
    assume={'Version':'2012-10-17','Statement':[{'Effect':'Allow','Principal':{'Service':'cloudformation.amazonaws.com'},'Action':'sts:AssumeRole','Condition':{'StringEquals':{'aws:SourceAccount':account}}}]}
    try:iam.create_role(RoleName=name,Path='/openngs/',AssumeRolePolicyDocument=json.dumps(assume),Tags=[{'Key':'Project','Value':'OpenNGS'}])
    except iam.exceptions.EntityAlreadyExistsException:pass
    roles=f'arn:aws:iam::{account}:role/openngs/pro/openngs-pro-*'
    statements=[{'Effect':'Allow','Action':['iam:CreateRole'],'Resource':roles,'Condition':{'StringEquals':{'iam:PermissionsBoundary':boundary}}},{'Effect':'Allow','Action':['iam:GetRole','iam:PutRolePolicy','iam:UpdateAssumeRolePolicy','iam:GetRolePolicy','iam:DeleteRolePolicy','iam:DeleteRole','iam:TagRole','iam:UntagRole'],'Resource':roles},{'Effect':'Allow','Action':['iam:PassRole'],'Resource':[roles,execution_arn],'Condition':{'StringEquals':{'iam:PassedToService':['ecs-tasks.amazonaws.com','ec2.amazonaws.com']}}},{'Effect':'Allow','Action':['s3:CreateBucket','s3:GetBucket*','s3:ListBucket','s3:PutBucketPolicy','s3:DeleteBucketPolicy','s3:PutBucketPublicAccessBlock','s3:PutBucketOwnershipControls','s3:PutEncryptionConfiguration','s3:GetEncryptionConfiguration','s3:PutBucketVersioning','s3:PutBucketCORS','s3:PutLifecycleConfiguration','s3:PutBucketTagging'],'Resource':pro_bucket},{'Effect':'Allow','Action':['batch:CreateComputeEnvironment','batch:UpdateComputeEnvironment','batch:DeleteComputeEnvironment','batch:CreateJobQueue','batch:UpdateJobQueue','batch:DeleteJobQueue','batch:RegisterJobDefinition','batch:DeregisterJobDefinition','batch:TagResource','batch:UntagResource'],'Resource':[f'arn:aws:batch:us-east-1:{account}:compute-environment/openngs-pro-*',f'arn:aws:batch:us-east-1:{account}:job-queue/openngs-pro-*',f'arn:aws:batch:us-east-1:{account}:job-definition/openngs-pro-*']},{'Effect':'Allow','Action':['batch:DescribeComputeEnvironments','batch:DescribeJobQueues','batch:DescribeJobDefinitions','batch:ListTagsForResource','ec2:DescribeSubnets','ec2:DescribeSecurityGroups'],'Resource':'*'},{'Effect':'Allow','Action':['logs:CreateLogGroup','logs:PutRetentionPolicy','logs:DescribeLogGroups','logs:TagResource','logs:ListTagsForResource','logs:DeleteLogGroup'],'Resource':f'arn:aws:logs:us-east-1:{account}:log-group:/openngs/pro/*'},{'Effect':'Allow','Action':['s3:GetObject','s3:GetObjectVersion'],'Resource':'arn:aws:s3:::'+bucket+'/operations/releases/*'},{'Effect':'Allow','Action':['kms:Decrypt','kms:DescribeKey'],'Resource':key}]
    statements.append({'Effect':'Allow','Action':['iam:CreateInstanceProfile','iam:GetInstanceProfile','iam:AddRoleToInstanceProfile','iam:RemoveRoleFromInstanceProfile','iam:DeleteInstanceProfile','iam:TagInstanceProfile'],'Resource':f'arn:aws:iam::{account}:instance-profile/openngs/pro/openngs-pro-*'})
    iam.put_role_policy(RoleName=name,PolicyName='FixedOpenNGSTenantResources',PolicyDocument=json.dumps({'Version':'2012-10-17','Statement':statements}))
    iam.put_role_policy(RoleName=execution,PolicyName='OpenNGSResearchLogs',PolicyDocument=json.dumps({'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':['logs:CreateLogStream','logs:PutLogEvents'],'Resource':f'arn:aws:logs:us-east-1:{account}:log-group:/openngs/pro/*:log-stream:*'}]}))
    budget_stack=cfn.describe_stack_resources(StackName='openngs-budget')['StackResources'];budget_key=next(x['PhysicalResourceId'] for x in budget_stack if x['LogicalResourceId']=='AlertKey')
    topic=s.client('sns').create_topic(Name='openngs-research-budget',Attributes={'KmsMasterKeyId':budget_key},Tags=[{'Key':'Project','Value':'OpenNGS'}])['TopicArn']
    topic_policy={'Version':'2012-10-17','Statement':[{'Effect':'Allow','Principal':{'Service':'budgets.amazonaws.com'},'Action':'sns:Publish','Resource':topic,'Condition':{'StringEquals':{'aws:SourceAccount':account},'ArnLike':{'aws:SourceArn':f'arn:aws:budgets::{account}:budget/openngs-pro-*'}}}]}
    s.client('sns').set_topic_attributes(TopicArn=topic,AttributeName='Policy',AttributeValue=json.dumps(topic_policy))
    budget_outputs={x['OutputKey']:x['OutputValue'] for x in cfn.describe_stacks(StackName='openngs-budget')['Stacks'][0]['Outputs']};queue=budget_outputs['QueueURL'];sqs=s.client('sqs');attributes=sqs.get_queue_attributes(QueueUrl=queue,AttributeNames=['QueueArn','Policy'])['Attributes'];queue_policy=json.loads(attributes['Policy'])
    queue_policy['Statement']=[x for x in queue_policy['Statement'] if x.get('Sid')!='OpenNGSResearchBudgetDelivery']
    queue_policy['Statement'].append({'Sid':'OpenNGSResearchBudgetDelivery','Effect':'Allow','Principal':{'Service':'sns.amazonaws.com'},'Action':'sqs:SendMessage','Resource':attributes['QueueArn'],'Condition':{'ArnEquals':{'aws:SourceArn':topic}}})
    sqs.set_queue_attributes(QueueUrl=queue,Attributes={'Policy':json.dumps(queue_policy)});s.client('sns').subscribe(TopicArn=topic,Protocol='sqs',Endpoint=attributes['QueueArn'])
    statements.append({'Effect':'Allow','Action':['budgets:ModifyBudget','budgets:ViewBudget','budgets:TagResource','budgets:UntagResource','budgets:ListTagsForResource'],'Resource':f'arn:aws:budgets::{account}:budget/openngs-pro-*'})
    iam.put_role_policy(RoleName=name,PolicyName='FixedOpenNGSTenantResources',PolicyDocument=json.dumps({'Version':'2012-10-17','Statement':statements}))
    extension_path=Path('evidence/deployment/extended-release.json')
    extension=json.loads(extension_path.read_text()) if extension_path.exists() else None
    secondary_path=Path('evidence/deployment/secondary-release.json')
    secondary=json.loads(secondary_path.read_text()) if secondary_path.exists() else None
    deepvariant_path=Path('evidence/deployment/deepvariant-release.json')
    deepvariant=json.loads(deepvariant_path.read_text()) if deepvariant_path.exists() else None
    document=template(o['WorkerSubnet'],o['WorkerSecurity'],execution_arn,image,'arn:aws:ecr:us-east-1:'+account+':repository/openngs-demo',bucket,key,boundary,o['URL'],topic,extension,secondary,deepvariant,vep,formats)
    raw=json.dumps(document,sort_keys=True).encode();digest=hashlib.sha256(raw).hexdigest();object_key='operations/releases/'+digest+'/research-tenant.json'
    uploaded=s.client('s3').put_object(Bucket=bucket,Key=object_key,Body=raw,ServerSideEncryption='aws:kms',SSEKMSKeyId=key)
    url=f'https://{bucket}.s3.us-east-1.amazonaws.com/{object_key}?versionId='+quote(uploaded['VersionId'],safe='')
    arn=f'arn:aws:cloudformation:us-east-1:{account}:stack/openngs-pro-*/*'
    app_statements=[{'Effect':'Allow','Action':['cloudformation:CreateStack','cloudformation:UpdateStack'],'Resource':arn,'Condition':{'StringEquals':{'cloudformation:TemplateUrl':url,'cloudformation:RoleArn':role_arn}}},{'Effect':'Allow','Action':['cloudformation:DescribeStacks'],'Resource':arn},{'Effect':'Allow','Action':['iam:PassRole'],'Resource':role_arn,'Condition':{'StringEquals':{'iam:PassedToService':'cloudformation.amazonaws.com'}}},{'Effect':'Allow','Action':['s3:ListBucket','s3:GetBucketLocation','s3:ListBucketMultipartUploads'],'Resource':pro_bucket},{'Effect':'Allow','Action':['s3:GetObject','s3:GetObjectVersion','s3:PutObject','s3:AbortMultipartUpload','s3:ListMultipartUploadParts'],'Resource':pro_bucket+'/tenants/*'},{'Effect':'Allow','Action':['batch:SubmitJob','batch:TagResource'],'Resource':[f'arn:aws:batch:us-east-1:{account}:job-queue/openngs-pro-*',f'arn:aws:batch:us-east-1:{account}:job-definition/openngs-pro-*']},{'Effect':'Allow','Action':['batch:TagResource'],'Resource':f'arn:aws:batch:us-east-1:{account}:job/*','Condition':{'StringEquals':{'aws:RequestTag/Project':'OpenNGS-Pro'}}},{'Effect':'Allow','Action':['batch:TerminateJob'],'Resource':f'arn:aws:batch:us-east-1:{account}:job/*','Condition':{'StringEquals':{'aws:ResourceTag/Project':'OpenNGS-Pro'}}}]
    iam.put_role_policy(RoleName=o['AppRole'],PolicyName='OpenNGSResearchLogRead',PolicyDocument=json.dumps({'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':['logs:GetLogEvents'],'Resource':f'arn:aws:logs:us-east-1:{account}:log-group:/openngs/pro/u_*:log-stream:research/*'}]}))
    iam.put_role_policy(RoleName=o['AppRole'],PolicyName='OnlyFixedFundedResearchTemplate',PolicyDocument=json.dumps({'Version':'2012-10-17','Statement':app_statements}))
    record={'template_url':url,'template_sha256':digest,'cfn_role':role_arn,'runtime_image':image,'budget_topic':topic,'runtime_build':build['build_id'],'validation':'Build/version checks only; native assay and scale validation are separate','extension':extension,'secondary':secondary,'deepvariant':deepvariant,'vep':vep,'formats':formats}
    Path('evidence/deployment/research-capacity.json').write_text(json.dumps(record,indent=2));print(json.dumps(record))

if __name__=='__main__':main()
