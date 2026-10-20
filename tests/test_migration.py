import base64
import hashlib
import json
import sqlite3
import time
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from backend import db, migration
from scripts.migrate_inventory import Migrator


@pytest.fixture
def migration_fixture(client, owner, tmp_path):
    with db.tx() as c:
        c.execute('DELETE FROM migration_assets');c.execute('DELETE FROM migration_objects')
    contents = b'ACGT test reference bytes\n';digest = hashlib.sha256(contents).hexdigest()
    paths = [tmp_path / 'source.fa', tmp_path / 'alias.fa']
    aliases = []
    for i, path in enumerate(paths):
        path.write_bytes(contents)
        aliases.append({'id': 'source_' + str(i), 'user_id': owner['id'], 'path': str(path), 'name': path.name, 'format': 'FASTA', 'bytes': len(contents), 'mtime': path.stat().st_mtime, 'mtime_ns': path.stat().st_mtime_ns, 'family': 'reference', 'assembly': 'engineering', 'metadata': {'original_note': 'Retain this source metadata'}})
    obj = {'sha256': digest, 'bytes': len(contents), 'aliases': aliases, 'dataset_id': 'ds_migration_test', 'key': f"tenants/{owner['id']}/datasets/ds_migration_test/source.fa", 'name': 'source.fa'}
    plan = {'plan_id': 'migration_' + 'a' * 24, 'bucket': 'private-test', 'kms': 'arn:test-key', 'owner_id': owner['id'], 'objects': [obj], 'total_bytes': len(contents)}
    return plan, obj, contents, tmp_path


def test_verified_import_preserves_all_ids_and_source_metadata(migration_fixture):
    plan, obj, content, _ = migration_fixture
    with db.tx() as c:
        for alias in obj['aliases']:
            c.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?,?)', tuple(alias[k] for k in ['id', 'user_id', 'path', 'name', 'format', 'bytes', 'mtime', 'family', 'assembly']) + (json.dumps(alias['metadata']),))
        c.execute('INSERT INTO graphs VALUES(?,?,?,1,?,?)', ('migration-saved', plan['owner_id'], 'Saved alias binding', json.dumps({'nodes': [{'params': {'asset_id': 'source_1'}}]}), time.time()))
    receipt = {'version_id': 'version-verified'}
    migration.import_manifest(plan, obj, receipt, obj['sha256'], 'raw-cloud-sha256')
    canonical = db.query('SELECT * FROM assets WHERE id=?', ('source_0',), one=True)
    alias = db.query('SELECT * FROM assets WHERE id=?', ('source_1',), one=True)
    assert canonical['path'].startswith('s3://private-test/')
    assert alias['path'] == obj['aliases'][1]['path']
    resolved = migration.resolve_asset(plan['owner_id'], alias)
    assert resolved['path'] == canonical['path'] and resolved['id'] == 'source_1'
    assert json.loads(resolved['metadata'])['original_note'] == 'Retain this source metadata'
    assert json.loads(resolved['metadata'])['dataset_id'] == obj['dataset_id']
    assert Path(alias['path']).read_bytes() == content
    assert db.query('SELECT body FROM graphs WHERE id=?', ('migration-saved',), one=True)['body'].find('source_1') >= 0
    # Idempotent verification never duplicates assets/data bytes.
    migration.import_manifest(plan, obj, receipt, obj['sha256'], 'raw-cloud-sha256')
    assert len(db.query('SELECT * FROM assets')) == 2
    assert len(db.query('SELECT * FROM datasets')) == 1


def test_wrong_raw_checksum_never_becomes_ready(migration_fixture):
    plan, obj, _, _ = migration_fixture
    with pytest.raises(ValueError, match='SHA256'):
        migration.import_manifest(plan, obj, {'version_id': 'wrong'}, 'b' * 64, 'raw-cloud-sha256')
    assert not db.query('SELECT * FROM datasets')


def test_source_provenance_warning_is_durable_and_does_not_change_identity(migration_fixture):
    plan,obj,content,_=migration_fixture;uid=plan['owner_id']
    warning={'code':'provider-size-mismatch','message':'Exact provider release differs in size; original bytes are preserved','source_bytes':obj['bytes'],'provider_bytes':999}
    migration.record_provenance_warning('reviewed-operator',uid,obj['sha256'],warning)
    migration.import_manifest(plan,obj,{'version_id':'immutable-version'},obj['sha256'],'raw-cloud-sha256')
    for alias in obj['aliases']:
        row=db.query('SELECT * FROM assets WHERE id=?',(alias['id'],),one=True)
        resolved=migration.resolve_asset(uid,row)
        assert json.loads(resolved['metadata'])['provenance_warnings'][0]['release_qualified'] is False
        assert json.loads(resolved['metadata'])['sha256']==obj['sha256']
        assert Path(alias['path']).read_bytes()==content
    assert migration.provenance_warnings('foreign-owner')==[]
    dataset=db.query('SELECT * FROM datasets WHERE id=?',(obj['dataset_id'],),one=True)
    assert dataset['state']=='Ready' and dataset['version_id']=='immutable-version'
    assert json.loads(dataset['manifest'])['provenance_warnings'][0]['code']=='provider-size-mismatch'
    assert any('migration.provenance.warning' in r['body'] for r in db.query('SELECT body FROM audit'))


def test_alias_cannot_resolve_across_tenants(migration_fixture):
    plan, obj, _, _ = migration_fixture
    migration.import_manifest(plan, obj, {'version_id': 'v1'}, obj['sha256'], 'raw-cloud-sha256')
    alias = db.query('SELECT * FROM assets WHERE id=?', ('source_1',), one=True)
    with pytest.raises(ValueError, match='verified'):
        migration.resolve_asset('another-tenant', alias)


class FakeS3:
    def __init__(self):
        self.objects = {};self.puts = 0

    def head_object(self, Bucket, Key, **kwargs):
        if (Bucket, Key) not in self.objects:
            raise ClientError({'Error': {'Code': '404'}}, 'HeadObject')
        return self.objects[(Bucket, Key)][1]

    def put_object(self, Bucket, Key, Body, **kwargs):
        assert kwargs.get('BucketKeyEnabled') is True
        if (Bucket, Key) in self.objects and kwargs.get('IfNoneMatch') == '*':
            raise ClientError({'Error': {'Code': 'PreconditionFailed'}}, 'PutObject')
        self.puts += 1
        head = {'VersionId': 'v' + str(self.puts), 'ContentLength': len(Body), 'Metadata': kwargs.get('Metadata', {}), 'ChecksumSHA256': kwargs.get('ChecksumSHA256'), 'ChecksumType': 'FULL_OBJECT'}
        self.objects[(Bucket, Key)] = (Body, head)
        return head


def test_migration_small_upload_checks_exact_source_and_resumes(migration_fixture):
    plan, obj, content, folder = migration_fixture;s3 = FakeS3()
    migrator = Migrator(plan, folder / 'ledger.sqlite', s3)
    migrator.transfer(obj)
    assert migrator.progress()['uploaded_bytes'] == len(content)
    puts = s3.puts
    Migrator(plan, folder / 'ledger.sqlite', s3).transfer(obj)
    assert s3.puts == puts
    assert s3.objects[(plan['bucket'], obj['key'])][0] == content
    assert Path(obj['aliases'][0]['path']).read_bytes() == content


def test_migration_changed_source_is_not_uploaded(migration_fixture):
    plan, obj, content, folder = migration_fixture;s3 = FakeS3()
    Path(obj['aliases'][0]['path']).write_bytes(content + b'changed')
    with pytest.raises(ValueError, match='Source identity'):
        Migrator(plan, folder / 'ledger.sqlite', s3).transfer(obj)
    assert s3.puts == 0


def test_private_source_authorization_pins_owner_version_and_digest(migration_fixture, monkeypatch):
    plan, obj, _, _ = migration_fixture
    monkeypatch.setenv('NGS_BUCKET', plan['bucket'])
    migration.import_manifest(plan, obj, {'version_id': 'version-123'}, obj['sha256'], 'raw-cloud-sha256')
    uri = 's3://' + plan['bucket'] + '/' + obj['key']
    proof = migration.authorize_source(plan['owner_id'], uri, 'version-123', obj['sha256'])
    assert proof['VersionId'] == 'version-123' and proof['sha256'] == obj['sha256']
    assert migration.authorize_source('foreign-user', uri) is None
    with pytest.raises(ValueError, match='version'):
        migration.authorize_source(plan['owner_id'], uri, 'wrong-version')
    with pytest.raises(ValueError, match='checksum'):
        migration.authorize_source(plan['owner_id'], uri, sha256='c' * 64)


def test_resumable_parts_are_reused_only_with_matching_provider_checksums(migration_fixture, monkeypatch):
    from scripts import migrate_inventory as engine
    plan, obj, content, folder = migration_fixture
    monkeypatch.setattr(engine, 'PART', 4)
    class Multipart(FakeS3):
        def __init__(self):
            super().__init__();self.parts={};self.upload_calls=[];self.create_calls=0;self.fail_part=3
        def create_multipart_upload(self, **kwargs):
            self.create_calls+=1;self.metadata=kwargs['Metadata'];return {'UploadId':'upload-exact'}
        def list_parts(self, **kwargs):
            return {'Parts':[{'PartNumber': n, 'ETag': p['ETag'], 'ChecksumSHA256':p['ChecksumSHA256'], 'Size':len(p['body'])} for n,p in sorted(self.parts.items())], 'IsTruncated':False}
        def upload_part(self, PartNumber, Body, ChecksumSHA256, **kwargs):
            if PartNumber==self.fail_part:
                raise TimeoutError('Simulated disconnected network')
            self.upload_calls.append(PartNumber)
            item={'ETag':'etag-'+str(PartNumber),'ChecksumSHA256':ChecksumSHA256,'body':Body};self.parts[PartNumber]=item
            return item
        def complete_multipart_upload(self, Bucket,Key,MultipartUpload,**kwargs):
            body=b''.join(self.parts[x['PartNumber']]['body'] for x in MultipartUpload['Parts'])
            self.objects[(Bucket,Key)]=(body,{'VersionId':'completed-multipart','ContentLength':len(body),'Metadata':self.metadata,'ChecksumType':'COMPOSITE'})
            return {'VersionId':'completed-multipart'}
    s3=Multipart();migrator=Migrator(plan,folder/'multipart.sqlite',s3)
    with pytest.raises(TimeoutError):
        migrator.transfer(obj)
    calls=list(s3.upload_calls);assert calls and 3 not in calls
    s3.fail_part=None
    Migrator(plan,folder/'multipart.sqlite',s3).transfer(obj)
    assert s3.create_calls==1
    assert all(s3.upload_calls.count(n)==1 for n in calls)
    assert s3.objects[(plan['bucket'],obj['key'])][0]==content
    assert migrator.progress()['transfer_complete']


def test_progress_refresh_recovers_independently_of_hash_worker(monkeypatch):
    calls = []
    class Stop:
        finished = False
        def is_set(self):
            return self.finished
        def wait(self, seconds):
            assert seconds == 30
            self.finished = len(calls) == 2
    def discover(limit):
        calls.append(limit)
        if len(calls) == 1:
            raise TimeoutError('Transient S3 discovery interruption')
    monkeypatch.setattr(migration, 'discover', discover)
    migration.refresh_loop(Stop())
    assert calls == [500, 500]


def test_checkpoint_connection_closes_after_each_transaction(tmp_path):
    from scripts.migrate_inventory import connect
    with connect(tmp_path / 'checkpoint.sqlite') as connection:
        connection.execute('CREATE TABLE closed_safely(id INTEGER)')
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        connection.execute('SELECT 1')


def test_cold_plan_is_fetched_and_expanded_once_across_threads(migration_fixture,monkeypatch):
    import concurrent.futures
    import threading
    from collections import OrderedDict
    plan,_,_,_=migration_fixture;payload=db.encode(plan).encode();digest=hashlib.sha256(payload).hexdigest()
    registration={'bucket':plan['bucket'],'key':'exact-plan','version_id':'version-1',
                  'sha256':digest,'owner_id':plan['owner_id'],'plan_id':plan['plan_id']}
    started=threading.Event();release=threading.Event();barrier=threading.Barrier(6);fetches=[]
    monkeypatch.setattr(migration,'_PLAN_CACHE',OrderedDict())
    monkeypatch.setattr(migration,'client',lambda:None)
    def fetch(*args):
        fetches.append(args);started.set()
        assert release.wait(3)
        return json.loads(payload),digest
    monkeypatch.setattr(migration,'_get_json',fetch)
    def load():
        barrier.wait(timeout=3)
        return migration.load_plan(registration)
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futures=[pool.submit(load) for _ in range(6)]
        try:
            assert started.wait(3)
            time.sleep(.05)
            assert len(fetches)==1
        finally:
            release.set()
        loaded=[f.result(timeout=3) for f in futures]
    assert all(p is loaded[0] for p in loaded)
    assert len(fetches)==1 and len(migration._PLAN_CACHE)==1
    with pytest.raises(ValueError,match='identity'):
        migration.load_plan({**registration,'owner_id':'different-owner'})
    assert len(fetches)==1


def test_plan_cache_evicts_before_parsing_and_keeps_failed_identity_out(migration_fixture,monkeypatch):
    from collections import OrderedDict
    plan,_,_,_=migration_fixture
    monkeypatch.setattr(migration,'_PLAN_CACHE',OrderedDict())
    monkeypatch.setattr(migration,'client',lambda:None)
    plans={};fetches=[]
    for n in range(3):
        value={**plan,'plan_id':'migration_'+str(n)*24}
        payload=db.encode(value).encode();digest=hashlib.sha256(payload).hexdigest()
        plans['plan-'+str(n)]=(payload,digest)
    def fetch(s3,bucket,key,version):
        assert len(migration._PLAN_CACHE)<migration._PLAN_CACHE_LIMIT
        fetches.append(key);payload,digest=plans[key]
        return json.loads(payload),digest
    monkeypatch.setattr(migration,'_get_json',fetch)
    def registration(n):
        return {'bucket':plan['bucket'],'key':'plan-'+str(n),'version_id':'v1',
                'sha256':plans['plan-'+str(n)][1],'owner_id':plan['owner_id'],'plan_id':'migration_'+str(n)*24}
    migration.load_plan(registration(0));migration.load_plan(registration(1))
    migration.load_plan(registration(0))  # The recently used plan survives.
    migration.load_plan(registration(2))
    assert len(migration._PLAN_CACHE)==2
    assert {key[1] for key in migration._PLAN_CACHE}=={'plan-0','plan-2'}
    migration.load_plan(registration(1))
    assert fetches==['plan-0','plan-1','plan-2','plan-1']
    forged={**registration(0),'sha256':'f'*64}
    with pytest.raises(ValueError,match='identity'):migration.load_plan(forged)
    assert not any(key[3]=='f'*64 for key in migration._PLAN_CACHE)
