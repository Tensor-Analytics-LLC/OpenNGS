"""Read immutable cloud format evidence and save a candidate; never publish routes."""
import argparse
import hashlib
import io
import json
from pathlib import Path,PurePosixPath
import tarfile
import tempfile
import zipfile

from backend.format_contract import BASE_IMAGE,SOURCE_FILES
from backend.runtime_catalog import validate_format_release
from scripts.build_portable_formats import spec_bytes
from scripts.qualify_portable_formats import verify_artifacts


def immutable_object(client,bucket,key,maximum,version=None):
    params={'Bucket':bucket,'Key':key}
    if version:params['VersionId']=version
    head=client.head_object(**params);version=head.get('VersionId')
    if not version or version=='null' or not 0<head['ContentLength']<=maximum:
        raise ValueError('Formats evidence requires a bounded immutable object version')
    response=client.get_object(Bucket=bucket,Key=key,VersionId=version);stream=response['Body']
    try:raw=stream.read(maximum+1)
    finally:stream.close()
    if len(raw)!=head['ContentLength'] or len(raw)>maximum or response.get('VersionId')!=version:
        raise ValueError('Format evidence version or length changed')
    return raw,{'bucket':bucket,'key':key,'version_id':version,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}


def verify_source(record,raw):
    if len(raw)!=record['archive_bytes'] or hashlib.sha256(raw).hexdigest()!=record['source_sha256']:
        raise ValueError('Formats source archive differs from the reviewed bytes')
    expected={row['path']:row for row in record['source_files']}
    if set(expected)!=set(SOURCE_FILES):raise ValueError('Formats source allowlist differs')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names=archive.namelist()
        allowed=set(SOURCE_FILES)|{'source-manifest.json','buildspec.yml'}|{'fixtures/'+r['name'] for r in record['fixture_files']}
        if len(names)!=len(allowed) or set(names)!=allowed:raise ValueError('Unexpected format source members')
        for name,row in expected.items():
            body=archive.read(name)
            if len(body)!=row['bytes'] or hashlib.sha256(body).hexdigest()!=row['sha256']:
                raise ValueError('Format implementation differs from its source manifest')
            if Path(name).read_bytes()!=body:raise ValueError('Finalizer source changed; restore the exact reviewed format source first')
        if archive.read('buildspec.yml')!=spec_bytes() or hashlib.sha256(spec_bytes()).hexdigest()!=record['buildspec_sha256']:
            raise ValueError('Format build commands differ from reviewed source')
        manifest=json.loads(archive.read('source-manifest.json'))
        if manifest!={'schema':1,'sources':record['source_files'],'fixtures':record['fixture_files']}:
            raise ValueError('Embedded format source manifest differs')
        for row in record['fixture_files']:
            body=archive.read('fixtures/'+row['name'])
            if len(body)!=row['bytes'] or hashlib.sha256(body).hexdigest()!=row['sha256']:
                raise ValueError('Format fixture bytes differ')


def verify_outputs(proof,raw):
    expected={obj['name']:obj for case in proof.get('cases',[]) for obj in case.get('outputs',[])}
    if len(expected)!=8:raise ValueError('Exactly eight retained format artifacts are required')
    seen=set();found={};expanded=0
    with tarfile.open(fileobj=io.BytesIO(raw),mode='r:gz') as archive:
        for member in archive:
            path=PurePosixPath(member.name)
            if path.is_absolute() or '..' in path.parts or member.issym() or member.islnk():raise ValueError('Unsafe format evidence archive')
            if member.isdir():continue
            name=str(path)
            if not member.isfile() or name in seen or member.size>1024**2:raise ValueError('Unexpected format evidence member')
            seen.add(name);expanded+=member.size
            if expanded>8*1024**2 or len(seen)>100:raise ValueError('Format evidence expansion exceeds its reviewed bound')
            if name in expected:
                body=archive.extractfile(member).read(member.size+1);row=expected[name]
                if len(body)!=row['bytes'] or hashlib.sha256(body).hexdigest()!=row['sha256']:
                    raise ValueError('Actual format output differs from its qualification SHA-256')
                found[name]=body
    if set(found)!=set(expected):raise ValueError('Actual format output artifacts are missing')
    with tempfile.TemporaryDirectory(prefix='openngs-format-evidence-') as directory:
        paths={}
        for name,body in found.items():
            path=Path(directory,name);path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(body);paths[name]=path
        try:return verify_artifacts(paths)
        except (AssertionError,ValueError,OSError) as error:raise ValueError('Retained format artifacts fail independent semantic verification') from error


def image_layers(client,digest):
    response=client.batch_get_image(repositoryName='openngs-demo',imageIds=[{'imageDigest':digest}],
        acceptedMediaTypes=['application/vnd.docker.distribution.manifest.v2+json','application/vnd.oci.image.manifest.v1+json'])
    if response.get('failures') or len(response.get('images',[]))!=1:raise ValueError('Exact private format image manifest is unavailable')
    image=response['images'][0];raw=image['imageManifest']
    if image['imageId']['imageDigest']!=digest or 'sha256:'+hashlib.sha256(raw.encode()).hexdigest()!=digest:
        raise ValueError('Format image manifest digest differs')
    manifest=json.loads(raw)
    if manifest.get('schemaVersion')!=2 or not isinstance(manifest.get('layers'),list):raise ValueError('A concrete single-platform format image is required')
    return manifest['layers']


def added_layer_bytes(base,derived):
    # OCI and Docker manifests may describe the same compressed blobs using
    # different media-type labels. Exact blob digests, sizes and order bind them.
    identities=lambda rows:[(row.get('digest'),row.get('size')) for row in rows]
    if not base or len(derived)<=len(base) or identities(derived[:len(base)])!=identities(base):
        raise ValueError('Formats must retain every exact base layer in order')
    added=derived[len(base):]
    if any(type(row.get('size')) is not int or row['size']<=0 for row in added):raise ValueError('Invalid derived format layer sizes')
    size=sum(row['size'] for row in added)
    if not 0<size<=16*1024**2:raise ValueError('Derived format layers exceed the reviewed storage bound')
    return size


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--build',default='evidence/deployment/portable-formats-build.json')
    parser.add_argument('--output',default='evidence/deployment/portable-formats-release-candidate.json');args=parser.parse_args()
    from backend.config import aws_session
    record=json.loads(Path(args.build).read_text());session=aws_session();cb=session.client('codebuild')
    build=cb.batch_get_builds(ids=[record['build_id']])['builds'][0]
    if (build['buildStatus']!='SUCCEEDED' or build.get('timeoutInMinutes')!=10
            or build.get('environment',{}).get('computeType')!='BUILD_GENERAL1_MEDIUM'
            or build.get('source',{}).get('type')!='S3'
            or build['source'].get('location')!=record['bucket']+'/'+record['source_key']
            or build.get('sourceVersion')!=record['source_version']
            or build['source'].get('buildspec')!=spec_bytes().decode()):
        raise ValueError('Formats require the successful exact-source, ten-minute reviewed build')
    s3=session.client('s3');raw,source_identity=immutable_object(s3,record['bucket'],record['source_key'],1024**2,record['source_version'])
    verify_source(record,raw)
    raw,report_identity=immutable_object(s3,record['bucket'],record['qualification_key'],1024**2);proof=json.loads(raw)
    raw,outputs_identity=immutable_object(s3,record['bucket'],record['artifacts_key'],8*1024**2)
    verification=verify_outputs(proof,raw)
    ecr=session.client('ecr');image=ecr.describe_images(repositoryName='openngs-demo',imageIds=[{'imageTag':record['tag']}])['imageDetails'][0]
    digest=image['imageDigest'];new_bytes=added_layer_bytes(image_layers(ecr,BASE_IMAGE.rsplit('@',1)[1]),image_layers(ecr,digest))
    candidate={**record,'image':BASE_IMAGE.rsplit('@',1)[0]+'@'+digest,'qualification':proof,
        'artifact_verification':verification,'new_layer_compressed_bytes':new_bytes,
        'incremental_monthly_ecr_usd_estimate':new_bytes/1024**3*.10,
        'evidence':{'source':source_identity,'qualification':report_identity,'outputs':outputs_identity},
        'publication':'Candidate only; explicit reviewed release and tenant reconciliation are required'}
    validate_format_release(candidate);Path(args.output).write_text(json.dumps(candidate,indent=2))
    print(json.dumps({'state':'Qualified candidate','image':candidate['image'],'new_layer_compressed_bytes':new_bytes,
                      'artifact_verification':verification,'aws_mutations':False},indent=2))


if __name__=='__main__':main()
