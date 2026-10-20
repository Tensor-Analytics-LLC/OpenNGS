"""Audit the exact optional image with production-like fresh writable mounts."""
import hashlib
import json
from pathlib import Path
import zipfile
from backend.config import aws_session

def main():
    s=aws_session();build=json.loads(Path('evidence/deployment/secondary-build.json').read_text())
    status=s.client('codebuild').batch_get_builds(ids=[build['build_id']])['builds'][0]
    if status['buildStatus']!='SUCCEEDED':raise ValueError('Environment image build must finish first')
    image=s.client('ecr').describe_images(repositoryName='openngs-demo',imageIds=[{'imageTag':build['tag']}])['imageDetails'][0]
    digest=build['base_image'].split('@')[0]+'@'+image['imageDigest']
    deploy=json.loads(Path('evidence/deployment/release.json').read_text())
    archive=Path('evidence/deployment/secondary-audit.zip')
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:z.write('scripts/qualify_secondary_runtime.py','audit.py')
    sha=hashlib.sha256(archive.read_bytes()).hexdigest();prefix='operations/builds/secondary-audit-'+sha[:20]
    key=prefix+'/source.zip';output=prefix+'/qualification.json'
    s.client('s3').upload_file(str(archive),deploy['bucket'],key,ExtraArgs={'ServerSideEncryption':'aws:kms','SSEKMSKeyId':deploy['kms']})
    project=s.client('codebuild').batch_get_projects(names=['openngs-demo-build'])['projects'][0];role=project['serviceRole'].rsplit('/',1)[-1]
    s.client('iam').put_role_policy(RoleName=role,PolicyName='OpenNGSSecondaryAudit',PolicyDocument=json.dumps({'Version':'2012-10-17','Statement':[
        {'Effect':'Allow','Action':['s3:GetObject','s3:GetObjectVersion'],'Resource':'arn:aws:s3:::'+deploy['bucket']+'/'+key},
        {'Effect':'Allow','Action':['s3:PutObject'],'Resource':'arn:aws:s3:::'+deploy['bucket']+'/'+output},
        {'Effect':'Allow','Action':['kms:GenerateDataKey','kms:Decrypt'],'Resource':deploy['kms'],'Condition':{'StringEquals':{'kms:ViaService':'s3.us-east-1.amazonaws.com'}}}]}))
    spec={'version':'0.2','phases':{'pre_build':{'commands':['aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin "$REPOSITORY"','docker pull "$QUALIFICATION_IMAGE"','mkdir report && chmod 777 report']},
        'build':{'commands':['docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges --user 1000:1000 --cpus 2 --memory 2500m --tmpfs /work:rw,uid=1000,gid=1000,size=512m --tmpfs /inputs:rw,uid=1000,gid=1000,size=128m --tmpfs /tmp:rw,uid=1000,gid=1000,size=512m -v "$CODEBUILD_SRC_DIR/audit.py:/audit.py:ro" -v "$CODEBUILD_SRC_DIR/report:/audit-report:rw" "$QUALIFICATION_IMAGE" python /audit.py /audit-report/qualification.json']},
        'post_build':{'commands':['test "$CODEBUILD_BUILD_SUCCEEDING" = 1','aws s3 cp report/qualification.json "$QUALIFICATION_URI" --sse aws:kms --sse-kms-key-id "$KMS_KEY"']}}}
    env={'QUALIFICATION_IMAGE':digest,'QUALIFICATION_URI':'s3://'+deploy['bucket']+'/'+output,'KMS_KEY':deploy['kms']}
    response=s.client('codebuild').start_build(projectName=project['name'],sourceTypeOverride='S3',sourceLocationOverride=deploy['bucket']+'/'+key,
        buildspecOverride=json.dumps(spec),environmentVariablesOverride=[{'name':k,'value':v,'type':'PLAINTEXT'} for k,v in env.items()],timeoutInMinutesOverride=15)
    record={'build_id':response['build']['id'],'image':digest,'source_sha256':sha,'bucket':deploy['bucket'],'qualification_key':output,'maximum_build_minutes':15}
    Path('evidence/deployment/secondary-audit-build.json').write_text(json.dumps(record,indent=2));print(json.dumps(record))

if __name__=='__main__':main()
