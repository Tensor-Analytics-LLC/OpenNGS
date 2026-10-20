"""Resumable private SSD-to-AWS migration; originals are read-only throughout.

Prepare creates an immutable, versioned plan. Run uploads SHA-addressed objects
and immutable completion manifests. The AWS migration verifier checks raw SHA256
before registering Ready datasets. Re-running the command resumes checkpoints.
"""
import argparse
import base64
import concurrent.futures
import contextlib
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import threading
import time
from pathlib import Path

from botocore.config import Config
from botocore.exceptions import ClientError
from backend.config import aws_session

PART = 32 * 1024**2
LOCK = threading.RLock()


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024**2), b''):
            digest.update(block)
    return digest.hexdigest()


@contextlib.contextmanager
def connect(path):
    connection = sqlite3.connect(path, timeout=30)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA journal_mode=WAL')
        with connection:
            yield connection
    finally:
        connection.close()


def initialize(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS transfers(sha256 TEXT PRIMARY KEY,state TEXT NOT NULL,upload_id TEXT,version_id TEXT,error TEXT,updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS parts(sha256 TEXT NOT NULL,number INTEGER NOT NULL,etag TEXT NOT NULL,checksum TEXT NOT NULL,bytes INTEGER NOT NULL,PRIMARY KEY(sha256,number));
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        ''')
    os.chmod(path, 0o600)


def safe_name(name):
    return re.sub(r'[^A-Za-z0-9._-]', '_', name)[:150] or 'dataset.bin'


def source_identity(path, expected):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    value = os.fstat(fd)
    if not stat.S_ISREG(value.st_mode) or value.st_size != expected['bytes'] or value.st_mtime_ns != expected['mtime_ns']:
        os.close(fd)
        raise ValueError('Source identity changed; refresh the read-only inventory before migration')
    return fd


def build_plan(inventory, source_db, owner, bucket, kms, max_bytes):
    if not re.fullmatch(r'u_[a-f0-9]{32}', owner):
        raise ValueError('Use an existing exact OpenNGS owner identifier')
    with sqlite3.connect('file:' + str(Path(source_db).resolve()) + '?mode=ro', uri=True) as src, sqlite3.connect('file:' + str(Path(inventory).resolve()) + '?mode=ro', uri=True) as inv:
        src.row_factory = inv.row_factory = sqlite3.Row
        records = {row['id']: dict(row) for row in src.execute('SELECT * FROM assets')}
        grouped = {}
        for raw in inv.execute('SELECT * FROM files ORDER BY sha256,asset_id'):
            row = dict(raw)
            if row['state'] != 'Verified source' or not re.fullmatch(r'[a-f0-9]{64}', row['sha256'] or ''):
                raise ValueError('Every inventoried source must have a verified SHA-256')
            if row['user_id'] != owner or row['asset_id'] not in records:
                raise ValueError('Inventory ownership or original asset record is inconsistent')
            original = records[row['asset_id']]
            if original['path'] != row['path'] or original['bytes'] != row['bytes']:
                raise ValueError('Source registry changed since inventory')
            fd = source_identity(row['path'], row);os.close(fd)
            entry = grouped.setdefault(row['sha256'], {'sha256': row['sha256'], 'bytes': row['bytes'], 'aliases': []})
            if entry['bytes'] != row['bytes']:
                raise ValueError('Digest identity has inconsistent sizes')
            entry['aliases'].append({**original, 'metadata': json.loads(original['metadata']), 'mtime_ns': row['mtime_ns']})
        objects = []
        for digest, obj in sorted(grouped.items()):
            canonical = obj['aliases'][0]
            dataset_id = 'ds_migration_' + hashlib.sha256((owner + ':' + digest).encode()).hexdigest()[:40]
            obj.update(dataset_id=dataset_id, key=f'tenants/{owner}/datasets/{dataset_id}/{safe_name(canonical["name"])}', name=canonical['name'])
            objects.append(obj)
    total = sum(o['bytes'] for o in objects)
    if total > max_bytes:
        raise ValueError('Migration exceeds the reviewed storage envelope')
    core = {'schema': 1, 'owner_id': owner, 'bucket': bucket, 'kms': kms, 'objects': objects, 'total_bytes': total, 'inventory_sha256': file_hash(inventory), 'source_records_preserved': True}
    core['plan_id'] = 'migration_' + hashlib.sha256(encode(core).encode()).hexdigest()[:24]
    return core


def public_sources(plan, client):
    """Prefer exact release paths; byte equality is proved by the AWS verifier."""
    candidates = []
    for obj in plan['objects']:
        name = obj['name']
        if re.fullmatch(r'gnomad\.exomes\.v4\.1\.sites\.chr(?:[0-9]+|X|Y)\.vcf\.bgz(?:\.tbi)?', name):
            candidates.append((obj, 'gnomad-public-us-east-1', 'release/4.1/vcf/exomes/' + name))
    results = []
    for obj, bucket, key in candidates:
        try:
            head = client.head_object(Bucket=bucket, Key=key)
            if head['ContentLength'] != obj['bytes']:
                results.append({'name': obj['name'], 'state': 'SSD fallback', 'reason': 'Exact release AWS object size differs from local inventory'})
                continue
            obj['cloud_source'] = {'bucket': bucket, 'key': key, 'etag': head['ETag'], 'version_id': head.get('VersionId'), 'bytes': head['ContentLength'], 'provider': 'https://registry.opendata.aws/broad-gnomad/', 'release': '4.1', 'verification': 'Raw SHA256 must equal local source inventory before Ready'}
            results.append({'name': obj['name'], 'state': 'Conditional AWS copy', 'bytes': obj['bytes']})
        except ClientError as error:
            results.append({'name': obj['name'], 'state': 'SSD fallback', 'reason': error.response['Error']['Code']})
    # The public-object proof becomes part of the immutable plan identity.
    plan.pop('plan_id', None)
    plan['plan_id'] = 'migration_' + hashlib.sha256(encode(plan).encode()).hexdigest()[:24]
    return results


def forecast(plan):
    parts = sum(math.ceil(x['bytes'] / PART) if x['bytes'] > PART else 1 for x in plan['objects'])
    multipart = sum(x['bytes'] > PART for x in plan['objects'])
    writes = parts + multipart * 2 + len(plan['objects']) + 10
    reads = len(plan['objects']) * 4 + parts * 2
    # Conservatively count a KMS operation per storage request even with bucket keys.
    requests = writes * .005 / 1000 + reads * .0004 / 1000 + (writes + reads) * .03 / 10000
    return {'unique_objects': len(plan['objects']), 'source_aliases': sum(len(o['aliases']) for o in plan['objects']), 'bytes': plan['total_bytes'], 'gib': plan['total_bytes'] / 1024**3, 'storage_monthly_pretax_usd': plan['total_bytes'] / 1024**3 * .023, 'multipart_part_size': PART, 'maximum_in_flight_requests': 4, 'estimated_s3_kms_request_usd_before_retries': round(requests, 4), 'request_and_verification_ceiling_usd': 3, 'unmeasured_transfer_hours_at_mbps': {str(speed): round(plan['total_bytes'] * 8 / (speed * 1000000) / 3600, 2) for speed in [100, 300, 1000]}, 'aws_copy_objects': sum(bool(o.get('cloud_source')) for o in plan['objects']), 'aws_copy_bytes': sum(o['bytes'] for o in plan['objects'] if o.get('cloud_source')), 'verification': 'AWS raw SHA256 or S3 validated FULL_OBJECT SHA256 must match before Ready registration; no source deletions or release substitution'}


def missing(error):
    return isinstance(error, ClientError) and error.response['Error']['Code'] in ['404', 'NoSuchKey', 'NotFound', 'NoSuchVersion']


class Migrator:
    def __init__(self, plan, ledger, client=None):
        self.plan, self.ledger = plan, str(ledger)
        self.client = client or aws_session().client('s3', config=Config(connect_timeout=5, read_timeout=90, max_pool_connections=8, retries={'mode': 'adaptive', 'max_attempts': 3}))
        self.semaphore = threading.BoundedSemaphore(4)
        initialize(self.ledger)
        with connect(self.ledger) as c:
            old = c.execute("SELECT value FROM metadata WHERE key='plan_id'").fetchone()
            if old and old[0] != plan['plan_id']:
                raise ValueError('Checkpoint belongs to a different immutable migration plan')
            c.execute("INSERT OR IGNORE INTO metadata VALUES('plan_id',?)", (plan['plan_id'],))

    def row(self, digest):
        with connect(self.ledger) as c:
            row = c.execute('SELECT * FROM transfers WHERE sha256=?', (digest,)).fetchone()
            return dict(row) if row else None

    def save(self, digest, **changes):
        with LOCK, connect(self.ledger) as c:
            c.execute("INSERT OR IGNORE INTO transfers VALUES(?,'pending',NULL,NULL,NULL,?)", (digest, time.time()))
            c.execute('UPDATE transfers SET ' + ','.join(k + '=?' for k in changes) + ',updated=? WHERE sha256=?', (*changes.values(), time.time(), digest))

    def call(self, method, **kwargs):
        with self.semaphore:
            return getattr(self.client, method)(**kwargs)

    def existing(self, obj):
        try:
            head = self.call('head_object', Bucket=self.plan['bucket'], Key=obj['key'], ChecksumMode='ENABLED')
        except ClientError as error:
            if missing(error):
                return None
            raise
        if head['ContentLength'] != obj['bytes'] or head.get('Metadata', {}).get('sha256') != obj['sha256'] or not head.get('VersionId') or head['VersionId'] == 'null':
            raise ValueError('Existing destination does not match the immutable source identity')
        return head

    def upload_part(self, obj, source, upload_id, number, known):
        offset = (number - 1) * PART
        length = min(PART, obj['bytes'] - offset)
        if number in known:
            with connect(self.ledger) as c:
                old = c.execute('SELECT * FROM parts WHERE sha256=? AND number=?', (obj['sha256'], number)).fetchone()
            part = known[number]
            if old and old['bytes'] == length and part['Size'] == length and old['etag'] == part['ETag'] and old['checksum'] == part.get('ChecksumSHA256'):
                return {'PartNumber': number, 'ETag': old['etag'], 'ChecksumSHA256': old['checksum']}
        if obj.get('cloud_source'):
            origin = obj['cloud_source'];copy = {'Bucket': origin['bucket'], 'Key': origin['key']}
            if origin.get('version_id') and origin['version_id'] != 'null':
                copy['VersionId'] = origin['version_id']
            result = self.call('upload_part_copy', Bucket=self.plan['bucket'], Key=obj['key'], UploadId=upload_id, PartNumber=number, CopySource=copy, CopySourceIfMatch=origin['etag'], CopySourceRange=f'bytes={offset}-{offset + length - 1}')['CopyPartResult']
            checksum = result.get('ChecksumSHA256')
            if not checksum:
                raise ValueError('AWS multipart copy did not provide its SHA256 part checksum')
            with LOCK, connect(self.ledger) as c:
                c.execute('INSERT OR REPLACE INTO parts VALUES(?,?,?,?,?)', (obj['sha256'], number, result['ETag'], checksum, length))
            return {'PartNumber': number, 'ETag': result['ETag'], 'ChecksumSHA256': checksum}
        fd = source_identity(source['path'], source)
        try:
            block = os.pread(fd, length, offset)
            if len(block) != length:
                raise ValueError('Source truncated during upload')
            after = os.fstat(fd)
            if after.st_mtime_ns != source['mtime_ns'] or after.st_size != obj['bytes']:
                raise ValueError('Source changed while reading an upload part')
        finally:
            os.close(fd)
        checksum = base64.b64encode(hashlib.sha256(block).digest()).decode()
        response = self.call('upload_part', Bucket=self.plan['bucket'], Key=obj['key'], UploadId=upload_id, PartNumber=number, Body=block, ChecksumSHA256=checksum)
        if response.get('ChecksumSHA256') != checksum:
            raise ValueError('AWS did not confirm the uploaded part checksum')
        with LOCK, connect(self.ledger) as c:
            c.execute('INSERT OR REPLACE INTO parts VALUES(?,?,?,?,?)', (obj['sha256'], number, response['ETag'], checksum, length))
        return {'PartNumber': number, 'ETag': response['ETag'], 'ChecksumSHA256': checksum}

    def completion(self, obj, head):
        key = f"operations/checkpoints/migration/{self.plan['plan_id']}/uploaded/{obj['sha256']}.json"
        body = encode({'schema': 1, 'plan_id': self.plan['plan_id'], 'sha256': obj['sha256'], 'bytes': obj['bytes'], 'key': obj['key'], 'version_id': head['VersionId'], 's3_checksum_sha256': head.get('ChecksumSHA256'), 's3_checksum_type': head.get('ChecksumType'), 'uploaded_at': time.time()}).encode()
        try:
            self.call('put_object', Bucket=self.plan['bucket'], Key=key, Body=body, IfNoneMatch='*', ServerSideEncryption='aws:kms', SSEKMSKeyId=self.plan['kms'], BucketKeyEnabled=True, Metadata={'sha256': hashlib.sha256(body).hexdigest()})
        except ClientError as error:
            if error.response['Error']['Code'] != 'PreconditionFailed':
                raise
            old = self.call('get_object', Bucket=self.plan['bucket'], Key=key)
            stream = old['Body']
            try:
                previous = json.loads(stream.read(16385))
            finally:
                stream.close()
            if any(previous.get(k) != json.loads(body)[k] for k in ['sha256', 'key', 'version_id', 'bytes', 'plan_id']):
                raise ValueError('Existing cloud checkpoint identifies different object bytes/version')
        self.save(obj['sha256'], state='uploaded', version_id=head['VersionId'], error=None)

    def transfer(self, obj):
        digest = obj['sha256']
        try:
            state = self.row(digest)
            if state and state['state'] == 'uploaded':
                return
            source = obj['aliases'][0]
            if not obj.get('cloud_source'):
                fd = source_identity(source['path'], source);os.close(fd)
            head = self.existing(obj)
            if head:
                self.completion(obj, head)
                return
            metadata = {'sha256': digest, 'tenant': self.plan['owner_id'], 'dataset': obj['dataset_id'], 'migration': self.plan['plan_id']}
            if obj.get('cloud_source') and obj['bytes'] <= 5 * 1024**3:
                origin = obj['cloud_source'];copy = {'Bucket': origin['bucket'], 'Key': origin['key']}
                if origin.get('version_id') and origin['version_id'] != 'null':
                    copy['VersionId'] = origin['version_id']
                self.call('copy_object', Bucket=self.plan['bucket'], Key=obj['key'], CopySource=copy, CopySourceIfMatch=origin['etag'], ChecksumAlgorithm='SHA256', MetadataDirective='REPLACE', Metadata=metadata, ServerSideEncryption='aws:kms', SSEKMSKeyId=self.plan['kms'], BucketKeyEnabled=True)
            elif obj['bytes'] <= PART:
                fd = source_identity(source['path'], source)
                try:
                    with os.fdopen(fd, 'rb') as stream:
                        body = stream.read(PART + 1)
                except BaseException:
                    raise
                if len(body) != obj['bytes'] or hashlib.sha256(body).hexdigest() != digest:
                    raise ValueError('Current source bytes differ from the inventoried checksum')
                self.call('put_object', Bucket=self.plan['bucket'], Key=obj['key'], Body=body, IfNoneMatch='*', ChecksumSHA256=base64.b64encode(bytes.fromhex(digest)).decode(), ServerSideEncryption='aws:kms', SSEKMSKeyId=self.plan['kms'], BucketKeyEnabled=True, Metadata=metadata)
            else:
                upload = state.get('upload_id') if state else None
                if state and state['state'] == 'creating' and not upload:
                    found = self.call('list_multipart_uploads', Bucket=self.plan['bucket'], Prefix=obj['key']).get('Uploads', [])
                    found = [x for x in found if x['Key'] == obj['key']]
                    if len(found) != 1:
                        raise ValueError('Ambiguous multipart creation requires exact provider reconciliation')
                    upload = found[0]['UploadId']
                    self.save(digest, state='uploading', upload_id=upload)
                if not upload:
                    self.save(digest, state='creating')
                    response = self.call('create_multipart_upload', Bucket=self.plan['bucket'], Key=obj['key'], ChecksumAlgorithm='SHA256', ServerSideEncryption='aws:kms', SSEKMSKeyId=self.plan['kms'], BucketKeyEnabled=True, Metadata=metadata)
                    upload = response['UploadId']
                    self.save(digest, state='uploading', upload_id=upload)
                known = {};request = {'Bucket': self.plan['bucket'], 'Key': obj['key'], 'UploadId': upload}
                while True:
                    page = self.call('list_parts', **request)
                    known.update({p['PartNumber']: p for p in page.get('Parts', [])})
                    if not page.get('IsTruncated'):
                        break
                    request['PartNumberMarker'] = page['NextPartNumberMarker']
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(self.upload_part, obj, source, upload, n, known) for n in range(1, math.ceil(obj['bytes'] / PART) + 1)]
                    parts = [future.result() for future in futures]
                if not obj.get('cloud_source'):
                    fd = source_identity(source['path'], source);os.close(fd)
                self.save(digest, state='completing')
                self.call('complete_multipart_upload', Bucket=self.plan['bucket'], Key=obj['key'], UploadId=upload, MultipartUpload={'Parts': sorted(parts, key=lambda p: p['PartNumber'])}, IfNoneMatch='*')
            head = self.existing(obj)
            if not head:
                raise ValueError('Completed destination is not yet visible for verification')
            self.completion(obj, head)
        except Exception as error:
            state = self.row(digest)
            self.save(digest, state=state['state'] if state and state['state'] in ['creating', 'uploading', 'completing'] else 'blocked', error=str(error)[:500])
            raise

    def progress(self):
        sizes = {o['sha256']: o['bytes'] for o in self.plan['objects']}
        with connect(self.ledger) as c:
            rows = list(c.execute('SELECT sha256,state,error FROM transfers'))
            transferred_parts = int(c.execute('SELECT COALESCE(SUM(bytes),0) FROM parts').fetchone()[0])
        uploaded = [r for r in rows if r['state'] == 'uploaded']
        errors = [{'sha256': r['sha256'], 'state': r['state'], 'error': r['error']} for r in rows if r['error']]
        return {'plan_id': self.plan['plan_id'], 'updated': time.time(), 'total_objects': len(sizes), 'total_bytes': self.plan['total_bytes'], 'uploaded_objects': len(uploaded), 'uploaded_bytes': sum(sizes[r['sha256']] for r in uploaded), 'checkpointed_multipart_bytes': transferred_parts, 'blocked': errors[:30], 'transfer_complete': len(uploaded) == len(sizes), 'ready_status': 'Cloud verification and registry import are tracked separately', 'source_deleted': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'run', 'status'])
    parser.add_argument('--inventory', default='evidence/deployment/inventory.sqlite')
    parser.add_argument('--source-db', default='.state/studio.sqlite')
    parser.add_argument('--directory', default='.state/migration')
    parser.add_argument('--owner')
    parser.add_argument('--bucket')
    parser.add_argument('--kms')
    parser.add_argument('--max-bytes', type=int, default=500 * 1024**3)
    parser.add_argument('--limit', type=int)
    args = parser.parse_args();folder = Path(args.directory);folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    plan_path = folder / 'plan.json';ledger = folder / 'checkpoint.sqlite'
    if args.action == 'prepare':
        plan = build_plan(args.inventory, args.source_db, args.owner, args.bucket, args.kms, args.max_bytes)
        sources = public_sources(plan, aws_session().client('s3'))
        (folder / 'public-sources.json').write_text(encode(sources))
        if plan_path.exists() and json.loads(plan_path.read_text()) != plan:
            raise ValueError('Existing directory holds another immutable plan; use a new checkpoint directory')
        plan_path.write_text(encode(plan));plan_path.chmod(0o600)
        print(encode(forecast(plan)), flush=True)
        return
    plan = json.loads(plan_path.read_text());migrator = Migrator(plan, ledger)
    if args.action == 'status':
        print(encode(migrator.progress()), flush=True)
        return
    # Upload immutable, private plan so AWS can independently validate each upload.
    payload = plan_path.read_bytes();key = f"operations/checkpoints/migration/{plan['plan_id']}/plan.json"
    try:
        published = migrator.call('put_object', Bucket=plan['bucket'], Key=key, Body=payload, IfNoneMatch='*', ServerSideEncryption='aws:kms', SSEKMSKeyId=plan['kms'], BucketKeyEnabled=True, Metadata={'sha256': hashlib.sha256(payload).hexdigest()})
    except ClientError as error:
        if error.response['Error']['Code'] != 'PreconditionFailed':
            raise
        published = migrator.call('head_object', Bucket=plan['bucket'], Key=key)
        if published.get('Metadata', {}).get('sha256') != hashlib.sha256(payload).hexdigest():
            raise ValueError('Cloud plan differs from local immutable checkpoint')
    registration = {'plan_id': plan['plan_id'], 'owner_id': plan['owner_id'], 'bucket': plan['bucket'], 'key': key, 'version_id': published['VersionId'], 'sha256': hashlib.sha256(payload).hexdigest()}
    (folder / 'registration.json').write_text(encode(registration))
    objects = sorted(plan['objects'], key=lambda x: (not bool(x.get('cloud_source')), x['bytes']))
    if args.limit is not None:
        objects = sorted(plan['objects'],key=lambda x:x['bytes'])[:args.limit]
    stop_progress = threading.Event()
    def write_progress():
        progress = migrator.progress()
        temporary = folder / 'progress.new';temporary.write_text(encode(progress));temporary.replace(folder / 'progress.json')
        migrator.call('put_object', Bucket=plan['bucket'], Key=f"operations/checkpoints/migration/{plan['plan_id']}/progress.json", Body=encode(progress).encode(), ServerSideEncryption='aws:kms', SSEKMSKeyId=plan['kms'], BucketKeyEnabled=True)
        print(encode(progress), flush=True)
    def progress_loop():
        while not stop_progress.wait(15):
            try:write_progress()
            except Exception as error:print(encode({'event':'migration.progress.deferred','exception':type(error).__name__}),flush=True)
    publisher = threading.Thread(target=progress_loop,daemon=True);publisher.start()
    try:
        # Four objects share one four-request semaphore, including all part
        # workers; concurrency cannot multiply into sixteen S3 requests.
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(migrator.transfer, obj): obj for obj in objects}
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except Exception as error:
                    print(encode({'event': 'migration.object.deferred', 'sha256': futures[future]['sha256'], 'reason': str(error)[:300]}), flush=True)
    finally:
        stop_progress.set();publisher.join(timeout=120)
    progress = migrator.progress();(folder / 'progress.json').write_text(encode(progress));print(encode(progress), flush=True)


if __name__ == '__main__':
    main()
