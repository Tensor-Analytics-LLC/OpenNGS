"""Acquire reviewed reference bundles inside AWS; dry-run unless --execute.

No large local files are created. Public inputs are exact version/ETag copies;
the one reviewed derived dictionary is streamed from an immutable AWS copy.
"""
import argparse
import base64
import hashlib
import json
import os
import time
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler

from botocore.config import Config
from botocore.exceptions import ClientError
from backend import cloud_references as refs
from backend.config import aws_session

CHUNK_BYTES = 4*1024*1024
MAX_LINE_BYTES = 1024*1024


def verify_cloud_runtime(release):
    """Require EC2 IMDSv2 identity in the configured cache's KMS account/region."""
    opener = build_opener(ProxyHandler({}))
    base = 'http://169.254.169.254/latest/'
    try:
        with opener.open(Request(base+'api/token', method='PUT', headers={'X-aws-ec2-metadata-token-ttl-seconds': '60'}), timeout=2) as response:
            token = response.read(4096).decode()
        if not token or len(token) >= 4096:
            raise ValueError('Invalid EC2 identity token')
        with opener.open(Request(base+'dynamic/instance-identity/document', headers={'X-aws-ec2-metadata-token': token}), timeout=2) as response:
            identity = json.loads(response.read(16384))
    except Exception as error:
        raise ValueError('Reference execution requires the deployed AWS EC2 host; local acquisition is forbidden') from error
    arn = release.get('kms', '').split(':')
    if (len(arn) < 6 or arn[:3] != ['arn', 'aws', 'kms'] or not identity.get('instanceId')
            or identity.get('accountId') != arn[4] or identity.get('region') != arn[3]):
        raise ValueError('AWS execution identity differs from the reviewed deployment account/region')
    if not release.get('bucket') or os.environ.get('NGS_BUCKET') not in [None, release['bucket']]:
        raise ValueError('Reference output must use the configured deployment cache bucket')
    return {'instance_id': identity.get('instanceId'), 'account': identity['accountId'], 'region': identity['region']}


def _checksum(value):
    try:
        digest = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as error:
        raise ValueError('A valid full-object SHA-256 checksum is required') from error
    if len(digest) != 32:
        raise ValueError('A valid full-object SHA-256 checksum is required')
    return digest


def _verify_cached(head, obj):
    if (head.get('ContentLength') != obj['bytes'] or head.get('VersionId') != obj['version_id']
            or not obj['version_id'] or obj['version_id'] == 'null'
            or head.get('ChecksumSHA256') != obj['checksum_sha256']
            or head.get('ChecksumType', 'FULL_OBJECT') != 'FULL_OBJECT'
            or obj.get('checksum_type') != 'FULL_OBJECT'):
        raise ValueError('Previously acquired immutable reference length/version/checksum changed')
    _checksum(obj['checksum_sha256'])


def _read_small(client, obj, maximum):
    response = client.get_object(Bucket=obj['bucket'], Key=obj['key'], VersionId=obj['version_id'])
    try:
        body = response['Body'].read(maximum+1)
    finally:
        response['Body'].close()
    if len(body) > maximum or len(body) != obj['bytes'] or hashlib.sha256(body).digest() != _checksum(obj['checksum_sha256']):
        raise ValueError('Small immutable reference companion differs from its acquired identity')
    return body


def _head_if_present(client, bucket, key):
    try:
        return client.head_object(Bucket=bucket, Key=key, ChecksumMode='ENABLED')
    except ClientError as error:
        if error.response['Error']['Code'] not in ['404', 'NoSuchKey', 'NotFound']:
            raise
        return None


def recover_current_copy(source_client, destination, item, release, runtime):
    """Prove an interrupted copy's exact bytes without overwriting its version.

    A new conditional GET must identify the pinned public source version. Its
    full streaming SHA-256 must match S3's FULL_OBJECT destination checksum.
    Provider HEAD identities bracket the stream; no source bytes are retained.
    """
    if (item['bucket'], item['key']) not in refs.CURRENT_ONLY_SOURCES:
        raise ValueError('Byte-equivalence recovery is restricted to reviewed current-only sources')
    if runtime.get('region') != 'us-east-1' or not runtime.get('instance_id'):
        raise ValueError('Byte-equivalence recovery must execute inside the deployed AWS region')
    source = {'Bucket': item['bucket'], 'Key': item['key']}
    def identity(head):
        return {'bytes': head.get('ContentLength'), 'etag': head.get('ETag'), 'version_id': head.get('VersionId')}
    expected = {'bytes': item['bytes'], 'etag': item['etag'], 'version_id': item['version']}
    before = identity(source_client.head_object(**source))
    if before != expected:
        raise ValueError('Public source changed before interrupted-copy recovery')
    wanted = _checksum(destination.get('ChecksumSHA256'))
    version = destination.get('VersionId')
    metadata = destination.get('Metadata', {})
    if (destination.get('ContentLength') != item['bytes'] or not version or version == 'null'
            or destination.get('ChecksumType', 'FULL_OBJECT') != 'FULL_OBJECT'
            or metadata.get('source-bucket') != item['bucket']
            or metadata.get('source-etag') != item['etag'].strip('"')
            or metadata.get('source-version') != item['version']):
        raise ValueError('Interrupted destination identity differs; preserved for operator review')
    response = source_client.get_object(**source, IfMatch=item['etag'])
    stream = response['Body'];value = hashlib.sha256();total = 0
    try:
        if identity(response) != expected:
            raise ValueError('Public GET did not return the exact reviewed immutable source version')
        for raw in iter(lambda: stream.read(CHUNK_BYTES), b''):
            total += len(raw)
            if total > item['bytes']:
                raise ValueError('Recovery source exceeded its reviewed byte ceiling')
            value.update(raw)
    finally:
        stream.close()
    after = identity(source_client.head_object(**source))
    if after != expected or total != item['bytes'] or value.digest() != wanted:
        raise ValueError('Interrupted destination is not byte-identical to the exact pinned public version')
    return {'method': 'aws-current-source-byte-equivalence-v1', 'source_before': before, 'source_after': after,
            'source_get_version_id': response['VersionId'], 'destination_version_id': version,
            'source_sha256': value.hexdigest(), 'bytes': total, 'runtime': runtime}


def derive_dictionary(chunks, fai_bytes, expected_bytes, expected_sha256, *,
                      max_contigs=1024, dictionary_max_bytes=refs.DICTIONARY_MAX_BYTES):
    """Bounded streaming FASTA validation and deterministic SAM dictionary.

    Validate every FAI field, complete raw-file SHA-256 and length. Only a line,
    per-contig digests, and the small dictionary remain in memory.
    """
    try:
        fai = [(fields[0], *(int(v) for v in fields[1:]))
               for fields in (line.split('\t') for line in fai_bytes.decode('ascii').splitlines())]
    except (ValueError, UnicodeError) as error:
        raise ValueError('Invalid exact FASTA index') from error
    if (type(max_contigs) is not int or max_contigs <= 0
            or type(dictionary_max_bytes) is not int or dictionary_max_bytes <= 0):
        raise ValueError('Dictionary verification bounds must be positive integers')
    if not fai or len(fai) > max_contigs or any(len(row) != 5 or row[1] <= 0 for row in fai) or len({row[0] for row in fai}) != len(fai):
        raise ValueError('Invalid exact FASTA index')
    rows, pending, total, offset = [], b'', 0, 0
    digest = hashlib.sha256()
    current = None

    def finish():
        if current is None:
            return
        actual = (current['name'], current['length'], current['offset'], current['line_bases'], current['line_width'])
        if len(rows) >= len(fai) or actual != fai[len(rows)]:
            raise ValueError('Derived sequence identity differs from exact FASTA index lengths/order/offsets/line widths')
        rows.append((current['name'], current['length'], current['md5'].hexdigest()))

    def line(raw):
        nonlocal current, offset
        bases = raw.rstrip(b'\r\n')
        if raw.startswith(b'>'):
            finish()
            try:
                name = bases[1:].split()[0].decode('ascii')
            except (IndexError, UnicodeError) as error:
                raise ValueError('Invalid FASTA contig name') from error
            if not name or any(ch.isspace() for ch in name):
                raise ValueError('Invalid FASTA contig name')
            current = {'name': name, 'length': 0, 'offset': offset+len(raw), 'line_bases': None,
                       'line_width': None, 'previous_short': False, 'md5': hashlib.md5()}
        else:
            sequence = bases.upper()
            if current is None or not sequence or sequence.translate(None, b'ACGTRYSWKMBDHVN'):
                raise ValueError('Invalid FASTA sequence line')
            if current['previous_short']:
                raise ValueError('An interior FASTA line differs from its FAI line width')
            if current['line_bases'] is None:
                current['line_bases'], current['line_width'] = len(sequence), len(raw)
            elif len(sequence) > current['line_bases'] or (len(sequence) == current['line_bases'] and len(raw) not in [current['line_width'], len(sequence)]):
                raise ValueError('FASTA line width differs from exact index')
            current['previous_short'] = len(sequence) < current['line_bases'] or not raw.endswith(b'\n')
            current['length'] += len(sequence)
            current['md5'].update(sequence)
        offset += len(raw)

    for chunk in chunks:
        if not isinstance(chunk, bytes) or len(chunk) > CHUNK_BYTES:
            raise ValueError('FASTA stream chunks must be bounded to 4 MiB')
        total += len(chunk)
        if total > expected_bytes:
            raise ValueError('FASTA stream exceeds its exact acquired length')
        digest.update(chunk)
        lines = (pending+chunk).split(b'\n')
        pending = lines.pop()
        for raw in lines:
            if len(raw) > MAX_LINE_BYTES:
                raise ValueError('FASTA line exceeds the bounded parser allowance')
            line(raw+b'\n')
        if len(pending) > MAX_LINE_BYTES:
            raise ValueError('FASTA line exceeds the bounded parser allowance')
    if pending:
        line(pending)
    finish()
    if total != expected_bytes or digest.digest() != _checksum(expected_sha256) or len(rows) != len(fai):
        raise ValueError('FASTA whole-object SHA-256, byte length or contig count differs')
    result = ('@HD\tVN:1.6\tSO:unsorted\n'+''.join(f'@SQ\tSN:{name}\tLN:{length}\tM5:{md5}\n' for name, length, md5 in rows)).encode('ascii')
    if len(result) > dictionary_max_bytes:
        raise ValueError('Derived dictionary exceeds its reviewed retained-byte bound')
    return result


def acquire(document, release, path, *, execute=False, stage_only=False, derive_only=False, recover_existing=False, client=None,
            source_client_factory=None):
    prepared = refs.acquisition_plan(document)
    if stage_only and derive_only:
        raise ValueError('Choose either server-side staging or AWS-host derivation')
    if not execute:
        return {**prepared, 'state': 'planned', 'aws_mutations': False,
                'scope': 'Exact reviewed source copies plus AWS-derived T2T raw dictionary; no aligner indices or annotation caches'}
    if os.environ.get('NGS_BUCKET') != release.get('bucket') or not release.get('kms'):
        raise ValueError('NGS_BUCKET must match the reviewed deployment cache before execution')
    # Operator-side staging issues S3 CopyObject and metadata HEAD requests only.
    # Every body GET and derived-object write requires an EC2 instance identity.
    runtime = {'execution': 'operator-server-side-copy-only'} if stage_only and not recover_existing else verify_cloud_runtime(release)
    injected_client = client is not None
    client = client or aws_session().client('s3', config=Config(connect_timeout=5, read_timeout=900, retries={'total_max_attempts': 1}))
    source_clients = {}

    def public_client(current_only):
        if not current_only:
            return client
        # Avoid botocore's us-east-1/global bucket-region discovery path for the
        # reviewed us-west-2 provider. Long copy timeouts apply only to the
        # destination CopyObject request, never public source HEAD operations.
        region = 'us-west-2'
        if region not in source_clients:
            config = Config(use_dualstack_endpoint=True, connect_timeout=3, read_timeout=5, retries={'total_max_attempts': 1})
            if source_client_factory:
                source_clients[region] = source_client_factory(region, config)
            elif injected_client:
                # Tests and operator-controlled API callers may supply one
                # complete fake/client; do not create an unexpected network client.
                source_clients[region] = client
            else:
                source_clients[region] = aws_session().client('s3', region_name=region, config=config)
        return source_clients[region]
    path = Path(path)
    result = json.loads(path.read_text()) if path.exists() else {'schema': 2, 'bundles': prepared['bundles'], 'objects': [], 'completed': False}
    if result.get('bundles', [refs.LEGACY_BUNDLE]) != prepared['bundles']:
        raise ValueError('An acquisition checkpoint cannot be reused for different reviewed bundles')
    expected = refs._bundle_objects(prepared['bundles'])
    if any((o.get('source_bucket'), o.get('source_key')) not in expected for o in result.get('objects', [])):
        raise ValueError('Checkpoint contains an unreviewed source')

    def save():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, indent=2))

    for item in prepared['objects']:
        prior = next((o for o in result['objects'] if (o['source_bucket'], o['source_key']) == (item['bucket'], item['key'])), None)
        if prior:
            refs._public_identity(prior, acquired=True)
            if prior.get('bucket') != release['bucket'] or prior.get('key') != 'operations/checkpoints/references/'+item['bucket']+'/'+item['key']:
                raise ValueError('Checkpoint source is outside the reviewed deployment cache')
            _verify_cached(client.head_object(Bucket=release['bucket'], Key=prior['key'], VersionId=prior['version_id'], ChecksumMode='ENABLED'), prior)
            continue
        if derive_only:
            raise ValueError('Derivation requires every exact source already staged in the reviewed cache')
        source = {'Bucket': item['bucket'], 'Key': item['key']}
        current_only = (item['bucket'], item['key']) in refs.CURRENT_ONLY_SOURCES
        if item['version'] is not None and not current_only:
            source['VersionId'] = item['version']
        source_client = public_client(current_only)
        head = source_client.head_object(**source)
        if (head.get('ContentLength'), head.get('ETag'), head.get('VersionId')) != (item['bytes'], item['etag'], item['version']):
            raise ValueError('Public source identity changed; no alternate version is acquired')
        key = 'operations/checkpoints/references/'+item['bucket']+'/'+item['key']
        # Recover an exact copy whose response/checkpoint write was interrupted.
        # A conflicting cache object is preserved for review, never overwritten.
        head = _head_if_present(client, release['bucket'], key)
        copied_source_version = None
        equivalence = None
        if head is not None:
            if current_only:
                # Expected-version metadata was supplied before CopyObject and
                # cannot prove the response's actual CopySourceVersionId. An
                # interrupted/unverified copy stays unready for operator review.
                if not recover_existing:
                    raise ValueError('Existing conditional cache copy lacks a checkpointed source-version response; retained unready')
                equivalence = recover_current_copy(source_client, head, item, release, runtime)
            metadata = head.get('Metadata', {})
            if (metadata.get('source-etag') != item['etag'].strip('"') or metadata.get('source-bucket') != item['bucket']
                    or (item['version'] is not None and metadata.get('source-version') != item['version'])):
                raise ValueError('Existing cache object has a different reviewed public source identity')
            version = head.get('VersionId')
        else:
            copied = client.copy_object(Bucket=release['bucket'], Key=key, CopySource=source,
                                        CopySourceIfMatch=item['etag'], ServerSideEncryption='aws:kms', SSEKMSKeyId=release['kms'],
                                        ChecksumAlgorithm='SHA256', MetadataDirective='REPLACE',
                                        Metadata={'source-etag': item['etag'].strip('"'), 'source-bucket': item['bucket'],
                                                  'source-version': item['version'] or 'unversioned'})
            version = copied.get('VersionId')
            if not version or version == 'null':
                raise ValueError('Acquisition destination did not retain an immutable version')
            if current_only:
                copied_source_version = copied.get('CopySourceVersionId')
                if copied_source_version != item['version']:
                    raise ValueError('Conditional copy source-version response differs or is absent; copied object retained unready')
                after = source_client.head_object(**source)
                if (after.get('ContentLength'), after.get('ETag'), after.get('VersionId')) != (item['bytes'], item['etag'], item['version']):
                    raise ValueError('Current public source identity changed after conditional copy; copied object retained unready')
            head = client.head_object(Bucket=release['bucket'], Key=key, VersionId=version, ChecksumMode='ENABLED')
        obj = {'source_bucket': item['bucket'], 'source_key': item['key'], 'source_etag': item['etag'], 'source_version': item['version'],
               'bucket': release['bucket'], 'key': key, 'version_id': version, 'bytes': item['bytes'],
               'checksum_sha256': head.get('ChecksumSHA256'), 'checksum_type': head.get('ChecksumType', 'FULL_OBJECT'), 'acquired': time.time()}
        if current_only:
            if copied_source_version:
                obj['copy_source_version_id'] = copied_source_version
            if equivalence:
                obj['source_byte_equivalence'] = equivalence
        refs._public_identity(obj, acquired=True)
        _verify_cached(head, obj)
        result['objects'].append(obj)
        save()
        print(json.dumps({'acquired': item['key'], 'bytes': item['bytes'], 'version_pinned': True}), flush=True)
    if stage_only:
        result.update(schema=2, bundles=prepared['bundles'], completed=False,
                      total_bytes=sum(o['bytes'] for o in result['objects']), runtime=runtime,
                      scope='Server-side source copies staged; complete cloud derivation and companion verification before publication')
        save()
        return {'state': 'staged', 'completed': False, 'bytes': result['total_bytes'],
                'requires_aws_host_derivation': refs.T2T_DICTIONARY in expected,
                'maximum_double_retained_bytes': prepared['maximum_double_retained_bytes']}
    if refs.T2T_DICTIONARY in expected:
        parents = [next(o for o in result['objects'] if (o['source_bucket'], o['source_key']) == ident)
                   for ident in refs.BUNDLES['t2t-chm13-raw-v2.0'] if ident != refs.T2T_DICTIONARY]
        provenance = {'algorithm': refs.DICTIONARY_ALGORITHM, 'fai_validated': True,
                      'parents': {o['source_key']: {'version_id': o['version_id'], 'checksum_sha256': o['checksum_sha256']} for o in parents}}
        prior = next((o for o in result['objects'] if (o['source_bucket'], o['source_key']) == refs.T2T_DICTIONARY), None)
        if prior:
            if prior.get('derivation') != provenance or prior.get('bucket') != release['bucket']:
                raise ValueError('Derived dictionary checkpoint parent identity differs')
            _verify_cached(client.head_object(Bucket=prior['bucket'], Key=prior['key'], VersionId=prior['version_id'], ChecksumMode='ENABLED'), prior)
        else:
            fasta, fai = parents
            index = _read_small(client, fai, 1024*1024)
            response = client.get_object(Bucket=fasta['bucket'], Key=fasta['key'], VersionId=fasta['version_id'])
            try:
                dictionary = derive_dictionary(response['Body'].iter_chunks(CHUNK_BYTES), index, fasta['bytes'], fasta['checksum_sha256'])
            finally:
                response['Body'].close()
            checksum = base64.b64encode(hashlib.sha256(dictionary).digest()).decode()
            bucket, source_key = refs.T2T_DICTIONARY
            key = 'operations/checkpoints/references/'+bucket+'/'+source_key
            head = _head_if_present(client, release['bucket'], key)
            if head is not None:
                if (head.get('ChecksumSHA256') != checksum or head.get('ContentLength') != len(dictionary)
                        or head.get('Metadata', {}).get('source-sha256') != _checksum(fasta['checksum_sha256']).hex()):
                    raise ValueError('Existing derived dictionary differs from the exact recomputed sequence identity')
                version = head.get('VersionId')
            else:
                copied = client.put_object(Bucket=release['bucket'], Key=key, Body=dictionary, ServerSideEncryption='aws:kms',
                                           SSEKMSKeyId=release['kms'], ChecksumAlgorithm='SHA256', ChecksumSHA256=checksum,
                                           Metadata={'derivation': refs.DICTIONARY_ALGORITHM, 'source-sha256': _checksum(fasta['checksum_sha256']).hex()})
                version = copied.get('VersionId')
            if not version or version == 'null':
                raise ValueError('Derived dictionary did not retain an immutable destination version')
            obj = {'source_bucket': bucket, 'source_key': source_key, 'bucket': release['bucket'], 'key': key,
                   'version_id': version, 'bytes': len(dictionary), 'checksum_sha256': checksum,
                   'checksum_type': 'FULL_OBJECT', 'derivation': provenance, 'acquired': time.time()}
            _verify_cached(client.head_object(Bucket=release['bucket'], Key=key, VersionId=obj['version_id'], ChecksumMode='ENABLED'), obj)
            result['objects'].append(obj)
            save()
    total = sum(o['bytes'] for o in result['objects'])
    result.update(schema=2, bundles=prepared['bundles'], completed=True, total_bytes=total,
                  monthly_s3_usd=total/1024**3*.023, double_retained_monthly_s3_usd=2*total/1024**3*.023,
                  runtime=runtime, scope='Exact AWS reference components; matching aligner indices and assay validation remain separate')
    # Validate complete companions, provenance and total bounds before completed evidence.
    refs.plan('acquisition-validation', result)
    save()
    receipt_hash = hashlib.sha256(json.dumps(prepared['bundles'], sort_keys=True).encode()).hexdigest()[:16]
    client.put_object(Bucket=release['bucket'], Key='operations/checkpoints/references/acquisition-'+receipt_hash+'.json',
                      Body=json.dumps(result).encode(), ServerSideEncryption='aws:kms', SSEKMSKeyId=release['kms'])
    return {'completed': True, 'bytes': total, 'double_retained_bytes': 2*total, 'double_retained_monthly_s3_usd': 2*total/1024**3*.023}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', default='evidence/deployment/reference-plan.json')
    parser.add_argument('--acquisition', help='Small resumable receipt; defaults to a unique name per bundle set')
    parser.add_argument('--release', default='evidence/deployment/release.json')
    parser.add_argument('--bundle', action='append', help='Plan exact reviewed bundle IDs instead of reading --plan')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--recover-existing', action='store_true', help='AWS host only: stream the exact pinned public version to prove an interrupted copy without replacing it')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--stage-only', action='store_true', help='Operator S3 server-side copies and metadata HEAD only; no body downloads')
    mode.add_argument('--derive-only', action='store_true', help='AWS EC2 host: require cached exact inputs and derive missing reviewed companions')
    args = parser.parse_args()
    document = refs.reviewed_manifest(args.bundle) if args.bundle else json.loads(Path(args.plan).read_text())
    if args.acquisition:
        path = args.acquisition
    elif isinstance(document, list):
        path = 'evidence/deployment/reference-acquisition.json'
    else:
        suffix = hashlib.sha256(json.dumps(document.get('bundles'), sort_keys=True).encode()).hexdigest()[:16]
        path = 'evidence/deployment/reference-acquisition-'+suffix+'.json'
    result = acquire(document, json.loads(Path(args.release).read_text()), path, execute=args.execute,
                     stage_only=args.stage_only, derive_only=args.derive_only, recover_existing=args.recover_existing)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
