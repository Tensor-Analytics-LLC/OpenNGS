import base64
import hashlib
import json
import time
import pytest
from backend import db, migration


def test_exact_priority_claim_preserves_other_queue_records_and_verifies_bytes(client, owner, monkeypatch):
    content = b'exact original ontology\n'; digest = hashlib.sha256(content).hexdigest()
    pid = 'migration_' + 'a' * 24
    obj = {'sha256': digest, 'bytes': len(content), 'key': 'tenants/' + owner['id'] + '/source'}
    plan = {'bucket': 'test-cache', '_by_sha': {digest: obj}}
    with db.tx() as c:
        c.execute('INSERT INTO controls VALUES(?,?)', ('migration:plan:' + pid, '{}'))
        for value, updated, lease in [(digest, 2, 0), ('f' * 64, 1, 0)]:
            c.execute('INSERT INTO migration_objects VALUES(?,?,?,?,?,?,?,?,?,?)',
                      (pid, value, owner['id'], 'Verifying', db.encode({'version_id': 'original-v1'}), 0, 'original-v1', lease, None, updated))
    monkeypatch.setattr(migration, 'load_plan', lambda registration: plan)
    class S3:
        def head_object(self, **request):
            assert request['VersionId'] == 'original-v1'
            return {'ContentLength': len(content), 'VersionId': 'original-v1',
                    'Metadata': {'sha256': digest}, 'ChecksumType': 'FULL_OBJECT',
                    'ChecksumSHA256': base64.b64encode(bytes.fromhex(digest)).decode()}
    monkeypatch.setattr(migration, 'client', S3)
    def imported(plan_value, object_value, receipt, verified_digest, method):
        assert plan_value is plan and object_value is obj and verified_digest == digest
        assert method == 's3-full-object-sha256'
        return {'sha256': digest, 'state': 'Ready'}
    monkeypatch.setattr(migration, 'import_manifest', imported)
    assert migration.verify_one(pid, digest)['state'] == 'Ready'
    rows = {r['sha256']: r for r in db.query('SELECT * FROM migration_objects')}
    assert json.loads(rows[digest]['receipt'])['_verification_attempts'] == 1
    assert rows[digest]['lease_until'] > time.time()
    assert rows['f' * 64]['lease_until'] == 0 and rows['f' * 64]['updated'] == 1
    assert '_verification_attempts' not in json.loads(rows['f' * 64]['receipt'])


def test_priority_never_resets_a_lease_and_requires_both_exact_ids(client, owner):
    pid, digest = 'migration_' + 'b' * 24, 'c' * 64
    until = time.time() + 3600
    with db.tx() as c:
        c.execute('INSERT INTO migration_objects VALUES(?,?,?,?,?,?,?,?,?,?)',
                  (pid, digest, owner['id'], 'Verifying', '{}', 0, 'original-v1', until, None, 1))
    assert migration.verify_one(pid, digest) is None
    assert db.query('SELECT lease_until FROM migration_objects WHERE plan_id=? AND sha256=?', (pid, digest), one=True)['lease_until'] == until
    for args in [(pid, None), (None, digest), ('unregistered-uri', digest), (pid, 'wrong')]:
        with pytest.raises(ValueError):
            migration.verify_one(*args)
