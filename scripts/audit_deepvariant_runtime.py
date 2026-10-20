"""Refresh the scratch-safe wrapper and qualify its exact image in isolation.

Packages locally unless --start and a funded reservation are supplied. One
MEDIUM CodeBuild execution is capped at ten minutes/$0.10; no routes publish.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile
from backend.config import aws_session
from backend.deepvariant_runtime import validate_release


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--start',action='store_true')
    parser.add_argument('--reservation')
    args=parser.parse_args()
    candidate=validate_release(json.loads(Path('evidence/deployment/deepvariant-candidate.json').read_text()),require_isolation=False)
    if args.start and not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}',args.reservation or ''):
        raise ValueError('--start requires a funded reservation identifier')
    archive=Path('.state/deepvariant-build/audit.zip')
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as target:
        for name in ['infra/deepvariant-refresh.Dockerfile','backend/deepvariant_tools.py','backend/deepvariant_runtime.py','scripts/qualify_deepvariant_runtime.py','scripts/audit_deepvariant_runtime.py']:
            target.write(name)
    sha=hashlib.sha256(archive.read_bytes()).hexdigest()
    tag='deepvariant-audit-'+sha[:20]
    prefix='operations/builds/'+tag
    record={'candidate_image':candidate['image'],'source_sha256':sha,'tag':tag,
            'maximum_build_minutes':10,'maximum_build_usd':.10,'compute':'BUILD_GENERAL1_MEDIUM'}
    if not args.start:
        print(json.dumps({**record,'status':'packaged-locally','archive_bytes':archive.stat().st_size}))
        return
    session=aws_session()
    deployment=json.loads(Path('evidence/deployment/release.json').read_text())
    bucket=deployment['bucket']
    source=prefix+'/source.zip';proof=prefix+'/qualification.json';outputs=prefix+'/outputs.tar.gz'
    session.client('s3').upload_file(str(archive),bucket,source,ExtraArgs={
        'ServerSideEncryption':'aws:kms','SSEKMSKeyId':deployment['kms'],
        'Tagging':'RuntimeFamily=deepvariant-cpu-v1&CostReservation='+args.reservation,
        'Metadata':{'sha256':sha}})
    project=session.client('codebuild').batch_get_projects(names=['openngs-demo-build'])['projects'][0]
    role=project['serviceRole'].rsplit('/',1)[-1]
    session.client('iam').put_role_policy(RoleName=role,PolicyName='OpenNGSDeepVariantAudit',PolicyDocument=json.dumps({
        'Version':'2012-10-17','Statement':[
            {'Effect':'Allow','Action':['s3:GetObject','s3:GetObjectVersion'],'Resource':'arn:aws:s3:::'+bucket+'/'+source},
            {'Effect':'Allow','Action':['s3:PutObject'],'Resource':['arn:aws:s3:::'+bucket+'/'+proof,'arn:aws:s3:::'+bucket+'/'+outputs]},
            {'Effect':'Allow','Action':['kms:GenerateDataKey','kms:Decrypt'],'Resource':deployment['kms'],
             'Condition':{'StringEquals':{'kms:ViaService':'s3.us-east-1.amazonaws.com'}}}]}))
    # Fargate's ordinary ephemeral /tmp volume permits shared-library mmap.
    # Docker tmpfs defaults to noexec; Bazel's bundled native modules need exec.
    command='docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges --user 1000:1000 --cpus 2 --memory 4096m --tmpfs /work:rw,uid=1000,gid=1000,size=768m --tmpfs /inputs:rw,uid=1000,gid=1000,size=128m --tmpfs /tmp:rw,exec,uid=1000,gid=1000,size=1024m -e OPENNGS_DEEPVARIANT_QUALIFICATION_ROOT=/tmp/deepvariant-audit -v "$CODEBUILD_SRC_DIR/report:/audit-report:rw" "$REPOSITORY:$IMAGE_TAG" /usr/bin/python3 /opt/studio/qualify_deepvariant_runtime.py /audit-report/qualification.json'
    spec={'version':'0.2','phases':{
        'pre_build':{'commands':['aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin "$REPOSITORY"','mkdir report && chmod 777 report']},
        'build':{'commands':[
            'docker build --platform linux/amd64 --build-arg "CANDIDATE_IMAGE=$CANDIDATE_IMAGE" -f infra/deepvariant-refresh.Dockerfile -t "$REPOSITORY:$IMAGE_TAG" .',
            command,
            "python -c 'import json; r=json.load(open(\"report/qualification.json\")); assert len(r[\"cases\"])==6 and all(c[\"status\"]==\"passed\" for c in r[\"cases\"]),r[\"cases\"]'",
            'tar -czf /tmp/deepvariant-outputs.tar.gz -C report calls.vcf.gz calls.vcf.gz.tbi calls.runtime.json positive.log',
            'cat report/qualification.json']},
        'post_build':{'commands':['test "$CODEBUILD_BUILD_SUCCEEDING" = 1','docker push "$REPOSITORY:$IMAGE_TAG"',
            'aws s3 cp report/qualification.json "$QUALIFICATION_URI" --sse aws:kms --sse-kms-key-id "$KMS_KEY"',
            'aws s3 cp /tmp/deepvariant-outputs.tar.gz "$OUTPUT_URI" --sse aws:kms --sse-kms-key-id "$KMS_KEY"']}}}
    variables={'CANDIDATE_IMAGE':candidate['image'],'IMAGE_TAG':tag,'QUALIFICATION_URI':'s3://'+bucket+'/'+proof,
               'OUTPUT_URI':'s3://'+bucket+'/'+outputs,'KMS_KEY':deployment['kms']}
    build=session.client('codebuild').start_build(projectName=project['name'],sourceTypeOverride='S3',sourceLocationOverride=bucket+'/'+source,
        buildspecOverride=json.dumps(spec),computeTypeOverride='BUILD_GENERAL1_MEDIUM',timeoutInMinutesOverride=10,
        environmentVariablesOverride=[{'name':k,'value':v,'type':'PLAINTEXT'} for k,v in variables.items()])
    record.update(build_id=build['build']['id'],bucket=bucket,qualification_key=proof,artifacts_key=outputs,reservation=args.reservation)
    Path('evidence/deployment/deepvariant-audit-build.json').write_text(json.dumps(record,indent=2))
    print(json.dumps(record))


if __name__=='__main__':main()
