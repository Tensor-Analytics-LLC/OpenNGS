"""Apply scoped network controls, retaining the exact previous endpoint policy."""
import json
from pathlib import Path
from backend.config import aws_session
from infra.network_guard import s3_policy,template

def main():
    s=aws_session();c=s.client('cloudformation');ec2=s.client('ec2');account=s.client('sts').get_caller_identity()['Account'];release=json.loads(Path('evidence/deployment/release.json').read_text());resources=c.describe_stack_resources(StackName='openngs-lean')['StackResources'];ids={x['LogicalResourceId']:x['PhysicalResourceId'] for x in resources}
    endpoint=ec2.describe_vpc_endpoints(VpcEndpointIds=[ids['S3Endpoint']])['VpcEndpoints'][0];path=Path('evidence/deployment/previous-s3-endpoint-policy.json')
    if not path.exists():path.write_text(endpoint['PolicyDocument'])
    policy=s3_policy(release['bucket'],account);ec2.modify_vpc_endpoint(VpcEndpointId=ids['S3Endpoint'],PolicyDocument=json.dumps(policy))
    t=template(ids['VPC'])
    try:c.create_stack(StackName='openngs-network-guard',TemplateBody=json.dumps(t),Tags=[{'Key':'Project','Value':'OpenNGS'}])
    except c.exceptions.AlreadyExistsException:
        try:c.update_stack(StackName='openngs-network-guard',TemplateBody=json.dumps(t))
        except c.exceptions.ClientError as e:
            if 'No updates are to be performed' not in str(e):raise
    s.client('route53resolver').update_firewall_config(ResourceId=ids['VPC'],FirewallFailOpen='DISABLED')
    Path('evidence/deployment/network-guard.json').write_text(json.dumps({'vpc':ids['VPC'],'s3_endpoint':ids['S3Endpoint'],'s3_policy':policy,'dns_stack':'openngs-network-guard','dns_fail_open':False,'scope':'Only OpenNGS VPC; other application networks and bucket policies unchanged'},indent=2));print('OpenNGS storage endpoint restricted; DNS allowlist deployment started')

if __name__=='__main__':main()
