"""Build the reviewed core runtime in AWS; no local scientific downloads."""
import hashlib,json,zipfile
from pathlib import Path
from backend.config import aws_session

def main():
    s=aws_session();release=json.loads(Path('evidence/deployment/release.json').read_text());archive=Path('evidence/deployment/research-build.zip')
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for p in Path('backend').rglob('*.py'):z.write(p)
        for p in ['infra/research.Dockerfile','infra/cloud_worker.py','requirements.lock']:z.write(p)
    digest=hashlib.sha256(archive.read_bytes()).hexdigest();tag='research-'+digest[:20];key='operations/builds/'+tag+'/source.zip'
    s.client('s3').upload_file(str(archive),release['bucket'],key,ExtraArgs={'ServerSideEncryption':'aws:kms','SSEKMSKeyId':release['kms']})
    project=s.client('codebuild').batch_get_projects(names=['openngs-demo-build'])['projects'][0]
    document={'version':'0.2','phases':{'pre_build':{'commands':['aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin "$REPOSITORY"','docker pull mambaorg/micromamba:2.3.2','BASE=$(docker image inspect mambaorg/micromamba:2.3.2 --format "{{index .RepoDigests 0}}")','echo "Pinned upstream: $BASE"']},'build':{'commands':['docker build --build-arg "MAMBA_IMAGE=$BASE" -f infra/research.Dockerfile -t "$REPOSITORY:$IMAGE_TAG" .']},'post_build':{'commands':['docker push "$REPOSITORY:$IMAGE_TAG"','docker image inspect "$REPOSITORY:$IMAGE_TAG" --format "{{json .RepoDigests}}"']}}}
    # The build role is restricted to reviewed source prefixes and this repository.
    response=s.client('codebuild').start_build(projectName=project['name'],sourceTypeOverride='S3',sourceLocationOverride=release['bucket']+'/'+key,buildspecOverride=json.dumps(document),environmentVariablesOverride=[{'name':'IMAGE_TAG','value':tag,'type':'PLAINTEXT'}],timeoutInMinutesOverride=20)
    record={'build_id':response['build']['id'],'source_sha256':digest,'source_key':key,'tag':tag}
    Path('evidence/deployment/research-build.json').write_text(json.dumps(record,indent=2));print(json.dumps(record))

if __name__=='__main__':main()
