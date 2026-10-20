"""Bake a private, encrypted scientific VM image without using local storage.

The builder contains only reviewed public software. It never receives user
datasets, application secrets or database credentials. No worker is kept idle.
"""
import json,shlex,time
from pathlib import Path
from backend.config import aws_session

def main():
    s=aws_session();iam=s.client('iam');ec2=s.client('ec2');c=s.client('cloudformation');account=s.client('sts').get_caller_identity()['Account'];record=json.loads(Path('evidence/deployment/research-capacity.json').read_text());image=record['runtime_image']
    o={x['OutputKey']:x['OutputValue'] for x in c.describe_stacks(StackName='openngs-lean')['Stacks'][0]['Outputs']};resources=c.describe_stack_resources(StackName='openngs-lean')['StackResources'];subnet=next(x['PhysicalResourceId'] for x in resources if x['LogicalResourceId']=='AppSubnet');sg=next(x['PhysicalResourceId'] for x in resources if x['LogicalResourceId']=='AppSecurity')
    name='OpenNGSScientificImageBuilder';assume={'Version':'2012-10-17','Statement':[{'Effect':'Allow','Principal':{'Service':'ec2.amazonaws.com'},'Action':'sts:AssumeRole'}]}
    try:iam.create_role(RoleName=name,Path='/openngs/',AssumeRolePolicyDocument=json.dumps(assume),Tags=[{'Key':'Project','Value':'OpenNGS-validation'}])
    except iam.exceptions.EntityAlreadyExistsException:pass
    iam.attach_role_policy(RoleName=name,PolicyArn='arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore')
    doc={'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':['ecr:GetAuthorizationToken'],'Resource':'*'},{'Effect':'Allow','Action':['ecr:GetDownloadUrlForLayer','ecr:BatchCheckLayerAvailability','ecr:BatchGetImage'],'Resource':f'arn:aws:ecr:us-east-1:{account}:repository/openngs-demo'}]}
    iam.put_role_policy(RoleName=name,PolicyName='OnlyReviewedOpenNGSImage',PolicyDocument=json.dumps(doc))
    try:iam.create_instance_profile(InstanceProfileName=name,Path='/openngs/')
    except iam.exceptions.EntityAlreadyExistsException:pass
    try:iam.add_role_to_instance_profile(InstanceProfileName=name,RoleName=name)
    except iam.exceptions.LimitExceededException:pass
    base=s.client('ssm').get_parameter(Name='/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64')['Parameter']['Value']
    script='''#!/bin/bash
set -euo pipefail
shutdown -h +20
dnf install -y docker
systemctl enable --now docker
cat > /etc/amazon/ssm/amazon-ssm-agent.json <<'SSM'
{"Ssm":{"Endpoint":"ssm.us-east-1.api.aws"},"Mgs":{"Endpoint":"ssmmessages.us-east-1.api.aws"},"Agent":{"Region":"us-east-1","UseDualStackEndpoint":true}}
SSM
systemctl restart amazon-ssm-agent
export AWS_DEFAULT_REGION=us-east-1 AWS_USE_DUALSTACK_ENDPOINT=true
aws ecr get-login-password | docker login --username AWS --password-stdin '''+shlex.quote(image.split('/')[0])+'''
docker pull '''+shlex.quote(image)+'''
docker logout '''+shlex.quote(image.split('/')[0])+'''
rm -f /root/.docker/config.json
docker image inspect '''+shlex.quote(image)+''' --format '{{index .RepoDigests 0}}' > /var/lib/openngs-image-digest
touch /var/lib/openngs-image-ready
'''
    tags=[{'Key':'Project','Value':'OpenNGS-validation'},{'Key':'Name','Value':'OpenNGS reviewed scientific image builder'}]
    launched=ec2.run_instances(ImageId=base,InstanceType='t3.medium',MinCount=1,MaxCount=1,SubnetId=subnet,SecurityGroupIds=[sg],IamInstanceProfile={'Name':name},InstanceInitiatedShutdownBehavior='terminate',MetadataOptions={'HttpTokens':'required','HttpPutResponseHopLimit':1},CreditSpecification={'CpuCredits':'standard'},BlockDeviceMappings=[{'DeviceName':'/dev/xvda','Ebs':{'VolumeSize':20,'VolumeType':'gp3','Encrypted':True,'DeleteOnTermination':True}}],TagSpecifications=[{'ResourceType':t,'Tags':tags} for t in ['instance','volume']],UserData=script,ClientToken='openngs-bake-'+image.rsplit(':',1)[1][:40])
    result={'builder':launched['Instances'][0]['InstanceId'],'base_ami':base,'image':image,'started':time.time(),'maximum_builder_seconds':1200}
    Path('evidence/deployment/vm-build.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))

if __name__=='__main__':main()
