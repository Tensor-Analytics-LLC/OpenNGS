"""One bounded, independent scientific environment build; no publication side effect."""
import hashlib
import argparse
import json
from pathlib import Path
import zipfile
from backend.config import aws_session
from backend.runtime_catalog import validate_release

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--refresh',action='store_true');args=parser.parse_args()
    session=aws_session();release=json.loads(Path('evidence/deployment/release.json').read_text())
    base=validate_release(json.loads(Path('evidence/deployment/extended-release.json').read_text()))
    image_base=base['image'];dockerfile='infra/secondary.Dockerfile'
    if args.refresh:
        previous=json.loads(Path('evidence/deployment/secondary-build.json').read_text())
        status=session.client('codebuild').batch_get_builds(ids=[previous['build_id']])['builds'][0]
        if status['buildStatus']!='SUCCEEDED':raise ValueError('Wait for initial environment build')
        published=session.client('ecr').describe_images(repositoryName='openngs-demo',imageIds=[{'imageTag':previous['tag']}])['imageDetails'][0]
        image_base=base['image'].split('@')[0]+'@'+published['imageDigest'];dockerfile='infra/secondary-refresh.Dockerfile'
    recipes=json.loads(Path('infra/secondary-environments.json').read_text())
    archive=Path('evidence/deployment/secondary-build.zip')
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for folder,pattern in [('backend','*.py'),('infra','*.py')]:
            for p in sorted(Path(folder).rglob(pattern)):
                if p.is_file() and not p.is_symlink() and '__pycache__' not in p.parts:z.write(p)
        for name in ['infra/secondary.Dockerfile','infra/secondary-refresh.Dockerfile','infra/secondary-environments.json','scripts/install_secondary_environments.py','scripts/qualify_secondary_runtime.py']:z.write(name)
    sha=hashlib.sha256(archive.read_bytes()).hexdigest();tag='secondary-'+sha[:20];prefix='operations/builds/'+tag
    key=prefix+'/source.zip';output=prefix+'/qualification.json'
    session.client('s3').upload_file(str(archive),release['bucket'],key,ExtraArgs={'ServerSideEncryption':'aws:kms','SSEKMSKeyId':release['kms']})
    project=session.client('codebuild').batch_get_projects(names=['openngs-demo-build'])['projects'][0]
    role=project['serviceRole'].rsplit('/',1)[-1]
    session.client('iam').put_role_policy(RoleName=role,PolicyName='OpenNGSSecondaryBuild',PolicyDocument=json.dumps({'Version':'2012-10-17','Statement':[
        {'Effect':'Allow','Action':['s3:GetObject','s3:GetObjectVersion'],'Resource':'arn:aws:s3:::'+release['bucket']+'/'+key},
        {'Effect':'Allow','Action':['s3:PutObject'],'Resource':['arn:aws:s3:::'+release['bucket']+'/'+output,'arn:aws:s3:::'+release['bucket']+'/'+prefix+'/locks.tar.gz']},
        {'Effect':'Allow','Action':['kms:GenerateDataKey','kms:Decrypt'],'Resource':release['kms'],'Condition':{'StringEquals':{'kms:ViaService':'s3.us-east-1.amazonaws.com'}}}]}))
    buildspec={'version':'0.2','phases':{'pre_build':{'commands':['aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin "$REPOSITORY"']},
      'build':{'commands':['docker build --build-arg "CORE_IMAGE=$CORE_IMAGE" -f "$DOCKERFILE" -t "$REPOSITORY:$IMAGE_TAG" .',
          'CID=$(docker create "$REPOSITORY:$IMAGE_TAG")','docker cp "$CID:/qualification/secondary-report.json" /tmp/qualification.json',
          'mkdir /tmp/locks','for family in analysis annotation happy legacy; do docker cp "$CID:/opt/studio/$family-explicit.lock" /tmp/locks/ || true; done',
          'tar -czf /tmp/locks.tar.gz -C /tmp/locks .','docker rm "$CID"','cat /tmp/qualification.json']},
      'post_build':{'commands':['test "$CODEBUILD_BUILD_SUCCEEDING" = 1','docker push "$REPOSITORY:$IMAGE_TAG"',
          'aws s3 cp /tmp/qualification.json "$QUALIFICATION_URI" --sse aws:kms --sse-kms-key-id "$KMS_KEY"',
          'aws s3 cp /tmp/locks.tar.gz "$LOCK_URI" --sse aws:kms --sse-kms-key-id "$KMS_KEY"']}}}
    variables={'CORE_IMAGE':image_base,'DOCKERFILE':dockerfile,'IMAGE_TAG':tag,'QUALIFICATION_URI':'s3://'+release['bucket']+'/'+output,
        'LOCK_URI':'s3://'+release['bucket']+'/'+prefix+'/locks.tar.gz','KMS_KEY':release['kms']}
    result=session.client('codebuild').start_build(projectName=project['name'],sourceTypeOverride='S3',sourceLocationOverride=release['bucket']+'/'+key,
        buildspecOverride=json.dumps(buildspec),environmentVariablesOverride=[{'name':k,'value':v,'type':'PLAINTEXT'} for k,v in variables.items()],timeoutInMinutesOverride=20)
    record={'family':'secondary-cpu-v1','build_id':result['build']['id'],'source_sha256':sha,'tag':tag,'bucket':release['bucket'],
        'qualification_key':output,'base_image':base['image'],'build_base':image_base,'dockerfile':dockerfile,'environments':recipes,'maximum_build_minutes':20,'maximum_build_usd':.10}
    Path('evidence/deployment/secondary-build.json').write_text(json.dumps(record,indent=2));print(json.dumps(record))

if __name__=='__main__':main()
