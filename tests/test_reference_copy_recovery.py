"""Interrupted exact-source recovery uses actual streams, never claimed metadata."""
import base64,copy,hashlib,io,json
import pytest
from backend import cloud_references as refs
from scripts.acquire_references import recover_current_copy


def fixture():
    raw=b'>chr1\nACGTACGT\n';bucket,key=next(iter(refs.CURRENT_ONLY_SOURCES));version='original-version'
    item={'bucket':bucket,'key':key,'version':version,'etag':'"pinned"','bytes':len(raw)}
    head={'ContentLength':len(raw),'ETag':item['etag'],'VersionId':version}
    destination={'ContentLength':len(raw),'VersionId':'preserved-destination','ChecksumType':'FULL_OBJECT',
                 'ChecksumSHA256':base64.b64encode(hashlib.sha256(raw).digest()).decode(),
                 'Metadata':{'source-bucket':bucket,'source-etag':'pinned','source-version':version}}
    class Client:
        def __init__(self):self.heads=0;self.gets=0
        def head_object(self,**kw):self.heads+=1;return dict(head)
        def get_object(self,**kw):
            assert kw=={'Bucket':bucket,'Key':key,'IfMatch':'"pinned"'}
            self.gets+=1;return {**head,'Body':io.BytesIO(raw)}
    return item,destination,Client(),head


def test_recovery_hashes_exact_source_and_preserves_destination():
    item,destination,client,_=fixture();original=copy.deepcopy(destination)
    proof=recover_current_copy(client,destination,item,{}, {'region':'us-east-1','instance_id':'i-0123456789abcdef0'})
    assert destination==original and client.gets==1 and client.heads==2
    assert proof['destination_version_id']=='preserved-destination'
    assert proof['source_sha256']==base64.b64decode(destination['ChecksumSHA256']).hex()


@pytest.mark.parametrize('fault',['checksum','source-version','length','composite','outside-aws'])
def test_unproven_recovery_never_succeeds(fault):
    item,destination,client,head=fixture();runtime={'region':'us-east-1','instance_id':'i-0123456789abcdef0'}
    if fault=='checksum':destination['ChecksumSHA256']=base64.b64encode(b'X'*32).decode()
    elif fault=='source-version':head['VersionId']='different-version'
    elif fault=='length':destination['ContentLength']+=1
    elif fault=='composite':destination['ChecksumType']='COMPOSITE'
    else:runtime['region']='local'
    with pytest.raises(ValueError):recover_current_copy(client,destination,item,{},runtime)


def test_publication_requires_full_bound_equivalence_proof():
    bucket,key=next(iter(refs.CURRENT_ONLY_SOURCES));size,etag,version=refs._IDENTITIES[(bucket,key)]
    sha=hashlib.sha256(b'validator-fixture-only').digest();identity={'bytes':size,'etag':etag,'version_id':version}
    obj={'source_bucket':bucket,'source_key':key,'bytes':size,'source_etag':etag,'source_version':version,
         'version_id':'unchanged-destination','checksum_type':'FULL_OBJECT','checksum_sha256':base64.b64encode(sha).decode(),
         'source_byte_equivalence':{'method':'aws-current-source-byte-equivalence-v1','source_before':identity,'source_after':dict(identity),
          'source_get_version_id':version,'destination_version_id':'unchanged-destination','source_sha256':sha.hex(),'bytes':size,
          'runtime':{'region':'us-east-1','instance_id':'i-0123456789abcdef0'}}}
    refs._public_identity(obj,acquired=True)
    for field in obj['source_byte_equivalence']:
        changed=copy.deepcopy(obj);changed['source_byte_equivalence'].pop(field)
        with pytest.raises(ValueError):refs._public_identity(changed,acquired=True)


def test_recovered_proof_survives_receipt_reload_and_tenant_publication(client,owner,monkeypatch,tmp_path):
    from backend import db
    from scripts import acquire_references as acquire
    from test_cloud_references import StagingS3,configure,tiny_fasta

    fasta,fai,_=tiny_fasta()
    sources=refs.BUNDLES['t2t-chm13-raw-v2.0'][:2]
    bodies=dict(zip(sources,(fasta,fai)))
    for ident,body in bodies.items():
        _,etag,version=refs._IDENTITIES[ident]
        monkeypatch.setitem(refs._IDENTITIES,ident,(len(body),etag,version))
    manifest=refs.reviewed_manifest(['t2t-chm13-raw-v2.0'])

    class Body(io.BytesIO):
        def iter_chunks(self,size):
            while chunk:=self.read(min(size,3)):yield chunk

    class CloudS3(StagingS3):
        def copy_object(self,**args):
            response=super().copy_object(**args)
            source=args['CopySource'];body=bodies[(source['Bucket'],source['Key'])]
            self.heads[(args['Bucket'],args['Key'])]['ChecksumSHA256']=base64.b64encode(hashlib.sha256(body).digest()).decode()
            return response

        def get_object(self,**args):
            self.gets+=1
            ident=next(ident for ident in bodies if args['Key'].endswith(ident[1]))
            if (args['Bucket'],args['Key']) in refs.CURRENT_ONLY_SOURCES:
                assert args=={'Bucket':ident[0],'Key':ident[1],'IfMatch':refs._IDENTITIES[ident][1]}
                return {**self.heads[ident],'Body':Body(bodies[ident])}
            assert args['VersionId']==self.heads[(args['Bucket'],args['Key'])]['VersionId']
            return {'Body':Body(bodies[ident])}

        def put_object(self,**args):
            body=args['Body'];version='derived-version'
            self.heads[(args['Bucket'],args['Key'])]={
                'ContentLength':len(body),'VersionId':version,'ChecksumType':'FULL_OBJECT',
                'ChecksumSHA256':base64.b64encode(hashlib.sha256(body).digest()).decode(),
                'Metadata':args.get('Metadata',{})}
            return {'VersionId':version}

    monkeypatch.setenv('NGS_BUCKET','reviewed-cache')
    runtime={'region':'us-east-1','instance_id':'i-0123456789abcdef0'}
    monkeypatch.setattr(acquire,'verify_cloud_runtime',lambda _:runtime)
    release={'bucket':'reviewed-cache','kms':'arn:test:key'}
    cloud=CloudS3(manifest);path=tmp_path/'receipt.json'
    acquire.acquire(manifest,release,path,execute=True,stage_only=True,client=cloud)
    interrupted=json.loads(path.read_text())
    lost=interrupted['objects'].pop(0)
    preserved=copy.deepcopy(cloud.heads[(lost['bucket'],lost['key'])])
    path.write_text(json.dumps(interrupted))

    # A lost CopyObject response has no usable receipt. Recover by hashing the
    # actual tiny public stream, retaining the already copied private version.
    acquire.acquire(manifest,release,path,execute=True,stage_only=True,recover_existing=True,client=cloud)
    receipt=json.loads(path.read_text())
    recovered=next(o for o in receipt['objects'] if o['source_key']==lost['source_key'])
    proof=recovered['source_byte_equivalence']
    assert proof['destination_version_id']==lost['version_id']
    assert cloud.heads[(lost['bucket'],lost['key'])]==preserved
    assert cloud.gets==1 and len(cloud.copies)==2

    # Restart uses durable proof without re-reading the public genome or
    # creating another retained version. Derivation consumes private copies.
    acquire.acquire(manifest,release,path,execute=True,stage_only=True,client=cloud)
    assert cloud.gets==1 and len(cloud.copies)==2
    assert acquire.acquire(manifest,release,path,execute=True,derive_only=True,client=cloud)['completed']
    record=json.loads(path.read_text())
    assert cloud.gets==3 and len(cloud.copies)==2
    assert next(o for o in record['objects'] if o['source_key']==lost['source_key'])['source_byte_equivalence']==proof

    tenant=configure(owner,record,monkeypatch)
    assert refs.publish(owner,owner['id'],record,execute=True)['state']=='Ready'
    assert tenant.copies==3
    metadata=json.loads(db.query("SELECT metadata FROM assets WHERE format='FASTA'",one=True)['metadata'])
    assert metadata['source_identity']['source_byte_equivalence']==proof
    assert metadata['source_identity']['acquired_version']==lost['version_id']
    assert len(metadata['secondary_objects'])==2
