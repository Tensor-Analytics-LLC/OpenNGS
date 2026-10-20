"""Launch a bounded offline audit of inherited core tools in an exact image."""
import hashlib
import json
import argparse
from pathlib import Path
import zipfile
from backend.config import aws_session
from backend.runtime_catalog import validate_release

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--adjunct',action='store_true');parser.add_argument('--portable',action='store_true');args=parser.parse_args()
    session=aws_session()
    release=validate_release(json.loads(Path('evidence/deployment/extended-release.json').read_text()))
    deployment=json.loads(Path('evidence/deployment/release.json').read_text())
    name='portable-qualification' if args.portable else 'core-adjunct' if args.adjunct else 'core-qualification'
    qualifier='scripts/qualify_portable_refresh.py' if args.portable else 'scripts/qualify_core_adjunct.py' if args.adjunct else 'scripts/qualify_core_refresh.py'
    archive=Path('evidence/deployment/'+name+'.zip')
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        z.write(qualifier,'qualify_core_refresh.py')
    sha=hashlib.sha256(archive.read_bytes()).hexdigest()
    prefix='operations/builds/'+name+'-'+sha[:20]
    source=prefix+'/source.zip';output=prefix+'/qualification.json'
    session.client('s3').upload_file(str(archive),deployment['bucket'],source,
        ExtraArgs={'ServerSideEncryption':'aws:kms','SSEKMSKeyId':deployment['kms']})
    project=session.client('codebuild').batch_get_projects(names=['openngs-demo-build'])['projects'][0]
    role=project['serviceRole'].rsplit('/',1)[-1]
    session.client('iam').put_role_policy(RoleName=role,PolicyName='OpenNGSCoreQualification',
        PolicyDocument=json.dumps({'Version':'2012-10-17','Statement':[
        {'Effect':'Allow','Action':['s3:GetObject','s3:GetObjectVersion'],'Resource':'arn:aws:s3:::'+deployment['bucket']+'/'+source},
        {'Effect':'Allow','Action':['s3:PutObject'],'Resource':'arn:aws:s3:::'+deployment['bucket']+'/'+output},
        {'Effect':'Allow','Action':['kms:GenerateDataKey','kms:Decrypt'],'Resource':deployment['kms'],
         'Condition':{'StringEquals':{'kms:ViaService':'s3.us-east-1.amazonaws.com'}}}]}))
    document={'version':'0.2','phases':{
        'pre_build':{'commands':['aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin "$REPOSITORY"',
                                 'docker pull "$QUALIFICATION_IMAGE"','mkdir report && chmod 777 report']},
        'build':{'commands':['docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges --user 1000:1000 --cpus 2 --memory 2500m --tmpfs /work:rw,uid=1000,gid=1000,size=256m --tmpfs /inputs:rw,uid=1000,gid=1000,size=128m --tmpfs /tmp:rw,uid=1000,gid=1000,size=256m -v "$CODEBUILD_SRC_DIR/qualify_core_refresh.py:/audit.py:ro" -v "$CODEBUILD_SRC_DIR/report:/audit-report:rw" "$QUALIFICATION_IMAGE" python /audit.py /audit-report/qualification.json']},
        'post_build':{'commands':['test "$CODEBUILD_BUILD_SUCCEEDING" = 1',
            'aws s3 cp report/qualification.json "$QUALIFICATION_URI" --sse aws:kms --sse-kms-key-id "$KMS_KEY"']}}}
    variables={'QUALIFICATION_IMAGE':release['image'],'QUALIFICATION_URI':'s3://'+deployment['bucket']+'/'+output,'KMS_KEY':deployment['kms']}
    response=session.client('codebuild').start_build(projectName=project['name'],sourceTypeOverride='S3',
        sourceLocationOverride=deployment['bucket']+'/'+source,buildspecOverride=json.dumps(document),
        environmentVariablesOverride=[{'name':k,'value':v,'type':'PLAINTEXT'} for k,v in variables.items()],
        timeoutInMinutesOverride=10)
    record={'build_id':response['build']['id'],'image':release['image'],'source_sha256':sha,
            'bucket':deployment['bucket'],'qualification_key':output,'maximum_build_minutes':10}
    Path('evidence/deployment/'+name+'-build.json').write_text(json.dumps(record,indent=2))
    print(json.dumps(record))

if __name__=='__main__':main()
