"""Publish a scoped, resumable derived-output retention service for OpenNGS."""
import json
from pathlib import Path
from backend.config import aws_session

def main():
    session=aws_session()
    account=session.client('sts').get_caller_identity()['Account']
    stack=session.client('cloudformation').describe_stacks(StackName='openngs-lean')['Stacks'][0]
    outputs={x['OutputKey']:x['OutputValue'] for x in stack['Outputs']}
    iam=session.client('iam')
    role=outputs['AppRole']
    buckets=['arn:aws:s3:::ngs-studio-cache-'+account+'-us-east-1',
             'arn:aws:s3:::openngs-pro-'+account+'-*']
    policy={'Version':'2012-10-17','Statement':[
        {'Effect':'Allow','Action':['s3:ListBucketVersions','s3:ListBucketMultipartUploads'],
         'Resource':buckets,'Condition':{'StringLike':{'s3:prefix':['tenants/*/runs/*','tenants/*/datasets/*']}}},
        {'Effect':'Allow','Action':['s3:GetObjectVersion','s3:DeleteObjectVersion'],
         'Resource':[b+'/tenants/*/runs/*' for b in buckets]},
        {'Effect':'Allow','Action':['s3:AbortMultipartUpload','s3:ListMultipartUploadParts'],
         'Resource':[b+'/tenants/*/'+p for b in buckets for p in ['runs/*','datasets/*']]}]}
    iam.put_role_policy(RoleName=role,PolicyName='OpenNGSExactVersionRetention',PolicyDocument=json.dumps(policy))
    command="""set -euo pipefail
cp /opt/openngs/infra/openngs-retention.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now openngs-retention
systemctl is-active openngs-retention
"""
    response=session.client('ssm').send_command(InstanceIds=[outputs['HostId']],DocumentName='AWS-RunShellScript',
        Parameters={'commands':[command],'executionTimeout':['90']},Comment='OpenNGS exact-version retention; no source data deletion')
    record={'command_id':response['Command']['CommandId'],'policy':policy,'enabled_by_default':False}
    Path('evidence/deployment/retention-publication.json').write_text(json.dumps(record,indent=2))
    print(json.dumps({'command_id':record['command_id'],'note':'Service installed; administrator enable control remains separate'}))

if __name__=='__main__':
    main()
