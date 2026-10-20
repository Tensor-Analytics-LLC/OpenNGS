"""Bounded AWS-only extension build with real scientific qualification."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile
from backend.config import aws_session
from backend.runtime_catalog import EXTENDED_PACKAGES,validate_release

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--max-minutes',type=int,default=20,choices=range(5,21))
    parser.add_argument('--refresh',action='store_true',help='Reuse an existing qualified immutable environment and rerun all execution cases')
    args=parser.parse_args()
    session=aws_session()
    release=json.loads(Path('evidence/deployment/release.json').read_text())
    capacity=json.loads(Path('evidence/deployment/research-capacity.json').read_text())
    core=capacity['runtime_image']
    if '@sha256:' not in core:raise ValueError('Existing qualified core image must be digest pinned')
    base=core
    dockerfile='infra/extended.Dockerfile'
    if args.refresh:
        previous=validate_release(json.loads(Path('evidence/deployment/extended-release.json').read_text()))
        if previous.get('core_image')!=core:raise ValueError('Refresh requires the unchanged qualified core image')
        base=previous['image'];dockerfile='infra/extended-refresh.Dockerfile'
    written=dict(line.strip().split('=') for line in Path('infra/extended-packages.txt').read_text().splitlines() if line.strip())
    if written!=EXTENDED_PACKAGES:raise ValueError('Pinned environment and reviewed catalogue differ')
    archive=Path('evidence/deployment/extended-build.zip')
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for folder,pattern in [('backend','*.py'),('infra','*.py'),('demo-data','*')]:
            for p in sorted(Path(folder).rglob(pattern)):
                if p.is_file() and not p.is_symlink() and '__pycache__' not in p.parts:z.write(p)
        for name in ['infra/extended.Dockerfile','infra/extended-refresh.Dockerfile','infra/extended-packages.txt','scripts/qualify_extended_runtime.py']:z.write(name)
    digest=hashlib.sha256(archive.read_bytes()).hexdigest()
    tag='extended-'+digest[:20];key='operations/builds/'+tag+'/source.zip'
    session.client('s3').upload_file(str(archive),release['bucket'],key,ExtraArgs={'ServerSideEncryption':'aws:kms','SSEKMSKeyId':release['kms']})
    result_key='operations/builds/'+tag+'/qualification.json'
    project=session.client('codebuild').batch_get_projects(names=['openngs-demo-build'])['projects'][0]
    role=project['serviceRole'].rsplit('/',1)[-1]
    # Only this source archive and its two bounded evidence artifacts are writable.
    session.client('iam').put_role_policy(RoleName=role,PolicyName='OpenNGSExtendedBuild',
        PolicyDocument=json.dumps({'Version':'2012-10-17','Statement':[
            {'Effect':'Allow','Action':['s3:GetObject','s3:GetObjectVersion'],'Resource':'arn:aws:s3:::'+release['bucket']+'/'+key},
            {'Effect':'Allow','Action':['s3:PutObject'],'Resource':['arn:aws:s3:::'+release['bucket']+'/'+result_key,'arn:aws:s3:::'+release['bucket']+'/'+result_key.replace('qualification.json','extended-explicit.lock'),'arn:aws:s3:::'+release['bucket']+'/'+result_key.replace('qualification.json','cnvkit-explicit.lock')]},
            {'Effect':'Allow','Action':['kms:GenerateDataKey','kms:Decrypt'],'Resource':release['kms'],'Condition':{'StringEquals':{'kms:ViaService':'s3.us-east-1.amazonaws.com'}}}
        ]}))
    document={'version':'0.2','phases':{
        'pre_build':{'commands':['aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin "$REPOSITORY"']},
        'build':{'commands':['docker build --build-arg "CORE_IMAGE=$CORE_IMAGE" -f "$DOCKERFILE" -t "$REPOSITORY:$IMAGE_TAG" .',
                             'CID=$(docker create "$REPOSITORY:$IMAGE_TAG")',
                             'docker cp "$CID:/qualification/report.json" /tmp/qualification.json',
                             'docker cp "$CID:/opt/studio/extended-explicit.lock" /tmp/extended-explicit.lock',
                             'docker cp "$CID:/opt/studio/cnvkit-explicit.lock" /tmp/cnvkit-explicit.lock',
                             'docker rm "$CID"',
                             'cat /tmp/qualification.json']},
        'post_build':{'commands':['test "$CODEBUILD_BUILD_SUCCEEDING" = 1',
                                  'docker push "$REPOSITORY:$IMAGE_TAG"',
                                  'aws s3 cp /tmp/qualification.json "$QUALIFICATION_URI" --sse aws:kms --sse-kms-key-id "$KMS_KEY"',
                                  'aws s3 cp /tmp/extended-explicit.lock "$LOCK_URI" --sse aws:kms --sse-kms-key-id "$KMS_KEY"',
                                  'aws s3 cp /tmp/cnvkit-explicit.lock "$CNVKIT_LOCK_URI" --sse aws:kms --sse-kms-key-id "$KMS_KEY"']}}}
    variables={'IMAGE_TAG':tag,'CORE_IMAGE':base,'DOCKERFILE':dockerfile,'QUALIFICATION_URI':'s3://'+release['bucket']+'/'+result_key,
               'LOCK_URI':'s3://'+release['bucket']+'/'+result_key.replace('qualification.json','extended-explicit.lock'),
               'CNVKIT_LOCK_URI':'s3://'+release['bucket']+'/'+result_key.replace('qualification.json','cnvkit-explicit.lock'),'KMS_KEY':release['kms']}
    response=session.client('codebuild').start_build(projectName='openngs-demo-build',sourceTypeOverride='S3',
        sourceLocationOverride=release['bucket']+'/'+key,buildspecOverride=json.dumps(document),
        environmentVariablesOverride=[{'name':k,'value':v,'type':'PLAINTEXT'} for k,v in variables.items()],
        timeoutInMinutesOverride=args.max_minutes)
    record={'family':'extended-cpu-v1','build_id':response['build']['id'],'source_sha256':digest,
            'source_key':key,'tag':tag,'qualification_key':result_key,'bucket':release['bucket'],
            'core_image':core,'build_base':base,'dockerfile':dockerfile,'maximum_build_minutes':args.max_minutes,'packages':EXTENDED_PACKAGES}
    Path('evidence/deployment/extended-build.json').write_text(json.dumps(record,indent=2))
    print(json.dumps(record))

if __name__=='__main__':main()
