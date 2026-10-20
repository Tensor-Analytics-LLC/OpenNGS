import base64
import copy
import hashlib
import json

import pytest
from botocore.exceptions import ClientError
from backend import cloud_references as refs, data, db, entitlements
from scripts import acquire_references as acquire
from test_entitlements import allowance


def acquisition(monkeypatch, bundles=None):
    monkeypatch.setenv('NGS_BUCKET', 'reviewed-cache')
    objects=[]
    manifest = refs.reviewed_manifest(bundles or [refs.LEGACY_BUNDLE])
    for i, item in enumerate(manifest['objects']):
        bucket, key = item['bucket'], item['key']
        digest=hashlib.sha256(key.encode()).digest()
        objects.append({'source_bucket':bucket,'source_key':key,'bucket':'reviewed-cache',
                        'key':'operations/checkpoints/references/'+bucket+'/'+key,
                        'version_id':'source-'+str(i),'bytes':item['bytes'],'checksum_type':'FULL_OBJECT',
                        'source_etag':item['etag'],'source_version':item['version'],
                        **({'copy_source_version_id':item['version']} if (bucket,key) in refs.CURRENT_ONLY_SOURCES else {}),
                        'checksum_sha256':base64.b64encode(digest).decode()})
    if bundles and 't2t-chm13-raw-v2.0' in bundles:
        bucket,key=refs.T2T_DICTIONARY
        objects.append({'source_bucket':bucket,'source_key':key,'bucket':'reviewed-cache',
                        'key':'operations/checkpoints/references/'+bucket+'/'+key,
                        'version_id':'derived-dictionary','bytes':200,'checksum_type':'FULL_OBJECT',
                        'checksum_sha256':base64.b64encode(hashlib.sha256(key.encode()).digest()).decode(),
                        'derivation':{'algorithm':refs.DICTIONARY_ALGORITHM,'fai_validated':True,
                            'parents':{o['source_key']:{'version_id':o['version_id'],'checksum_sha256':o['checksum_sha256']}
                                       for o in objects if o['source_bucket']=='human-pangenomics'}}})
    result={'completed':True,'objects':objects,'total_bytes':sum(o['bytes'] for o in objects)}
    if bundles:result.update(schema=2,bundles=bundles)
    return result


class S3:
    def __init__(self, record, corrupt=False):
        self.heads={(o['bucket'],o['key']):{'ContentLength':o['bytes'],'ChecksumSHA256':o['checksum_sha256'],
                    'ChecksumType':'FULL_OBJECT','VersionId':o['version_id'],'ETag':'exact-etag'} for o in record['objects']}
        self.copies=0
        self.corrupt=corrupt

    def head_object(self, **args):
        head=self.heads.get((args['Bucket'],args['Key']))
        if head is None:
            raise ClientError({'Error':{'Code':'404'}},'HeadObject')
        return dict(head)

    def copy_object(self, **args):
        source=args['CopySource'];self.copies+=1
        assert args['CopySourceIfMatch']=='exact-etag'
        assert args['ServerSideEncryption']=='aws:kms'
        head={**self.heads[(source['Bucket'],source['Key'])], 'VersionId':'tenant-'+str(self.copies), 'Metadata':args['Metadata']}
        if self.corrupt:head['ContentLength']+=1
        self.heads[(args['Bucket'],args['Key'])]=head
        return {'VersionId':head['VersionId']}


def configure(owner, record, monkeypatch, corrupt=False):
    entitlements.grant(owner,owner['id'],{'reason':'Owner funded exact reference publication','funding_confirmed':True,'allowance':allowance()})
    with db.tx() as c:
        c.execute('INSERT INTO settings VALUES(?,?)',(owner['id'],db.encode({'s3_bucket':'private-tenant','kms_key_arn':'arn:test:key','research_profile':{'state':'Ready'}})))
    s3=S3(record,corrupt)
    class Session:
        def client(self,name,**kwargs):
            assert name=='s3'
            return s3
    monkeypatch.setattr(refs,'aws_session',lambda:Session())
    return s3


def test_publication_is_dry_by_default_and_never_accepts_arbitrary_sources(client,owner,monkeypatch):
    record=acquisition(monkeypatch);s3=configure(owner,record,monkeypatch)
    prepared=refs.publish(owner,owner['id'],record)
    assert prepared['state']=='planned' and not prepared['aws_mutations'] and s3.copies==0
    assert db.query('SELECT * FROM datasets')==[]
    for changed in [dict(record,completed=False),dict(record,objects=record['objects'][:-1])]:
        with pytest.raises(ValueError):refs.publish(owner,owner['id'],changed,execute=True)
    changed=copy.deepcopy(record);changed['objects'][0]['key']='tenants/foreign/private.fa'
    with pytest.raises(ValueError,match='reviewed platform'):refs.publish(owner,owner['id'],changed,execute=True)
    assert s3.copies==0


def test_exact_copies_commit_complete_companions_and_retry_without_recopying(client,owner,monkeypatch):
    record=acquisition(monkeypatch);s3=configure(owner,record,monkeypatch)
    result=refs.publish(owner,owner['id'],record,execute=True)
    assert result['state']=='Ready' and s3.copies==6
    rows=db.query('SELECT * FROM datasets');assert len(rows)==6 and all(r['state']=='Ready' and r['version_id'].startswith('tenant-') for r in rows)
    fasta=db.query("SELECT * FROM assets WHERE format='FASTA'",one=True);meta=json.loads(fasta['metadata'])
    assert len(meta['secondary_objects'])==2 and meta['index_sha256'] and meta['dictionary_version_id']
    assert all(o['uri'].startswith('s3://private-tenant/tenants/'+owner['id']+'/') for o in meta['secondary_objects'])
    assert refs.publish(owner,owner['id'],record,execute=True)['additional_dataset_bytes']==0
    assert s3.copies==6 and len(db.query('SELECT * FROM cost_reservations'))==1


def test_corrupt_copy_never_becomes_ready_and_holds_are_preserved(client,owner,monkeypatch):
    record=acquisition(monkeypatch);s3=configure(owner,record,monkeypatch,corrupt=True)
    with pytest.raises(ValueError,match='checksum differs'):refs.publish(owner,owner['id'],record,execute=True)
    assert s3.copies==1 and db.query('SELECT * FROM assets')==[]
    assert all(r['state']=='Provisioning' for r in db.query('SELECT * FROM datasets'))
    assert db.query('SELECT state FROM cost_reservations',one=True)['state']=='reserved'


def test_only_funded_workspace_and_active_admin_can_publish(client,owner,monkeypatch):
    record=acquisition(monkeypatch)
    with pytest.raises(ValueError,match='funded Research Pro'):refs.publish(owner,owner['id'],record,execute=True)
    with pytest.raises(ValueError,match='active administrator'):refs.publish({**owner,'role':'user'},owner['id'],record,execute=True)
    with pytest.raises(ValueError,match='active administrator'):refs.publish({**owner,'frozen':True},owner['id'],record,execute=True)


def test_reviewed_expansion_costs_fit_double_retained_bound_and_reject_substitution(monkeypatch):
    manifest=refs.reviewed_manifest(['grch37-broad-v0','t2t-chm13-raw-v2.0'])
    prepared=refs.acquisition_plan(manifest)
    assert prepared['source_bytes']==6297034463
    assert prepared['maximum_double_retained_bytes']<=12*1024**3
    assert prepared['maximum_double_retained_monthly_s3_usd_estimate']<.28
    for field,value in [('bucket','unreviewed'),('key','https://example.test/arbitrary.fa'),
                        ('bytes',1),('etag','different'),('version','latest')]:
        changed=copy.deepcopy(manifest);changed['objects'][0][field]=value
        with pytest.raises(ValueError):refs.acquisition_plan(changed)
    with pytest.raises(ValueError,match='bound'):
        refs.acquisition_plan(refs.reviewed_manifest([refs.LEGACY_BUNDLE,'grch37-broad-v0','t2t-chm13-raw-v2.0']))
    with pytest.raises(ValueError):refs.acquisition_plan({**manifest,'bundles':['unknown']})


def test_grch37_and_raw_t2t_publish_with_exact_assembly_and_required_companions(client,owner,monkeypatch):
    record=acquisition(monkeypatch,['grch37-broad-v0','t2t-chm13-raw-v2.0'])
    s3=configure(owner,record,monkeypatch)
    result=refs.publish(owner,owner['id'],record,execute=True)
    assert result['state']=='Ready' and s3.copies==6
    assets=db.query("SELECT * FROM assets WHERE format='FASTA'")
    assert {asset['assembly'] for asset in assets}=={'GRCh37','T2T-CHM13'}
    for asset in assets:
        meta=json.loads(asset['metadata'])
        assert meta['assembly']==asset['assembly']==meta['source_identity']['assembly']
        assert len(meta['secondary_objects'])==2
        assert meta['index'] and meta['dictionary'] and meta['dictionary_version_id']
        if asset['assembly']=='T2T-CHM13':
            assert 'maskedY_rCRS' in meta['reference_variant']
            dictionary=db.query("SELECT metadata FROM assets WHERE name='chm13v2.0.dict'",one=True)
            assert json.loads(dictionary['metadata'])['derivation']['fai_validated']


def test_raw_t2t_missing_dictionary_or_changed_derivation_never_publishes(client,owner,monkeypatch):
    record=acquisition(monkeypatch,['t2t-chm13-raw-v2.0']);s3=configure(owner,record,monkeypatch)
    absent=copy.deepcopy(record);absent['objects']=absent['objects'][:-1]
    with pytest.raises(ValueError,match='dictionary'):refs.publish(owner,owner['id'],absent,execute=True)
    changed=copy.deepcopy(record);changed['objects'][-1]['derivation']['parents']={}
    with pytest.raises(ValueError,match='parent identities'):refs.publish(owner,owner['id'],changed,execute=True)
    changed=copy.deepcopy(record);changed['objects'][-1]['bytes']=refs.DICTIONARY_MAX_BYTES+1
    with pytest.raises(ValueError,match='bounded'):refs.publish(owner,owner['id'],changed,execute=True)
    assert s3.copies==0 and db.query('SELECT * FROM datasets')==[]


def test_legacy_six_object_dataset_ids_remain_identical(monkeypatch):
    record=acquisition(monkeypatch)
    prepared=refs.plan('owner-identity',record)
    for obj in prepared['objects']:
        expected='ds_reference_'+hashlib.sha256(('owner-identity:'+obj['source_bucket']+':'+obj['source_key']+':'+obj['sha256']).encode()).hexdigest()[:40]
        assert obj['dataset_id']==expected
    assert prepared['bundles']==[refs.LEGACY_BUNDLE]


def tiny_fasta():
    fasta=b'>chr1 description\naCgT\nNN\n>chrM\nAC\n'
    fai=b'chr1\t6\t18\t4\t5\nchrM\t2\t32\t2\t3\n'
    checksum=base64.b64encode(hashlib.sha256(fasta).digest()).decode()
    return fasta,fai,checksum


def test_dictionary_derivation_streams_across_arbitrary_chunk_boundaries_and_uppercases_md5():
    fasta,fai,checksum=tiny_fasta()
    # Boundaries split both headers and sequence lines; no full FASTA file is made.
    chunks=[fasta[i:i+3] for i in range(0,len(fasta),3)]
    output=acquire.derive_dictionary(chunks,fai,len(fasta),checksum)
    assert output==('@HD\tVN:1.6\tSO:unsorted\n'
                    '@SQ\tSN:chr1\tLN:6\tM5:'+hashlib.md5(b'ACGTNN').hexdigest()+'\n'
                    '@SQ\tSN:chrM\tLN:2\tM5:'+hashlib.md5(b'AC').hexdigest()+'\n').encode()
    assert b'UR:' not in output


@pytest.mark.parametrize('field,value',[(b'6\t18',b'7\t18'),(b'6\t18',b'6\t19'),
                                      (b'4\t5',b'3\t4'),(b'chrM',b'chrY')])
def test_dictionary_derivation_rejects_fai_length_offset_width_or_contig_substitution(field,value):
    fasta,fai,checksum=tiny_fasta()
    with pytest.raises(ValueError,match='index'):
        acquire.derive_dictionary([fasta],fai.replace(field,value),len(fasta),checksum)


def test_dictionary_derivation_requires_complete_raw_checksum_and_bounded_input():
    fasta,fai,checksum=tiny_fasta()
    with pytest.raises(ValueError,match='SHA-256'):
        acquire.derive_dictionary([fasta],fai,len(fasta),base64.b64encode(b'x'*32).decode())
    with pytest.raises(ValueError,match='exceeds'):
        acquire.derive_dictionary([fasta],fai,len(fasta)-1,checksum)
    with pytest.raises(ValueError,match='bounded'):
        acquire.derive_dictionary([b'x'*(acquire.CHUNK_BYTES+1)],fai,acquire.CHUNK_BYTES+1,checksum)


class StagingS3:
    def __init__(self,manifest,changed=False):
        self.heads={(o['bucket'],o['key']):{'ContentLength':o['bytes'],'ETag':o['etag'],
                                           'VersionId':o['version']} for o in manifest['objects']}
        if changed:self.heads[next(iter(self.heads))]['ETag']='unreviewed-new-object'
        self.copies=[]
        self.gets=0

    def head_object(self,**args):
        if (args['Bucket'],args['Key']) in refs.CURRENT_ONLY_SOURCES:
            assert 'VersionId' not in args
        head=self.heads.get((args['Bucket'],args['Key']))
        if head is None:raise ClientError({'Error':{'Code':'404'}},'HeadObject')
        return head

    def copy_object(self,**args):
        source=args['CopySource'];head=self.heads[(source['Bucket'],source['Key'])]
        if (source['Bucket'],source['Key']) in refs.CURRENT_ONLY_SOURCES:
            assert 'VersionId' not in source
        else:
            assert source.get('VersionId')==head['VersionId']
        assert args['CopySourceIfMatch']==head['ETag']
        self.copies.append(args)
        version='cache-'+str(len(self.copies))
        self.heads[(args['Bucket'],args['Key'])]={**head,'VersionId':version,'ChecksumType':'FULL_OBJECT','Metadata':args['Metadata'],
            'ChecksumSHA256':base64.b64encode(hashlib.sha256(args['Key'].encode()).digest()).decode()}
        return {'VersionId':version,'CopySourceVersionId':head['VersionId']}

    def get_object(self,**args):
        self.gets+=1
        raise AssertionError('Operator staging cannot read any object body')


def test_operator_staging_uses_exact_server_side_versions_without_any_body_get(monkeypatch,tmp_path):
    manifest=refs.reviewed_manifest(['grch37-broad-v0','t2t-chm13-raw-v2.0'])
    monkeypatch.setenv('NGS_BUCKET','reviewed-cache')
    monkeypatch.setattr(acquire,'verify_cloud_runtime',lambda _:pytest.fail('Staging does not need EC2 body access'))
    s3=StagingS3(manifest);path=tmp_path/'receipt.json'
    result=acquire.acquire(manifest,{'bucket':'reviewed-cache','kms':'arn:test:key'},path,
                           execute=True,stage_only=True,client=s3)
    assert result['state']=='staged' and not result['completed'] and len(s3.copies)==5 and s3.gets==0
    assert json.loads(path.read_text())['completed'] is False
    assert not path.with_suffix('.fa').exists()
    again=acquire.acquire(manifest,{'bucket':'reviewed-cache','kms':'arn:test:key'},path,
                          execute=True,stage_only=True,client=s3)
    assert again['state']=='staged' and len(s3.copies)==5


def test_changed_public_source_or_missing_cloud_staging_never_copies(monkeypatch,tmp_path):
    manifest=refs.reviewed_manifest(['grch37-broad-v0'])
    monkeypatch.setenv('NGS_BUCKET','reviewed-cache')
    s3=StagingS3(manifest,changed=True)
    with pytest.raises(ValueError,match='identity changed'):
        acquire.acquire(manifest,{'bucket':'reviewed-cache','kms':'arn:test:key'},tmp_path/'receipt.json',
                        execute=True,stage_only=True,client=s3)
    assert s3.copies==[]
    monkeypatch.setattr(acquire,'verify_cloud_runtime',lambda _:{'instance_id':'test'})
    with pytest.raises(ValueError,match='already staged'):
        acquire.acquire(manifest,{'bucket':'reviewed-cache','kms':'arn:test:key'},tmp_path/'receipt2.json',
                        execute=True,derive_only=True,client=s3)
    assert s3.copies==[]


def test_execute_on_local_device_is_refused_before_s3_access(monkeypatch,tmp_path):
    manifest=refs.reviewed_manifest(['t2t-chm13-raw-v2.0'])
    monkeypatch.setenv('NGS_BUCKET','reviewed-cache')
    def refused(_):raise ValueError('Local acquisition is forbidden')
    monkeypatch.setattr(acquire,'verify_cloud_runtime',refused)
    s3=StagingS3(manifest)
    with pytest.raises(ValueError,match='Local'):
        acquire.acquire(manifest,{'bucket':'reviewed-cache','kms':'arn:test:key'},tmp_path/'receipt.json',
                        execute=True,derive_only=True,client=s3)
    assert s3.copies==[] and s3.gets==0


def test_interrupted_server_copy_is_reconciled_without_creating_another_retained_version(monkeypatch,tmp_path):
    manifest=refs.reviewed_manifest(['grch37-broad-v0'])
    monkeypatch.setenv('NGS_BUCKET','reviewed-cache')
    s3=StagingS3(manifest);release={'bucket':'reviewed-cache','kms':'arn:test:key'}
    path=tmp_path/'receipt.json'
    acquire.acquire(manifest,release,path,execute=True,stage_only=True,client=s3)
    assert len(s3.copies)==3
    # Simulate lost receipt after successful cloud copies, then recover exact heads.
    path.unlink()
    acquire.acquire(manifest,release,path,execute=True,stage_only=True,client=s3)
    assert len(s3.copies)==3 and s3.gets==0
    path.unlink()
    next(head for (bucket,key),head in s3.heads.items() if bucket=='reviewed-cache')['Metadata']={'source-etag':'different'}
    with pytest.raises(ValueError,match='Existing cache object'):
        acquire.acquire(manifest,release,path,execute=True,stage_only=True,client=s3)
    assert len(s3.copies)==3


def test_ec2_identity_gate_checks_imdsv2_and_expected_kms_account_and_region(monkeypatch):
    import io
    class Opener:
        def __init__(self):self.requests=[]
        def open(self,request,timeout):
            self.requests.append(request)
            if request.get_method()=='PUT':return io.BytesIO(b'imdsv2-test-token')
            assert request.get_header('X-aws-ec2-metadata-token')=='imdsv2-test-token'
            return io.BytesIO(json.dumps({'instanceId':'i-reviewed','accountId':'123456789012','region':'us-east-1'}).encode())
    opener=Opener();monkeypatch.setattr(acquire,'build_opener',lambda *_:opener)
    monkeypatch.setenv('NGS_BUCKET','reviewed-cache')
    release={'bucket':'reviewed-cache','kms':'arn:aws:kms:us-east-1:123456789012:key/test'}
    assert acquire.verify_cloud_runtime(release)['instance_id']=='i-reviewed'
    assert opener.requests[0].get_header('X-aws-ec2-metadata-token-ttl-seconds')=='60'
    with pytest.raises(ValueError,match='account/region'):
        acquire.verify_cloud_runtime({**release,'kms':'arn:aws:kms:us-west-2:123456789012:key/test'})
    monkeypatch.setenv('NGS_BUCKET','foreign-cache')
    with pytest.raises(ValueError,match='configured deployment'):
        acquire.verify_cloud_runtime(release)


def test_cloud_only_derived_dictionary_completes_verified_receipt_and_retries_without_body_reads(monkeypatch,tmp_path):
    import io
    fasta,fai,checksum=tiny_fasta()
    source_ids=refs.BUNDLES['t2t-chm13-raw-v2.0'][:2]
    bodies={source_ids[0]:fasta,source_ids[1]:fai}
    # Small stand-in provider bytes exercise the entire flow without genome downloads.
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
            source=next(ident for ident in bodies if args['Key'].endswith(ident[1]))
            return {'Body':Body(bodies[source])}
        def put_object(self,**args):
            body=args['Body'];version='derived-version'
            self.heads[(args['Bucket'],args['Key'])]={'ContentLength':len(body),'VersionId':version,
                'ChecksumType':'FULL_OBJECT','ChecksumSHA256':base64.b64encode(hashlib.sha256(body).digest()).decode(),
                'Metadata':args.get('Metadata',{})}
            return {'VersionId':version}
    monkeypatch.setenv('NGS_BUCKET','reviewed-cache')
    monkeypatch.setattr(acquire,'verify_cloud_runtime',lambda _:{'instance_id':'i-reviewed'})
    s3=CloudS3(manifest);path=tmp_path/'receipt.json';release={'bucket':'reviewed-cache','kms':'arn:test:key'}
    acquire.acquire(manifest,release,path,execute=True,stage_only=True,client=s3)
    result=acquire.acquire(manifest,release,path,execute=True,derive_only=True,client=s3)
    assert result['completed'] and s3.gets==2 and len(s3.copies)==2
    receipt=json.loads(path.read_text())
    assert receipt['completed'] and len(receipt['objects'])==3
    prepared=refs.plan('owner',receipt)
    assert prepared['bytes']==receipt['total_bytes']
    assert receipt['objects'][-1]['derivation']['fai_validated']
    acquire.acquire(manifest,release,path,execute=True,derive_only=True,client=s3)
    assert s3.gets==2 and len(s3.copies)==2


@pytest.mark.parametrize('failure',['missing-response-version','different-response-version','changed-after-copy'])
def test_conditional_public_copy_requires_actual_version_response_and_stable_current_head(monkeypatch,tmp_path,failure):
    manifest=refs.reviewed_manifest(['t2t-chm13-raw-v2.0'])
    monkeypatch.setenv('NGS_BUCKET','reviewed-cache')
    class ConditionalS3(StagingS3):
        def copy_object(self,**args):
            response=super().copy_object(**args)
            if failure=='missing-response-version':response.pop('CopySourceVersionId')
            elif failure=='different-response-version':response['CopySourceVersionId']='unreviewed-version'
            else:
                source=args['CopySource']
                self.heads[(source['Bucket'],source['Key'])]['VersionId']='new-current-version'
            return response
    s3=ConditionalS3(manifest);path=tmp_path/'receipt.json';release={'bucket':'reviewed-cache','kms':'arn:test:key'}
    with pytest.raises(ValueError,match='retained unready'):
        acquire.acquire(manifest,release,path,execute=True,stage_only=True,client=s3)
    assert len(s3.copies)==1 and s3.gets==0 and not path.exists()
    # Without checkpointed response evidence, source-version metadata alone
    # cannot admit the already retained object or cause another retained copy.
    with pytest.raises(ValueError):
        acquire.acquire(manifest,release,path,execute=True,stage_only=True,client=s3)
    assert len(s3.copies)==1


def test_conditional_source_proof_survives_receipt_and_is_required_for_publication(client,owner,monkeypatch,tmp_path):
    manifest=refs.reviewed_manifest(['grch37-broad-v0','t2t-chm13-raw-v2.0'])
    monkeypatch.setenv('NGS_BUCKET','reviewed-cache')
    s3=StagingS3(manifest);path=tmp_path/'receipt.json'
    acquire.acquire(manifest,{'bucket':'reviewed-cache','kms':'arn:test:key'},path,execute=True,stage_only=True,client=s3)
    receipt=json.loads(path.read_text())
    conditional=[o for o in receipt['objects'] if (o['source_bucket'],o['source_key']) in refs.CURRENT_ONLY_SOURCES]
    assert len(conditional)==2 and all(o['copy_source_version_id']==o['source_version'] for o in conditional)
    record=acquisition(monkeypatch,['t2t-chm13-raw-v2.0']);publication=configure(owner,record,monkeypatch)
    record['objects'][0].pop('copy_source_version_id')
    with pytest.raises(ValueError,match='proof'):
        refs.publish(owner,owner['id'],record,execute=True)
    assert publication.copies==0


@pytest.mark.parametrize('inject_factory',[False,True])
def test_t2t_heads_use_reviewed_regional_short_timeout_client_separate_from_destination(monkeypatch,tmp_path,inject_factory):
    manifest=refs.reviewed_manifest(['grch37-broad-v0','t2t-chm13-raw-v2.0'])
    monkeypatch.setenv('NGS_BUCKET','reviewed-cache')
    class Destination(StagingS3):
        def head_object(self,**args):
            assert (args['Bucket'],args['Key']) not in refs.CURRENT_ONLY_SOURCES
            return super().head_object(**args)
    destination=Destination(manifest)
    class SourceHeads:
        calls=[]
        def head_object(self,**args):
            ident=(args['Bucket'],args['Key'])
            assert ident in refs.CURRENT_ONLY_SOURCES and 'VersionId' not in args
            self.calls.append(ident)
            return destination.heads[ident]
    source=SourceHeads();clients=[]
    def regional_factory(region,config):
        clients.append(region)
        assert region=='us-west-2'
        assert config.use_dualstack_endpoint is True
        assert config.connect_timeout==3 and config.read_timeout==5
        assert config.retries=={'total_max_attempts':1}
        return source
    class Session:
        def client(self,name,**kwargs):
            assert name=='s3'
            if 'region_name' in kwargs:return regional_factory(kwargs['region_name'],kwargs['config'])
            assert kwargs['config'].read_timeout==900
            return destination
    monkeypatch.setattr(acquire,'aws_session',lambda:Session())
    kwargs={'source_client_factory':regional_factory,'client':destination} if inject_factory else {}
    result=acquire.acquire(manifest,{'bucket':'reviewed-cache','kms':'arn:test:key'},tmp_path/'receipt.json',
                           execute=True,stage_only=True,**kwargs)
    assert result['state']=='staged' and len(destination.copies)==5 and destination.gets==0
    assert clients==['us-west-2']
    assert len(source.calls)==4 and all(source.calls.count(ident)==2 for ident in refs.CURRENT_ONLY_SOURCES)
