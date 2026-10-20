"""Deploy the retained encrypted audit-head archive, without touching app data."""
import json
from pathlib import Path
from backend.config import aws_session
from infra.audit_stack import template

def main():
    s=aws_session();cf=s.client('cloudformation');account=s.client('sts').get_caller_identity()['Account']
    release=json.loads(Path('evidence/deployment/release.json').read_text());outputs={x['OutputKey']:x['OutputValue'] for x in cf.describe_stacks(StackName='openngs-lean')['Stacks'][0]['Outputs']}
    document=template(account,release['kms'],outputs['AppRole'])
    try:cf.create_stack(StackName='openngs-audit',TemplateBody=json.dumps(document),Capabilities=['CAPABILITY_IAM'],Tags=[{'Key':'Project','Value':'OpenNGS'}])
    except cf.exceptions.AlreadyExistsException:
        try:cf.update_stack(StackName='openngs-audit',TemplateBody=json.dumps(document),Capabilities=['CAPABILITY_IAM'])
        except cf.exceptions.ClientError as e:
            if 'No updates are to be performed' not in str(e):raise
    print(json.dumps({'stack':'openngs-audit','bucket':'openngs-audit-'+account+'-us-east-1','retention':'GOVERNANCE / 30 days','application_delete_or_bypass':False}))

if __name__=='__main__':main()
