"""Narrow launch permissions for the reviewed private, exclusive-worker AMI."""
import json
from pathlib import Path
from backend.config import aws_session
from backend.vm import PROFILES

def main():
    s=aws_session();iam=s.client('iam');account=s.client('sts').get_caller_identity()['Account'];r=json.loads(Path('evidence/deployment/vm-build.json').read_text())
    if not r.get('ready'):raise ValueError('Verify and finish the private image bake first')
    image=s.client('ec2').describe_images(ImageIds=[r['ami']])['Images'][0]
    if image['State']!='available' or image.get('Public') or image['OwnerId']!=account:raise ValueError('Published AMI must be owned, available and private')
    o={x['OutputKey']:x['OutputValue'] for x in s.client('cloudformation').describe_stacks(StackName='openngs-lean')['Stacks'][0]['Outputs']}
    base=f'arn:aws:ec2:us-east-1:{account}:'
    tag_condition={'StringEquals':{'aws:RequestTag/Project':'OpenNGS-worker'},'StringLike':{'aws:RequestTag/TenantId':'u_*'}}
    statements=[{'Effect':'Allow','Action':['ec2:RunInstances'],'Resource':[f'arn:aws:ec2:us-east-1::image/{r["ami"]}',base+'subnet/'+o['WorkerSubnet'],base+'security-group/'+o['WorkerSecurity']]},{'Effect':'Allow','Action':['ec2:RunInstances'],'Resource':[base+'instance/*'],'Condition':{**tag_condition,'StringEquals':{**tag_condition['StringEquals'],'ec2:InstanceType':[p['instance_type'] for p in PROFILES],'ec2:MetadataHttpTokens':'required'}}},{'Effect':'Allow','Action':['ec2:RunInstances'],'Resource':[base+'volume/*',base+'network-interface/*'],'Condition':tag_condition},{'Effect':'Allow','Action':['ec2:CreateTags'],'Resource':[base+'instance/*',base+'volume/*',base+'network-interface/*'],'Condition':{'StringEquals':{'ec2:CreateAction':'RunInstances','aws:RequestTag/Project':'OpenNGS-worker'}}},{'Effect':'Allow','Action':['iam:PassRole'],'Resource':f'arn:aws:iam::{account}:role/openngs/pro/openngs-pro-*','Condition':{'StringEquals':{'iam:PassedToService':'ec2.amazonaws.com'}}}]
    iam.put_role_policy(RoleName=o['AppRole'],PolicyName='OnlyReviewedExclusiveScientificVMs',PolicyDocument=json.dumps({'Version':'2012-10-17','Statement':statements}))
    result={'ami':r['ami'],'image':r['image'],'subnet':o['WorkerSubnet'],'security_group':o['WorkerSecurity'],'profiles':[p['instance_type']+' / '+str(p['vcpus'])+' vCPU / '+str(p['memory_mb']//1024)+' GiB' for p in PROFILES],'maximum_scratch_gib':2048,'execution':'Fresh private VM per stage; terminate after committed result or deadline; no reuse'}
    Path('evidence/deployment/vm-capacity.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))

if __name__=='__main__':main()
