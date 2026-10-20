"""Prioritize four exact original ClinVar/HPO migration objects.

Dry-run by default. Uses the already registered immutable migration plan and
ordinary completion receipts. Never opens or changes the shared transfer ledger,
resets a verifier lease, or replaces an existing object/version.
"""
import argparse
import base64
import contextlib
import concurrent.futures
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import threading
import time

from botocore.config import Config
from botocore.exceptions import ClientError
from botocore.exceptions import ConnectionClosedError, EndpointConnectionError, ReadTimeoutError
from backend.config import aws_session


APPROVED_PLAN = 'migration_765d67a4a7ddd10bfd877595'
APPROVED_PLAN_SHA256 = '41047a533687c94988ab048ea13d7877067b564b1c636ce6c0bf76e695b20ea4'
APPROVED_PLAN_VERSION = 'z6u0FaglZyTgxAnGAHUfUEmR04O2zss6'
APPROVED_OWNER = 'u_ea9418a6aad54dda84e4f940c6717060'
APPROVED_BUCKET = 'ngs-studio-cache-509505638328-us-east-1'
# Exact identities distinguish the original 2026-06-23 HPO ontology from older
# files with the same basename elsewhere in the source inventory.
REVIEWED = {
    '1fb70c6fd2a66df441b10c389a122ff476e0f7c39edf58967848eed5bc599050': ('clinvar.vcf.gz', 'clinvar', 192290992),
    'edb74cae1f47af2623fc52fdc89da308abf464ab99e723f3197d997741b58025': ('clinvar.vcf.gz.tbi', 'clinvar', 609919),
    'a5092cbdf605f568403cf7380d9173014015692433b2cc631bc5c1b053876b1b': ('hp.obo', 'hpo', 11222341),
    '3b646565695329aa399e937883c68d5d424d0df5eaab2f22baa0e08d44fdbe87': ('hp.json', 'hpo', 23019454),
}
MISSING = {'404', 'NoSuchKey', 'NotFound', 'NoSuchVersion'}
MAX_MANIFEST = 32 * 1024**2
PART_BYTES = 8 * 1024**2
MULTIPART_THRESHOLD = 8 * 1024**2


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def bounded_read(response, limit):
    stream = response['Body']
    try:
        if response.get('ContentLength', 0) > limit:
            raise ValueError('Cloud metadata exceeds its reviewed size limit')
        raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError('Cloud metadata exceeds its reviewed size limit')
        return raw
    finally:
        stream.close()


def selected_objects(plan):
    if (plan.get('plan_id'), plan.get('owner_id'), plan.get('bucket')) != (APPROVED_PLAN, APPROVED_OWNER, APPROVED_BUCKET):
        raise ValueError('Only the reviewed original migration plan is permitted')
    result = []
    for digest, (name, family, size) in REVIEWED.items():
        matches = [o for o in plan.get('objects', []) if o.get('sha256') == digest]
        if len(matches) != 1:
            raise ValueError('The registered plan must contain each exact reviewed source once')
        obj = matches[0]
        dataset = 'ds_migration_' + hashlib.sha256((APPROVED_OWNER + ':' + digest).encode()).hexdigest()[:40]
        expected_key = f'tenants/{APPROVED_OWNER}/datasets/{dataset}/{name}'
        aliases = obj.get('aliases', [])
        if (obj.get('name'), obj.get('bytes'), obj.get('dataset_id'), obj.get('key')) != (name, size, dataset, expected_key):
            raise ValueError('A reviewed migration object has inconsistent identity or tenant scope')
        if not aliases or any((a.get('name'), a.get('family'), a.get('user_id'), a.get('bytes')) != (name, family, APPROVED_OWNER, size) for a in aliases):
            raise ValueError('Reviewed name, family and owner must agree for every source alias')
        if obj.get('cloud_source'):
            raise ValueError('Priority upload must preserve the original local source')
        result.append(obj)
    if sum(o['bytes'] for o in result) > 228 * 1024**2:
        raise ValueError('Priority upload exceeds its reviewed source scope')
    return result


def load_reviewed_plan(folder, client):
    registration = json.loads((folder / 'registration.json').read_bytes())
    if (registration.get('plan_id'), registration.get('owner_id'), registration.get('bucket'), registration.get('sha256')) != (APPROVED_PLAN, APPROVED_OWNER, APPROVED_BUCKET, APPROVED_PLAN_SHA256):
        raise ValueError('Registration differs from the reviewed immutable plan')
    if registration.get('key') != f'operations/checkpoints/migration/{APPROVED_PLAN}/plan.json' or registration.get('version_id') != APPROVED_PLAN_VERSION:
        raise ValueError('Registration requires the exact immutable cloud manifest version')
    local_raw = (folder / 'plan.json').read_bytes()
    if len(local_raw) > MAX_MANIFEST or hashlib.sha256(local_raw).hexdigest() != APPROVED_PLAN_SHA256:
        raise ValueError('Local manifest differs from its registered SHA-256')
    # Registration already verified this exact cloud version's complete SHA-256
    # in AWS. S3 versions cannot be edited in place. Pin that reviewed VersionId
    # as well as the local bytes, then verify its immutable HEAD identity rather
    # than repeatedly downloading the 10-MB private source inventory.
    remote = client.head_object(Bucket=registration['bucket'], Key=registration['key'], VersionId=APPROVED_PLAN_VERSION)
    if (remote.get('VersionId') != APPROVED_PLAN_VERSION or remote.get('ContentLength') != len(local_raw)
            or remote.get('Metadata', {}).get('sha256') != APPROVED_PLAN_SHA256):
        raise ValueError('Cloud manifest version or checksum differs from the registered plan')
    plan = json.loads(local_raw)
    if sum(o['bytes'] for o in plan['objects']) != plan['total_bytes']:
        raise ValueError('Registered migration totals are inconsistent')
    return plan, selected_objects(plan)


def identity(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


@contextlib.contextmanager
def verified_source(obj):
    alias = obj['aliases'][0]
    fd = os.open(alias['path'], os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size != obj['bytes'] or before.st_mtime_ns != alias['mtime_ns']:
            raise ValueError('Original source identity changed; priority upload stopped')
        digest = hashlib.sha256()
        for block in iter(lambda: stream.read(4 * 1024**2), b''):
            digest.update(block)
        if digest.hexdigest() != obj['sha256'] or identity(os.fstat(stream.fileno())) != identity(before):
            raise ValueError('Original source does not match its inventoried SHA-256')
        stream.seek(0)
        yield stream
        if identity(os.fstat(stream.fileno())) != identity(before):
            raise ValueError('Original source identity changed during priority upload')


def expected_checksum(obj):
    return base64.b64encode(bytes.fromhex(obj['sha256'])).decode()


def metadata(plan, obj):
    return {'sha256': obj['sha256'], 'tenant': plan['owner_id'], 'dataset': obj['dataset_id'], 'migration': plan['plan_id']}


def verify_existing(head, plan, obj, *, composite=None):
    if head.get('ContentLength') != obj['bytes'] or not head.get('VersionId') or head['VersionId'] == 'null':
        raise ValueError('Existing cloud object differs from its exact migration identity')
    if any(head.get('Metadata', {}).get(k) != v for k, v in metadata(plan, obj).items()):
        raise ValueError('Existing cloud object metadata differs from the registered source')
    if composite is not None:
        if head.get('ChecksumType') != 'COMPOSITE' or head.get('ChecksumSHA256') != composite:
            raise ValueError('Existing cloud object differs from confirmed multipart SHA-256 checksums')
    elif head.get('ChecksumType', 'FULL_OBJECT') != 'FULL_OBJECT' or head.get('ChecksumSHA256') != expected_checksum(obj):
        raise ValueError('Existing cloud object lacks the exact full-object SHA-256')


def head_existing(client, plan, obj, *, composite=None):
    try:
        head = client.head_object(Bucket=plan['bucket'], Key=obj['key'], ChecksumMode='ENABLED')
    except ClientError as error:
        if error.response['Error']['Code'] in MISSING:
            return None
        raise
    if head.get('ChecksumType') == 'COMPOSITE' and composite is None:
        # Ordinary migration may have completed a different valid part layout.
        # Its composite digest is not the source SHA-256. Read its immutable
        # bytes before accepting it; never confuse metadata with checksum proof.
        verify_existing(head, plan, obj, composite=head.get('ChecksumSHA256'))
        response = client.get_object(Bucket=plan['bucket'], Key=obj['key'], VersionId=head['VersionId'])
        stream = response['Body'];digest = hashlib.sha256();total = 0
        try:
            if response.get('VersionId') != head['VersionId'] or response.get('ContentLength') != obj['bytes']:
                raise ValueError('Existing cloud multipart version differs from its declared identity')
            for block in iter(lambda: stream.read(4 * 1024**2), b''):
                total += len(block)
                if total > obj['bytes']:
                    raise ValueError('Existing cloud multipart object exceeded its exact length')
                digest.update(block)
        finally:
            stream.close()
        if total != obj['bytes'] or digest.hexdigest() != obj['sha256']:
            raise ValueError('Existing cloud multipart object differs from original SHA-256')
    else:
        verify_existing(head, plan, obj, composite=composite)
    return head


class MultipartCheckpoint:
    """One private per-object checkpoint, independent of migration's ledger."""
    def __init__(self, folder, plan, obj):
        self.path = folder / (obj['sha256'] + '.json')
        self.binding = {'plan_id': plan['plan_id'], 'bucket': plan['bucket'], 'key': obj['key'],
                        'sha256': obj['sha256'], 'bytes': obj['bytes'], 'part_bytes': PART_BYTES}
        self.lock = threading.RLock()
        self.value = json.loads(self.path.read_bytes()) if self.path.exists() else {**self.binding, 'state': 'pending', 'parts': {}}
        if any(self.value.get(k) != v for k, v in self.binding.items()):
            raise ValueError('Private priority checkpoint belongs to another exact source')

    def save(self, **changes):
        with self.lock:
            self.value.update(changes)
            raw = encode(self.value)
            temporary = self.path.with_suffix('.new')
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw);stream.flush();os.fsync(stream.fileno())
            temporary.replace(self.path)


@contextlib.contextmanager
def private_checkpoint(folder, plan, obj):
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    lockfile = folder / (obj['sha256'] + '.lock')
    with lockfile.open('a+b') as lock:
        os.chmod(lockfile, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another priority uploader owns this exact private checkpoint') from None
        try:
            yield MultipartCheckpoint(folder, plan, obj)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def multipart_upload(client, plan, obj, stream, folder):
    with private_checkpoint(folder, plan, obj) as checkpoint:
        existing = head_existing(client, plan, obj)
        if existing is not None:
            checkpoint.save(state='completed', version_id=existing['VersionId'])
            return existing
        state = checkpoint.value
        if state['state'] in ['creating', 'completing', 'completed']:
            # A lost create/complete response is not permission to issue another
            # mutation. HEAD on the next invocation may recover the final object.
            raise ValueError('Ambiguous priority multipart mutation requires operator reconciliation; no mutation repeated')
        upload = state.get('upload_id')
        if not upload:
            checkpoint.save(state='creating')
            response = client.create_multipart_upload(
                Bucket=plan['bucket'], Key=obj['key'], ChecksumAlgorithm='SHA256',
                ServerSideEncryption='aws:kms', SSEKMSKeyId=plan['kms'], BucketKeyEnabled=True,
                Metadata=metadata(plan, obj))
            upload = response['UploadId']
            checkpoint.save(state='uploading', upload_id=upload)
        known = {};request = {'Bucket': plan['bucket'], 'Key': obj['key'], 'UploadId': upload}
        while True:
            page = client.list_parts(**request)
            known.update({p['PartNumber']: p for p in page.get('Parts', [])})
            if not page.get('IsTruncated'):
                break
            request['PartNumberMarker'] = page['NextPartNumberMarker']
        part_count = (obj['bytes'] + PART_BYTES - 1) // PART_BYTES
        if any(n < 1 or n > part_count for n in known):
            raise ValueError('Private priority multipart upload has unexpected part numbers')

        def part(number):
            offset = (number - 1) * PART_BYTES
            length = min(PART_BYTES, obj['bytes'] - offset)
            raw = os.pread(stream.fileno(), length, offset)
            if len(raw) != length:
                raise ValueError('Original priority source truncated while reading a part')
            checksum = base64.b64encode(hashlib.sha256(raw).digest()).decode()
            previous = known.get(number)
            if previous and previous.get('Size') == length and previous.get('ChecksumSHA256') == checksum:
                response = previous
            else:
                for attempt in range(3):
                    try:
                        response = client.upload_part(Bucket=plan['bucket'], Key=obj['key'], UploadId=upload,
                                                      PartNumber=number, Body=raw, ChecksumSHA256=checksum)
                        break
                    except (ConnectionClosedError, EndpointConnectionError, ReadTimeoutError):
                        # Same upload ID, part number, bytes and checksum: an
                        # ambiguous part response can safely replace only those
                        # identical bytes. Never retry upload creation/completion.
                        if attempt == 2:
                            raise
                        time.sleep(.25 * (attempt + 1))
                if response.get('ChecksumSHA256') != checksum:
                    raise ValueError('Cloud did not confirm the original priority part SHA-256')
            result = {'PartNumber': number, 'ETag': response['ETag'], 'ChecksumSHA256': checksum}
            with checkpoint.lock:
                parts = dict(checkpoint.value['parts']);parts[str(number)] = {**result, 'bytes': length}
                checkpoint.save(parts=parts)
            return result

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            parts = list(pool.map(part, range(1, part_count + 1)))
        composite = base64.b64encode(hashlib.sha256(b''.join(base64.b64decode(p['ChecksumSHA256']) for p in parts)).digest()).decode() + '-' + str(len(parts))
        checkpoint.save(state='completing', composite_sha256=composite)
        try:
            response = client.complete_multipart_upload(
                Bucket=plan['bucket'], Key=obj['key'], UploadId=upload,
                MultipartUpload={'Parts': parts}, IfNoneMatch='*')
        except ClientError as error:
            if error.response['Error']['Code'] != 'PreconditionFailed':
                raise
            head = head_existing(client, plan, obj)
            if head is None:
                raise ValueError('Conditional priority multipart race has no verifiable winner')
            # This upload ID came only from this private checkpoint. The normal
            # worker's multipart state and upload IDs are never touched.
            client.abort_multipart_upload(Bucket=plan['bucket'], Key=obj['key'], UploadId=upload)
        else:
            version = response.get('VersionId')
            if not version or version == 'null':
                raise ValueError('Priority multipart completion returned no immutable version')
            head = client.head_object(Bucket=plan['bucket'], Key=obj['key'], VersionId=version, ChecksumMode='ENABLED')
            verify_existing(head, plan, obj, composite=composite)
        checkpoint.save(state='completed', version_id=head['VersionId'])
        return head


def receipt_identity(plan, obj, head):
    return {'plan_id': plan['plan_id'], 'sha256': obj['sha256'], 'bytes': obj['bytes'], 'key': obj['key'], 'version_id': head['VersionId']}


def publish_receipt(client, plan, obj, head):
    key = f"operations/checkpoints/migration/{plan['plan_id']}/uploaded/{obj['sha256']}.json"
    expected = receipt_identity(plan, obj, head)
    body = encode({'schema': 1, **expected, 's3_checksum_sha256': head['ChecksumSHA256'], 's3_checksum_type': head.get('ChecksumType', 'FULL_OBJECT'), 'uploaded_at': time.time()})
    try:
        client.put_object(Bucket=plan['bucket'], Key=key, Body=body, IfNoneMatch='*',
                          ServerSideEncryption='aws:kms', SSEKMSKeyId=plan['kms'], BucketKeyEnabled=True,
                          Metadata={'sha256': hashlib.sha256(body).hexdigest()})
    except ClientError as error:
        if error.response['Error']['Code'] != 'PreconditionFailed':
            raise
        previous = json.loads(bounded_read(client.get_object(Bucket=plan['bucket'], Key=key), 16384))
        if any(previous.get(k) != v for k, v in expected.items()):
            raise ValueError('Existing completion receipt identifies another immutable version')


def prioritize(client, plan, objects, *, execute=False, checkpoint_directory=Path('.state/priority-migration')):
    results = []
    for obj in objects:
        with verified_source(obj) as stream:
            head = head_existing(client, plan, obj)
            existed = head is not None
            if execute and head is None:
                if obj['bytes'] > MULTIPART_THRESHOLD:
                    head = multipart_upload(client, plan, obj, stream, Path(checkpoint_directory))
                else:
                    try:
                        response = client.put_object(
                            Bucket=plan['bucket'], Key=obj['key'], Body=stream, ContentLength=obj['bytes'], IfNoneMatch='*',
                            ChecksumSHA256=expected_checksum(obj), ServerSideEncryption='aws:kms', SSEKMSKeyId=plan['kms'],
                            BucketKeyEnabled=True, Metadata=metadata(plan, obj))
                    except ClientError as error:
                        if error.response['Error']['Code'] != 'PreconditionFailed':
                            raise
                        # Another ordinary migration upload won the race. Verify
                        # its immutable bytes; never create another version.
                        head = head_existing(client, plan, obj)
                        if head is None:
                            raise ValueError('Conditional race has no verifiable cloud object')
                    else:
                        if not response.get('VersionId') or response['VersionId'] == 'null':
                            raise ValueError('Priority upload did not return an immutable version')
                        head = client.head_object(Bucket=plan['bucket'], Key=obj['key'], VersionId=response['VersionId'], ChecksumMode='ENABLED')
                        verify_existing(head, plan, obj)
        # The source identity check after the PUT must pass before announcing a
        # completion receipt to discovery and the normal cloud verifier.
        if execute:
            publish_receipt(client, plan, obj, head)
        results.append({'name': obj['name'], 'sha256': obj['sha256'], 'dataset_id': obj['dataset_id'], 'bytes': obj['bytes'],
                        'state': 'Uploaded; awaiting ordinary cloud discovery/verification' if execute else 'Verified existing object' if existed else 'Planned conditional original upload',
                        'version_id': head['VersionId'] if head else None})
    return {'plan_id': plan['plan_id'], 'owner_id': plan['owner_id'], 'aws_mutations': execute,
            'shared_ledger_modified': False, 'leases_reset': False, 'objects': results,
            'bytes': sum(o['bytes'] for o in objects), 'scope': 'Exact original ClinVar and HPO migration objects; ordinary verifier controls Ready status'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', default='.state/migration')
    parser.add_argument('--checkpoint-directory', default='.state/priority-migration')
    parser.add_argument('--sha256', choices=sorted(REVIEWED), action='append', help='Only these reviewed source identities; may be repeated')
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    # A single conditional mutation may outlive the usual short API timeout.
    # Do not blindly retry an ambiguous PUT; rerun first verifies HEAD identity.
    client = aws_session().client('s3', config=Config(connect_timeout=5, read_timeout=900, retries={'total_max_attempts': 1}))
    plan, objects = load_reviewed_plan(Path(args.directory), client)
    objects = sorted([o for o in objects if not args.sha256 or o['sha256'] in args.sha256], key=lambda o: o['bytes'])
    print(json.dumps(prioritize(client, plan, objects, execute=args.execute, checkpoint_directory=Path(args.checkpoint_directory)), indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # OSError/network errors may embed private local paths in their text.
        # Keep CLI diagnostics path-free; no credentials or source paths escape.
        raise SystemExit('Priority migration stopped (' + type(error).__name__ + '). No existing object was replaced.')
