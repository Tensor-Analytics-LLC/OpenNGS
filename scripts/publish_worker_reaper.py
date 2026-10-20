"""Deploy independent worker deadline enforcement without changing app services."""
import json
from backend.config import aws_session
from infra.worker_reaper import template

def main():
    cf=aws_session().client('cloudformation')
    outputs={v['OutputKey']:v['OutputValue'] for v in cf.describe_stacks(StackName='openngs-lean')['Stacks'][0]['Outputs']}
    ec2=aws_session().client('ec2');vpc=ec2.describe_subnets(SubnetIds=[outputs['WorkerSubnet']])['Subnets'][0]['VpcId']
    document=json.dumps(template(vpc))
    try:cf.create_stack(StackName='openngs-worker-deadlines',TemplateBody=document,Capabilities=['CAPABILITY_IAM'],Tags=[{'Key':'Project','Value':'OpenNGS'}])
    except cf.exceptions.AlreadyExistsException:
        try:cf.update_stack(StackName='openngs-worker-deadlines',TemplateBody=document,Capabilities=['CAPABILITY_IAM'])
        except cf.exceptions.ClientError as e:
            if 'No updates are to be performed' not in str(e):raise
    print(json.dumps({'stack':'openngs-worker-deadlines','vpc':vpc,'cadence_seconds':60,'data_access':False}))

if __name__=='__main__':main()
