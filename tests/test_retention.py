import json
import time
from datetime import datetime, timezone

import pytest
from botocore.exceptions import ClientError
from backend import db, retention, entitlements


def missing():
    return ClientError({'Error': {'Code': 'NoSuchVersion'}}, 'HeadObject')


class VersionedS3:
    def __init__(self):
        self.objects = {}
        self.deleted = []
        self.fail_after_delete = False

    def add(self, bucket, key, version='v1', size=200, modified=None):
        self.objects[(bucket, key, version)] = {'VersionId': version, 'ContentLength': size, 'ETag': '"verified"', 'LastModified': datetime.fromtimestamp(modified or time.time() - 15 * retention.DAY, timezone.utc)}

    def list_object_versions(self, Bucket, Prefix, MaxKeys=1000, **kwargs):
        rows = [{'Key': key, 'VersionId': version, 'Size': obj['ContentLength'], 'ETag': obj['ETag'], 'LastModified': obj['LastModified']} for (bucket, key, version), obj in self.objects.items() if bucket == Bucket and key.startswith(Prefix)]
        return {'Versions': rows[:MaxKeys], 'IsTruncated': len(rows) > MaxKeys}

    def head_object(self, Bucket, Key, VersionId):
        if self.fail_after_delete and (Bucket, Key, VersionId) in self.deleted:
            self.fail_after_delete = False
            raise TimeoutError('Provider response lost after deletion')
        try:
            return self.objects[(Bucket, Key, VersionId)]
        except KeyError:
            raise missing()

    def delete_object(self, Bucket, Key, VersionId):
        ident = Bucket, Key, VersionId
        self.deleted.append(ident)
        self.objects.pop(ident, None)
        return {'VersionId': VersionId}


@pytest.fixture
def retention_setup(client, owner, monkeypatch):
    with db.tx() as c:
        for table in ['retention_tombstones', 'retention_leases', 'retention_pins', 'retention_objects', 'retention_plans', 'node_cache', 'demo_tasks', 'demo_cache']:
            c.execute('DELETE FROM ' + table)
    s3 = VersionedS3()
    monkeypatch.setattr(retention, 's3_client', lambda: s3)
    monkeypatch.setattr(retention, '_providers_stopped', lambda run: None)
    uid, rid = owner['id'], 'run_expired'
    prefix = 'tenants/' + uid + '/runs/' + rid + '/'
    graph = {'nodes': [], 'edges': [], '_execution': {'access': 'research-pro', 'allowance': {'retention_days': 7}, 'cloud_settings': {'s3_bucket': 'test-retention'}}}
    finished = time.time() - 12 * retention.DAY
    with db.tx() as c:
        c.execute("INSERT INTO runs(id,user_id,operation_id,state,graph,created,finished,reserved) VALUES(?,?,?,'succeeded',?,?,?,1)", (rid, uid, 'retention-fixture-op', json.dumps(graph), finished - 100, finished))
        c.execute('INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?,?,?,?)', ('artifact_expired', uid, rid, 'n1', 'result.vcf', 's3://test-retention/' + prefix + 'outputs/result.vcf', 'VCF', 200, 'sha256:' + 'a' * 64, json.dumps({'version_id': 'v1'}), finished))
    s3.add('test-retention', prefix + 'outputs/result.vcf')
    s3.add('test-retention', 'tenants/' + uid + '/datasets/source/source.fastq', size=10000)
    return uid, rid, prefix, s3


def test_retention_exact_versions_releases_only_derived_quota(retention_setup):
    uid, rid, prefix, s3 = retention_setup
    with db.tx() as c:
        before = entitlements.storage_exposure(c, uid)
    assert before == 200
    assert retention.discover() == 1
    assert retention.process_one()['state'] == 'inventory'
    assert retention.process_one()['state'] == 'deleted'
    assert s3.deleted == [('test-retention', prefix + 'outputs/result.vcf', 'v1')]
    assert len(s3.objects) == 1 and '/datasets/' in next(iter(s3.objects))[1]
    assert db.query('SELECT * FROM runs WHERE id=?', (rid,), one=True)
    assert db.query('SELECT * FROM retention_tombstones WHERE id=?', ('artifact_expired',), one=True)
    assert db.query('SELECT * FROM artifacts WHERE id=?', ('artifact_expired',), one=True) is None
    with db.tx() as c:
        assert entitlements.storage_exposure(c, uid) == 0
    with pytest.raises(ValueError, match='expired'):
        retention.ensure_available(uid, 'artifact_expired')


def test_retention_protects_saved_workflows_pins_reads_and_active_work(retention_setup):
    uid, rid, prefix, s3 = retention_setup
    retention.discover()
    retention.pin(uid, rid, True, 'Keep for manuscript review')
    assert retention.process_one()['state'] == 'protected'
    assert not s3.deleted
    retention.pin(uid, rid, False, 'Release manuscript results')
    with db.tx() as c:
        c.execute('UPDATE retention_plans SET lease_until=0')
        c.execute('INSERT INTO graphs VALUES(?,?,?,1,?,?)', ('saved', uid, 'Reusable analysis', json.dumps({'nodes': [{'params': {'asset_id': 'artifact_expired'}}]}), time.time()))
    assert 'saved workflow' in retention.process_one()['reason']
    with db.tx() as c:
        c.execute('DELETE FROM graphs WHERE id=?', ('saved',))
        c.execute('UPDATE retention_plans SET lease_until=0')
    retention.lease(uid, 'artifact_expired')
    assert 'read is in progress' in retention.process_one()['reason']
    with db.tx() as c:
        c.execute('DELETE FROM retention_leases')
        c.execute('UPDATE retention_plans SET lease_until=0')
        c.execute("INSERT INTO runs(id,user_id,operation_id,state,graph,created,reserved) VALUES(?,?,?,'queued',?,?,1)", ('active', uid, 'active-op', json.dumps({'_execution': {'assets': {'input': {'path': 's3://test-retention/' + prefix + 'outputs/result.vcf'}}}}), time.time()))
    assert 'Accepted or interrupted' in retention.process_one()['reason']
    assert not s3.deleted


def test_partial_delete_restarts_from_checkpoint_without_releasing_early(retention_setup):
    uid, rid, prefix, s3 = retention_setup
    retention.discover()
    retention.process_one()
    s3.fail_after_delete = True
    assert retention.process_one()['state'] == 'deferred'
    assert db.query('SELECT * FROM artifacts WHERE id=?', ('artifact_expired',), one=True)
    with pytest.raises(ValueError, match='expired'):
        retention.lease(uid, 'artifact_expired')
    with db.tx() as c:
        c.execute('UPDATE retention_plans SET lease_until=0')
    assert retention.process_one()['state'] == 'deleted'
    assert len(s3.deleted) == 1  # Missing exact version is a safe resumed success.


def test_scope_and_unknown_provider_state_fail_closed(retention_setup, monkeypatch):
    uid, rid, prefix, s3 = retention_setup
    retention.discover()
    def unresolved(run):
        raise ValueError('Unknown provider state requires reconciliation')
    monkeypatch.setattr(retention, '_providers_stopped', unresolved)
    assert retention.process_one()['state'] == 'deferred'
    assert not s3.deleted
    plan = db.query('SELECT * FROM retention_plans WHERE run_id=?', (rid,), one=True)
    with pytest.raises(ValueError, match='escaped'):
        retention._delete_object({'bucket': 'test-retention', 'object_key': 'tenants/another/runs/x/output.vcf', 'version_id': 'v1'}, plan, s3)


def test_recent_cache_preserved_and_expired_cache_removed(retention_setup):
    uid, rid, prefix, s3 = retention_setup
    outputs = {'output': [{'path': 's3://test-retention/' + prefix + 'outputs/result.vcf'}]}
    with db.tx() as c:
        c.execute('INSERT INTO node_cache VALUES(?,?,?,?,?,1)', (uid, 'reusable', json.dumps(outputs), time.time() - 20 * retention.DAY, time.time()))
    retention.discover()
    assert 'cache' in retention.process_one()['reason']
    with db.tx() as c:
        c.execute('UPDATE node_cache SET last_hit=?', (time.time() - 20 * retention.DAY,))
        c.execute('UPDATE retention_plans SET lease_until=0')
    retention.process_one()
    assert retention.process_one()['state'] == 'deleted'
    assert not db.query('SELECT * FROM node_cache WHERE user_id=?', (uid,))
    with pytest.raises(ValueError, match='expired'):
        retention.ensure_outputs(uid, outputs)


def test_frozen_longer_retention_is_not_shortened_by_downgrade(retention_setup):
    uid, rid, prefix, s3 = retention_setup
    run = db.query('SELECT * FROM runs WHERE id=?', (rid,), one=True)
    graph = json.loads(run['graph']);graph['_execution']['allowance']['retention_days'] = 30
    with db.tx() as c:
        c.execute('UPDATE runs SET graph=? WHERE id=?', (json.dumps(graph), rid))
    assert retention.discover() == 0
    assert not s3.deleted


def test_service_disabled_without_policy_activation(retention_setup):
    assert retention.tick() == {'state': 'disabled'}
    assert not db.query('SELECT * FROM retention_plans')


def test_operator_qualification_targets_only_named_run(retention_setup):
    uid, rid, prefix, s3 = retention_setup
    retention.discover()
    assert retention.process_one(run_id='different-run') is None
    assert not s3.deleted
    assert retention.process_one(run_id=rid)['state'] == 'inventory'
    assert retention.process_one(run_id=rid)['state'] == 'deleted'
