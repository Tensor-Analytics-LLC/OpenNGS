"""Reconcile reviewed OpenNGS service settings after a verified source release.

Infrastructure-operator command. Does not import/replace application data or
alter any independent project's resources. Existing secret settings are retained.
"""
import json
from pathlib import Path
from backend.config import aws_session

def main():
    s=aws_session();cf=s.client('cloudformation');stack=cf.describe_stacks(StackName='openngs-lean')['Stacks'][0]
    outputs={x['OutputKey']:x['OutputValue'] for x in stack['Outputs']};host=outputs['HostId'];ec=s.client('ec2')
    instance=ec.describe_instances(InstanceIds=[host])['Reservations'][0]['Instances'][0]
    groups=ec.describe_security_groups(Filters=[{'Name':'vpc-id','Values':[instance['VpcId']]}])['SecurityGroups']
    managed=[g for g in groups if g['GroupName'].startswith('CloudFront-VPCOrigins-Service-SG')]
    if len(managed)!=1:raise ValueError('Expected exactly one CloudFront VPC-origin managed group in the OpenNGS VPC')
    for sg in instance['SecurityGroups']:
        try:ec.authorize_security_group_ingress(GroupId=sg['GroupId'],IpPermissions=[{'IpProtocol':'tcp','FromPort':8080,'ToPort':8080,'UserIdGroupPairs':[{'GroupId':managed[0]['GroupId'],'Description':'Only CloudFront managed VPC origin'}]}])
        except ec.exceptions.ClientError as e:
            if e.response['Error']['Code']!='InvalidPermission.Duplicate':raise
    research=json.loads(Path('evidence/deployment/research-capacity.json').read_text());account=s.client('sts').get_caller_identity()['Account']
    env={'NGS_RESEARCH_TEMPLATE_URL':research['template_url'],'NGS_RESEARCH_CFN_ROLE':research['cfn_role'],'NGS_PRO_BUDGET_TOPIC':research['budget_topic'],'NGS_AUDIT_BUCKET':'openngs-audit-'+account+'-us-east-1'}
    budget={x['OutputKey']:x['OutputValue'] for x in cf.describe_stacks(StackName='openngs-budget')['Stacks'][0]['Outputs']}
    env.update(NGS_BUDGET_QUEUE_URL=budget['QueueURL'],NGS_BUDGET_WARNING_TOPIC=budget['WarningTopic'],NGS_BUDGET_CRITICAL_TOPIC=budget['CriticalTopic'])
    script="set -euo pipefail\nset -a; source /var/lib/openngs/runtime.env; set +a\ncd /opt/openngs\n/opt/openngs/runtime/bin/python - <<'REMOTE'\n"
    script+="from pathlib import Path\nimport json\nfrom backend import db\nfrom backend.postgres import close\nchanges="+repr(env)+"\np=Path('/var/lib/openngs/runtime.env')\nrows=[r for r in p.read_text().splitlines() if r.partition('=')[0] not in changes]\nrows.extend(k+'='+v for k,v in changes.items())\np.write_text('\\n'.join(rows)+'\\n');p.chmod(0o600)\n"
    script+="with db.tx() as c:\n c.execute('INSERT INTO controls VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',('research-runtime-release',db.encode("+repr({'image':research['runtime_image'],'build':research['runtime_build'],'status':'Version checks; scientific qualification is separate'})+")))\n db.audit(c,'deployment-operator','deployment.services.configured','OpenNGS',{'secrets_preserved':True,'data_reset':False})\nclose()\nREMOTE\n"
    script+="cp /opt/openngs/infra/openngs-{demo,verifier,research,billing,audit}.service /etc/systemd/system/\nsystemctl daemon-reload\nsystemctl enable openngs-{demo,verifier,research,billing,audit}\nsystemctl restart openngs openngs-{demo,verifier,research,billing,audit}\nsystemctl is-active openngs openngs-{demo,verifier,research,billing,audit}\n"
    response=s.client('ssm').send_command(InstanceIds=[host],DocumentName='AWS-RunShellScript',Parameters={'commands':[script],'executionTimeout':['120']},Comment='OpenNGS reconcile reviewed service settings; preserve accounts and Vault')
    result={'command_id':response['Command']['CommandId'],'host':host};Path('evidence/deployment/services-reconciled.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))

if __name__=='__main__':main()
