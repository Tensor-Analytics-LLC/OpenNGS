"""Prepare eight format controls; --start requires a reviewed funded reservation.

Default operation is local packaging only. One existing CodeBuild MEDIUM build
has a hard ten-minute/$0.10 ceiling. It creates a candidate, never public routes.
"""
import argparse
import base64
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
import zipfile

from backend.format_contract import BASE_IMAGE,FAMILY,SOURCE_FILES,TOOLS,VERSION
from scripts.qualify_portable_formats import fixture_files


def sha(raw):return hashlib.sha256(raw).hexdigest()


def buildspec():
    return {'version':'0.2','phases':{
        'pre_build':{'commands':['aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin "$REPOSITORY"']},
        'build':{'commands':[
            'docker build --platform linux/amd64 --network none --build-arg "FORMAT_BASE_IMAGE=$FORMAT_BASE_IMAGE" -f infra/portable-formats.Dockerfile -t "$REPOSITORY:$IMAGE_TAG" .',
            'mkdir -p /tmp/format-work && chmod 777 /tmp/format-work',
            'docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges --user 1000:1000 --cpus 2 --memory 2500m --pids-limit 128 --tmpfs /tmp:rw,exec,uid=1000,gid=1000,size=128m -v /tmp/format-work:/work:rw -v "$CODEBUILD_SRC_DIR/fixtures:/inputs/fixtures:ro" "$REPOSITORY:$IMAGE_TAG" python /opt/studio/qualify_portable_formats.py']},
        'post_build':{'commands':[
            'if test -d /tmp/format-work; then tar -czf /tmp/format-outputs.tar.gz -C /tmp/format-work .; aws s3 cp /tmp/format-outputs.tar.gz "$OUTPUT_URI" --sse aws:kms --sse-kms-key-id "$KMS_KEY"; fi',
            'if test -f /tmp/format-work/qualification.json; then aws s3 cp /tmp/format-work/qualification.json "$QUALIFICATION_URI" --sse aws:kms --sse-kms-key-id "$KMS_KEY"; fi',
            'test "$CODEBUILD_BUILD_SUCCEEDING" = 1','docker push "$REPOSITORY:$IMAGE_TAG"']}}}


def spec_bytes():return json.dumps(buildspec(),sort_keys=True,separators=(',',':')).encode()


def package(directory):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    fixture_manifest=fixture_files(directory/'fixtures')
    sources=[{'path':name,'sha256':sha(Path(name).read_bytes()),'bytes':Path(name).stat().st_size} for name in SOURCE_FILES]
    members={name:Path(name).read_bytes() for name in SOURCE_FILES}
    members.update({'fixtures/'+row['name']:(directory/'fixtures'/row['name']).read_bytes() for row in fixture_manifest})
    members['source-manifest.json']=json.dumps({'schema':1,'sources':sources,'fixtures':fixture_manifest},sort_keys=True).encode()
    members['buildspec.yml']=spec_bytes()
    archive=directory/'source.zip'
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as target:
        for name,raw in sorted(members.items()):
            item=zipfile.ZipInfo(name,(1980,1,1,0,0,0));item.external_attr=0o100644<<16;item.compress_type=zipfile.ZIP_DEFLATED
            target.writestr(item,raw)
    if archive.stat().st_size>1024**2:raise ValueError('Format source exceeds the reviewed one MiB ceiling')
    source_sha=sha(archive.read_bytes());tag='formats-'+source_sha[:20]
    record={'schema':1,'family':FAMILY,'version':VERSION,'base_image':BASE_IMAGE,'tools':sorted(TOOLS),
        'source_sha256':source_sha,'source_files':sources,'fixture_files':fixture_manifest,
        'archive_bytes':archive.stat().st_size,'tag':tag,'buildspec_sha256':sha(spec_bytes()),
        'compute':'BUILD_GENERAL1_MEDIUM','maximum_build_minutes':10,'maximum_build_usd':.10,
        'maximum_new_layer_compressed_bytes':16*1024**2,'maximum_incremental_monthly_ecr_usd':.002}
    (directory/'buildspec.json').write_bytes(spec_bytes())
    (directory/'source-manifest.json').write_text(json.dumps(record,indent=2))
    return archive,record


def require_idle(client):
    ids=client.list_builds_for_project(projectName='openngs-demo-build',sortOrder='DESCENDING').get('ids',[])[:20]
    if ids and any(b.get('buildStatus') in {'IN_PROGRESS','STOPPING'} for b in client.batch_get_builds(ids=ids)['builds']):
        raise ValueError('The existing build project is busy; preserve the running acquisition or worker build')


def allow_exact_acquisition(client,project,session,build_id,reservation):
    """Allow only one explicitly named, immutable non-Docker acquisition beside us."""
    record=json.loads(Path('evidence/deployment/vep-direct-acquisition-build.json').read_text())
    if (record.get('build_id')!=build_id or record.get('reservation')!=reservation
            or record.get('privileged') is not False or record.get('maximum_build_minutes')!=45
            or record.get('service_role') not in {project['serviceRole'],project['serviceRole'].rsplit('/',1)[-1]}):
        raise ValueError('Concurrent build must be the exact reviewed acquisition under the same funded hold')
    ids=client.list_builds_for_project(projectName=project['name'],sortOrder='DESCENDING').get('ids',[])[:100]
    active=[b for b in client.batch_get_builds(ids=ids)['builds'] if b.get('buildStatus') in {'IN_PROGRESS','STOPPING'}] if ids else []
    if not active:return {'policy':'idle-project','reviewed_acquisition':build_id}
    if len(active)!=1 or active[0]['id']!=build_id:raise ValueError('Only the one explicitly reviewed acquisition may overlap the format audit')
    observed=active[0];source=record['source'];environment=observed.get('environment',{})
    if (environment.get('privilegedMode') is not False or environment.get('computeType')!='BUILD_GENERAL1_MEDIUM'
            or observed.get('timeoutInMinutes')!=45 or observed.get('serviceRole')!=project['serviceRole']
            or observed.get('sourceVersion')!=source['version_id'] or observed.get('source',{}).get('type')!='S3'
            or observed['source'].get('location')!=source['bucket']+'/'+source['key']
            or sha(observed['source'].get('buildspec','').encode())!=source['buildspec_sha256']
            or not source['key'].startswith('operations/builds/exact-vep-')):
        raise ValueError('Running acquisition differs from the exact nonprivileged immutable build contract')
    head=session.client('s3').head_object(Bucket=source['bucket'],Key=source['key'],VersionId=source['version_id'],ChecksumMode='ENABLED')
    if (head.get('VersionId')!=source['version_id'] or head.get('ContentLength')!=source['bytes']
            or head.get('Metadata',{}).get('sha256')!=source['sha256']
            or head.get('ChecksumSHA256')!=base64.b64encode(bytes.fromhex(source['sha256'])).decode()):
        raise ValueError('Concurrent acquisition source immutable identity differs')
    quota=session.client('service-quotas').get_service_quota(ServiceCode='codebuild',QuotaCode='L-2DC20C30')['Quota']
    if quota.get('Value',0)<2:raise ValueError('The applied regional Linux/Medium quota must permit two concurrent builds')
    limit=project.get('concurrentBuildLimit')
    if limit is not None and limit<2:raise ValueError('The project concurrent build limit does not permit two builds')
    return {'policy':'exact-nonprivileged-acquisition-plus-one-format-audit','reviewed_acquisition':build_id,
            'acquisition_source_version':source['version_id'],'acquisition_source_sha256':source['sha256'],
            'project_concurrent_build_limit':limit,'regional_medium_quota':quota['Value'],'maximum_project_builds':2}


def atomic_record(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w',prefix='.formats-',dir=path.parent,delete=False) as handle:
        temporary=Path(handle.name);os.chmod(temporary,0o600)
        json.dump(value,handle,indent=2);handle.flush();os.fsync(handle.fileno())
    os.replace(temporary,path)


def find_launch(client,token):
    response=client.list_builds_for_project(projectName='openngs-demo-build',sortOrder='DESCENDING');ids=response.get('ids',[])
    jobs=client.batch_get_builds(ids=ids[:100])['builds'] if ids else []
    found=[b for b in jobs if any(v.get('name')=='FORMAT_LAUNCH_TOKEN' and v.get('value')==token
                                for v in b.get('environment',{}).get('environmentVariables',[]))]
    if len(found)>1:raise ValueError('Multiple builds match one format launch intent; operator reconciliation is required')
    return found[0] if found else None


def launch_once(client,request,record,path,now=time.time,pre_start=lambda:{}):
    """Persist intent before the non-idempotent boundary, then reconcile ambiguity."""
    token=sha(json.dumps({key:record[key] for key in ['source_version','source_sha256','reservation']},sort_keys=True).encode())
    variables=request.get('environmentVariablesOverride',[])
    if any(row.get('name')=='FORMAT_LAUNCH_TOKEN' for row in variables):raise ValueError('Launch token is controlled by the persistent operator intent')
    request={**request,'idempotencyToken':token,'environmentVariablesOverride':[
        *variables,{'name':'FORMAT_LAUNCH_TOKEN','value':token,'type':'PLAINTEXT'}]}
    request_sha=sha(json.dumps(request,sort_keys=True,separators=(',',':')).encode())
    path=Path(path)
    if path.exists():
        previous=json.loads(path.read_text())
        if previous.get('idempotency_token')!=token or previous.get('request_sha256')!=request_sha:
            raise ValueError('A different persistent format launch intent exists; preserve it for operator review')
        if previous.get('build_id'):return previous
        observed=find_launch(client,token)
        if observed:
            if (observed.get('sourceVersion')!=record['source_version']
                    or observed.get('source',{}).get('type')!='S3'
                    or observed['source'].get('location')!=record['bucket']+'/'+record['source_key']
                    or observed['source'].get('buildspec')!=request['buildspecOverride']
                    or observed.get('timeoutInMinutes')!=10
                    or observed.get('environment',{}).get('computeType')!='BUILD_GENERAL1_MEDIUM'):
                raise ValueError('Reconciled format build differs from its exact persistent source intent')
            previous.update(state='launched',build_id=observed['id'],reconciled=True)
            atomic_record(path,previous);return previous
        if now()-previous['created']>=240:
            raise ValueError('Ambiguous format launch is outside the safe token retry window; reconcile it without starting another build')
        intent=previous
    else:
        intent={**record,'state':'launch-intent','created':now(),'idempotency_token':token,'request_sha256':request_sha}
    intent.update(pre_start());atomic_record(path,intent)
    try:response=client.start_build(**request)
    except Exception as error:
        intent.update(state='launch-outcome-unknown',last_error=type(error).__name__);atomic_record(path,intent);raise
    intent.update(state='launched',build_id=response['build']['id']);atomic_record(path,intent);return intent


@contextmanager
def operator_lock(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    descriptor=os.open(path,os.O_RDWR|os.O_CREAT,0o600)
    try:
        try:fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as error:raise ValueError('Another local format launch controller owns this intent') from error
        yield
    finally:
        fcntl.flock(descriptor,fcntl.LOCK_UN);os.close(descriptor)


def put_source(client,bucket,key,raw,kms):
    checksum=base64.b64encode(hashlib.sha256(raw).digest()).decode()
    try:
        response=client.put_object(Bucket=bucket,Key=key,Body=raw,IfNoneMatch='*',
            ChecksumSHA256=checksum,ServerSideEncryption='aws:kms',SSEKMSKeyId=kms,Metadata={'sha256':sha(raw)})
        version=response.get('VersionId')
    except Exception as error:
        if getattr(error,'response',{}).get('Error',{}).get('Code') not in {'412','PreconditionFailed'}:raise
        head=client.head_object(Bucket=bucket,Key=key,ChecksumMode='ENABLED')
        if head.get('ContentLength')!=len(raw) or head.get('ChecksumSHA256')!=checksum or head.get('Metadata',{}).get('sha256')!=sha(raw):
            raise ValueError('Existing format build source has a different immutable identity') from error
        version=head.get('VersionId')
    if not version or version=='null':raise ValueError('An immutable source object version is required')
    return version


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--directory',default='.state/portable-formats-build')
    parser.add_argument('--start',action='store_true');parser.add_argument('--reservation')
    parser.add_argument('--concurrent-acquisition-build',help='Permit only this exact reviewed nonprivileged VEP acquisition alongside one format audit')
    args=parser.parse_args()
    if args.start and not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}',args.reservation or ''):
        raise ValueError('--start requires the operator funded cost reservation identifier')
    archive,record=package(args.directory)
    if not args.start:
        print(json.dumps({**record,'status':'packaged-locally','aws_mutations':False},indent=2));return
    from backend.config import aws_session
    deployment=json.loads(Path('evidence/deployment/release.json').read_text());session=aws_session()
    if session.region_name!='us-east-1' or session.client('sts').get_caller_identity()['Account']!='509505638328':
        raise ValueError('Only the reviewed AWS account and us-east-1 region may build this candidate')
    with operator_lock('evidence/deployment/.portable-formats-launch.lock'):
        launched=start_packaged(session,deployment,archive,record,args.reservation,args.concurrent_acquisition_build)
    print(json.dumps(launched,indent=2))


def start_packaged(session,deployment,archive,record,reservation,concurrent_build=None):
    cb=session.client('codebuild')
    project=cb.batch_get_projects(names=['openngs-demo-build'])['projects'][0]
    if (not project['environment'].get('privilegedMode') or project['environment'].get('image')!='aws/codebuild/standard:7.0'
            or project['environment'].get('type')!='LINUX_CONTAINER'):
        raise ValueError('The existing reviewed Docker build project is required')
    bucket=deployment['bucket'];prefix='operations/builds/'+record['tag'];source_key=prefix+'/source.zip'
    report_key=prefix+'/qualification.json';artifacts_key=prefix+'/outputs.tar.gz'
    source_version=put_source(session.client('s3'),bucket,source_key,archive.read_bytes(),deployment['kms'])
    role=project['serviceRole'].rsplit('/',1)[-1]
    policy=json.dumps({
        'Version':'2012-10-17','Statement':[
            {'Effect':'Allow','Action':['s3:GetObject','s3:GetObjectVersion'],'Resource':'arn:aws:s3:::'+bucket+'/'+source_key},
            {'Effect':'Allow','Action':['s3:PutObject'],'Resource':['arn:aws:s3:::'+bucket+'/'+key for key in [report_key,artifacts_key]]},
            {'Effect':'Allow','Action':['kms:GenerateDataKey','kms:Decrypt'],'Resource':deployment['kms'],
             'Condition':{'StringEquals':{'kms:ViaService':'s3.us-east-1.amazonaws.com'}}}]})
    variables={'FORMAT_BASE_IMAGE':BASE_IMAGE,'IMAGE_TAG':record['tag'],'QUALIFICATION_URI':'s3://'+bucket+'/'+report_key,
               'OUTPUT_URI':'s3://'+bucket+'/'+artifacts_key,'KMS_KEY':deployment['kms']}
    request=dict(projectName=project['name'],sourceTypeOverride='S3',sourceLocationOverride=bucket+'/'+source_key,
        sourceVersion=source_version,buildspecOverride=spec_bytes().decode(),computeTypeOverride=record['compute'],
        timeoutInMinutesOverride=10,privilegedModeOverride=True,
        environmentVariablesOverride=[{'name':k,'value':v,'type':'PLAINTEXT'} for k,v in variables.items()])
    record={**record,'bucket':bucket,'source_key':source_key,'source_version':source_version,
        'qualification_key':report_key,'artifacts_key':artifacts_key,'reservation':reservation,'service_role':project['serviceRole']}
    def pre_start():
        if concurrent_build:audit=allow_exact_acquisition(cb,project,session,concurrent_build,reservation)
        else:require_idle(cb);audit={'policy':'idle-project'}
        session.client('iam').put_role_policy(RoleName=role,PolicyName='OpenNGSFormatBuild',PolicyDocument=policy)
        return {'concurrency':audit}
    launched=launch_once(cb,request,record,'evidence/deployment/portable-formats-launch-intent.json',pre_start=pre_start)
    atomic_record('evidence/deployment/portable-formats-build.json',launched);return launched


if __name__=='__main__':main()
