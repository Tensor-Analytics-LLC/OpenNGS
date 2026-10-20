"""Publish a reviewed source snapshot, verify its digest, then restart only OpenNGS.

Uses short-lived SSM commands and the existing instance role. No credentials are
included in source bundles or command output. Existing databases are preserved.
"""
import hashlib
import json
import tarfile
import time
from pathlib import Path
from backend.config import aws_session

def publish(rebootstrap=False, source_root=None):
    root=Path.cwd();record=json.loads((root/'evidence/deployment/release.json').read_text());s=aws_session()
    source=Path(source_root).resolve() if source_root is not None else root
    outputs={x['OutputKey']:x['OutputValue'] for x in s.client('cloudformation').describe_stacks(StackName='openngs-lean')['Stacks'][0]['Outputs']}
    archive=root/'evidence/deployment/latest-source.tar.gz'
    with tarfile.open(archive,'w:gz') as t:
        for folder in ['backend','frontend','scripts','infra','tests','demo-data']:
            t.add(source/folder,arcname=folder,filter=lambda m:None if '__pycache__' in m.name or m.name.endswith('.pyc') else m)
        for f in ['requirements.lock','README.md','LICENSE']:t.add(source/f,arcname=f)
    digest=hashlib.sha256(archive.read_bytes()).hexdigest();key='operations/releases/'+digest+'/source.tar.gz'
    extra={'ServerSideEncryption':'aws:kms','SSEKMSKeyId':record['kms']}
    s.client('s3').upload_file(str(archive),record['bucket'],key,ExtraArgs=extra)
    commands=['set -euo pipefail','umask 077','export AWS_DEFAULT_REGION=us-east-1 AWS_USE_DUALSTACK_ENDPOINT=true',
        'aws s3 cp s3://'+record['bucket']+'/'+key+' /tmp/openngs-release.tar.gz',
        "echo '"+digest+"  /tmp/openngs-release.tar.gz' | sha256sum -c -",
        'cp -a /opt/openngs/backend /opt/openngs/backend.previous-'+str(int(time.time())),
        'tar -xzf /tmp/openngs-release.tar.gz -C /opt/openngs',
        'chmod -R a+rX /opt/openngs']
    if rebootstrap:
        secret=s.client('rds').describe_db_instances(DBInstanceIdentifier='openngs-lean')['DBInstances'][0]['MasterUserSecret']['SecretArn']
        commands+=['export NGS_BUCKET='+record['bucket'],'export NGS_RELEASE='+record['release'],'export NGS_SOURCE_KEY='+key,
          'set -a; source /var/lib/openngs/runtime.env; set +a',
          "export NGS_DB_HOST=$("+"/opt/openngs/runtime/bin/python -c \"import os; from urllib.parse import urlsplit; print(urlsplit(os.environ['NGS_DATABASE_URL']).hostname)\")",
          'export NGS_DB_SECRET='+secret,'export NGS_KMS_KEY='+record['kms'],
          'bash /opt/openngs/infra/bootstrap.sh']
    else:commands+=['systemctl restart openngs']
    response=s.client('ssm').send_command(InstanceIds=[outputs['HostId']],DocumentName='AWS-RunShellScript',Parameters={'commands':['\n'.join(commands)],'executionTimeout':['900']},Comment='OpenNGS verified source release '+digest[:12])
    result={'source_sha256':digest,'source_key':key,'command_id':response['Command']['CommandId'],'started':time.time()}
    (root/'evidence/deployment/latest-release.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))

if __name__=='__main__':
    import sys
    publish('--rebootstrap' in sys.argv)
