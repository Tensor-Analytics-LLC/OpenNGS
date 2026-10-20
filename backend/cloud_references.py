"""Publish already acquired exact public references into a funded tenant cache.

Operator-only function: no HTTP endpoint accepts arbitrary buckets or manifests.
Copying and checksum verification happen entirely inside AWS. Dataset rows are
not Ready until every primary/companion object has its immutable verified copy.
"""
import base64
import hashlib
import json
import os
import re
import time

from botocore.config import Config
from botocore.exceptions import ClientError
from . import budget, data, db, entitlements
from .config import aws_session

_BROAD = 'hg38/v0/'
_GIAB = 'release/AshkenazimTrio/HG002_NA24385_son/NISTv4.2.1/GRCh38/'
_VCF = 'HG002_GRCh38_1_22_v4.2.1_benchmark.vcf.gz'
# This is an operator-reviewed allowlist, not a remote manifest interpreter.
# Keep the legacy six-object set and its dataset identity derivation unchanged.
REVIEWED = {
    ('broad-references', _BROAD+'Homo_sapiens_assembly38.fasta'): ('reference', 'FASTA', 'hg38/v0', 'Broad assembly38 reference'),
    ('broad-references', _BROAD+'Homo_sapiens_assembly38.fasta.fai'): ('reference', 'INDEX', 'hg38/v0', 'Broad assembly38 FASTA index'),
    ('broad-references', _BROAD+'Homo_sapiens_assembly38.dict'): ('reference', 'INDEX', 'hg38/v0', 'Broad assembly38 sequence dictionary'),
    ('giab', _GIAB+_VCF): ('giab', 'INDEXED_VCF', '4.2.1', 'GIAB HG002 autosomal truth variants'),
    ('giab', _GIAB+_VCF+'.tbi'): ('giab', 'INDEX', '4.2.1', 'GIAB HG002 truth VCF index'),
    ('giab', _GIAB+'HG002_GRCh38_1_22_v4.2.1_benchmark_noinconsistent.bed'): ('giab', 'BED', '4.2.1', 'GIAB HG002 confident benchmark regions'),
}
LEGACY_BUNDLE = 'grch38-broad-giab-v1'
MAX_ACQUISITION_BYTES = 6*1024**3
MAX_RETAINED_BYTES = 12*1024**3
DICTIONARY_MAX_BYTES = 65536
DICTIONARY_ALGORITHM = 'sam-sequence-dictionary-uppercase-md5-v1'
_HG19 = 'hg19/v0/Homo_sapiens_assembly19'
_T2T = 'T2T/CHM13/assemblies/chm13v2.0.fa'
T2T_DICTIONARY = ('openngs-derived', 't2t-chm13/v2.0/raw/chm13v2.0.dict')
# This provider permits public current-object reads, but does not grant public
# GetObjectVersion. Only these reviewed sources use conditional current copies;
# S3 must prove the actual copied version and current HEAD must remain pinned.
CURRENT_ONLY_SOURCES = frozenset([('human-pangenomics', _T2T), ('human-pangenomics', _T2T+'.fai')])
_IDENTITIES = {
    ('broad-references', _BROAD+'Homo_sapiens_assembly38.fasta'): (3249912778, '"e307d81c605fb91b7720a08f00276842-388"', None),
    ('broad-references', _BROAD+'Homo_sapiens_assembly38.fasta.fai'): (160928, '"f76371b113734a56cde236bc0372de0a"', None),
    ('broad-references', _BROAD+'Homo_sapiens_assembly38.dict'): (581712, '"3884c62eb0e53fa92459ed9bff133ae6"', None),
    ('giab', _GIAB+_VCF): (156252944, '"73f1d5be8739131a500a9eaf8396b5cb-10"', None),
    ('giab', _GIAB+_VCF+'.tbi'): (1657747, '"121e2975fb3ff0317ae6a684d0ce6f2f"', None),
    ('giab', _GIAB+'HG002_GRCh38_1_22_v4.2.1_benchmark_noinconsistent.bed'): (11494021, '"97265e922a97c69a0391cf3f92a89b8b"', None),
    ('broad-references', _HG19+'.fasta'): (3140756381, '"be7cb527b5c2914c35cb1bf513d63a61-375"', None),
    ('broad-references', _HG19+'.fasta.fai'): (2780, '"fdc0ab679f6461d78980de2a2e97e8f3"', None),
    ('broad-references', _HG19+'.dict'): (14811, '"cd3b1dde0427afd8a115b2737a96713e"', None),
    ('human-pangenomics', _T2T): (3156259703, '"2cd18f7b70854f8172c833528e88d63d-377"', 'ZvzJTIpSZLGJrk.ENPNu0XQIZyQ6ctZ3'),
    ('human-pangenomics', _T2T+'.fai'): (788, '"724744bb9abcdd484ab4a525f54be8d3"', 'ZA8yvRJbyRnv8PO1r8KLvZSlZlLnrmZv'),
}
REVIEWED_OBJECTS = {
    ident: {'family': value[0], 'format': value[1], 'release': value[2], 'label': value[3],
            'assembly': 'GRCh38', 'source_kind': 'public'}
    for ident, value in REVIEWED.items()
}
for suffix, fmt, label in [('.fasta', 'FASTA', 'Broad assembly19 reference'),
                           ('.fasta.fai', 'INDEX', 'Broad assembly19 FASTA index'),
                           ('.dict', 'INDEX', 'Broad assembly19 sequence dictionary')]:
    REVIEWED_OBJECTS[('broad-references', _HG19+suffix)] = {
        'family': 'reference', 'format': fmt, 'release': 'hg19/v0', 'label': label,
        'assembly': 'GRCh37', 'source_kind': 'public'}
for key, fmt, label in [(_T2T, 'FASTA', 'T2T-CHM13 v2.0 raw reference (original chrY and chrM)'),
                        (_T2T+'.fai', 'INDEX', 'T2T-CHM13 v2.0 raw FASTA index')]:
    REVIEWED_OBJECTS[('human-pangenomics', key)] = {
        'family': 'reference', 'format': fmt, 'release': '2.0-raw', 'label': label,
        'assembly': 'T2T-CHM13', 'source_kind': 'public',
        'reference_variant': 'raw; not the recommended short-read maskedY_rCRS analysis set'}
REVIEWED_OBJECTS[T2T_DICTIONARY] = {
    'family': 'reference', 'format': 'INDEX', 'release': '2.0-raw', 'assembly': 'T2T-CHM13',
    'label': 'T2T-CHM13 v2.0 raw sequence dictionary derived in AWS', 'source_kind': 'derived'}
BUNDLES = {
    LEGACY_BUNDLE: tuple(REVIEWED),
    'grch37-broad-v0': tuple(('broad-references', _HG19+s) for s in ['.fasta', '.fasta.fai', '.dict']),
    't2t-chm13-raw-v2.0': (('human-pangenomics', _T2T), ('human-pangenomics', _T2T+'.fai'), T2T_DICTIONARY),
}
COMPANIONS = {
    ('broad-references', _BROAD+'Homo_sapiens_assembly38.fasta'): {
        'index': ('broad-references', _BROAD+'Homo_sapiens_assembly38.fasta.fai'),
        'dictionary': ('broad-references', _BROAD+'Homo_sapiens_assembly38.dict')},
    ('giab', _GIAB+_VCF): {'index': ('giab', _GIAB+_VCF+'.tbi')},
    ('broad-references', _HG19+'.fasta'): {
        'index': ('broad-references', _HG19+'.fasta.fai'), 'dictionary': ('broad-references', _HG19+'.dict')},
    ('human-pangenomics', _T2T): {'index': ('human-pangenomics', _T2T+'.fai'), 'dictionary': T2T_DICTIONARY},
}


def _bundle_objects(bundles):
    if (not isinstance(bundles, list) or not bundles or any(not isinstance(b, str) for b in bundles)
            or len(bundles) != len(set(bundles)) or any(b not in BUNDLES for b in bundles)):
        raise ValueError('Select explicit operator-reviewed reference bundles')
    return {ident for bundle in bundles for ident in BUNDLES[bundle]}


def reviewed_manifest(bundles):
    """Return exact public inputs; derived members are bounded separately."""
    identities = _bundle_objects(bundles)
    return {'schema': 2, 'bundles': bundles, 'objects': [
        {'bucket': bucket, 'key': key, 'bytes': _IDENTITIES[(bucket, key)][0],
         'etag': _IDENTITIES[(bucket, key)][1], 'version': _IDENTITIES[(bucket, key)][2]}
        for bucket, key in sorted(identities) if REVIEWED_OBJECTS[(bucket, key)]['source_kind'] == 'public']}


def _public_identity(item, *, acquired=False):
    bucket, key = (item.get('source_bucket'), item.get('source_key')) if acquired else (item.get('bucket'), item.get('key'))
    expected = _IDENTITIES.get((bucket, key))
    if expected is None:
        raise ValueError('Source is outside the exact reviewed public-object allowlist')
    fields = ('bytes', 'source_etag', 'source_version') if acquired else ('bytes', 'etag', 'version')
    if tuple(item.get(f) for f in fields) != expected:
        raise ValueError('Reviewed source bytes, ETag or version differ; substitution is forbidden')
    if acquired and (bucket, key) in CURRENT_ONLY_SOURCES and item.get('copy_source_version_id') != expected[2]:
        proof = item.get('source_byte_equivalence', {})
        head = {'bytes': expected[0], 'etag': expected[1], 'version_id': expected[2]}
        try:
            checksum = base64.b64decode(item.get('checksum_sha256', ''), validate=True).hex()
        except (ValueError, TypeError):
            checksum = ''
        if (proof.get('method') != 'aws-current-source-byte-equivalence-v1'
                or proof.get('source_before') != head or proof.get('source_after') != head
                or proof.get('source_get_version_id') != expected[2]
                or proof.get('destination_version_id') != item.get('version_id')
                or not item.get('version_id') or item.get('checksum_type') != 'FULL_OBJECT'
                or not re.fullmatch('[a-f0-9]{64}', checksum)
                or proof.get('source_sha256') != checksum or proof.get('bytes') != expected[0]
                or proof.get('runtime', {}).get('region') != 'us-east-1'
                or not re.fullmatch('i-[a-f0-9]{8,17}', proof.get('runtime', {}).get('instance_id', ''))):
            raise ValueError('Conditional public copy lacks proof of its exact reviewed source version')


def acquisition_plan(document):
    """Validate before any AWS request; old exact six-object plans remain valid."""
    if isinstance(document, list):
        bundles, objects = [LEGACY_BUNDLE], document
    elif isinstance(document, dict) and document.get('schema') == 2:
        bundles, objects = document.get('bundles'), document.get('objects')
    else:
        raise ValueError('Use the legacy exact plan or a schema-2 reviewed bundle manifest')
    identities = _bundle_objects(bundles)
    expected = {i for i in identities if REVIEWED_OBJECTS[i]['source_kind'] == 'public'}
    if (not isinstance(objects, list) or any(not isinstance(o, dict) for o in objects)
            or len(objects) != len(expected) or {(o.get('bucket'), o.get('key')) for o in objects} != expected):
        raise ValueError('The acquisition must contain every exact public member of its reviewed bundles')
    for obj in objects:
        _public_identity(obj)
    source_bytes = sum(o['bytes'] for o in objects)
    maximum_bytes = source_bytes + sum(DICTIONARY_MAX_BYTES for i in identities if REVIEWED_OBJECTS[i]['source_kind'] == 'derived')
    if maximum_bytes > MAX_ACQUISITION_BYTES or 2*maximum_bytes > MAX_RETAINED_BYTES:
        raise ValueError('Reviewed acquisition exceeds the 6 GiB source / 12 GiB double-retained bound')
    return {'schema': 2, 'bundles': bundles, 'objects': objects, 'source_bytes': source_bytes,
            'maximum_bytes': maximum_bytes, 'maximum_double_retained_bytes': 2*maximum_bytes,
            'maximum_double_retained_monthly_s3_usd_estimate': 2*maximum_bytes/1024**3*.023,
            'request_reservation_usd': .15}


def plan(uid, acquisition):
    if acquisition.get('completed') is not True:
        raise ValueError('Only a completed exact reference-acquisition record can be published')
    source_bucket = os.environ.get('NGS_BUCKET')
    if not source_bucket:
        raise ValueError('The platform source cache is not configured')
    objects = acquisition.get('objects', [])
    bundles = acquisition.get('bundles', [LEGACY_BUNDLE])
    expected = _bundle_objects(bundles)
    if (not isinstance(objects, list) or any(not isinstance(o, dict) for o in objects)
            or len(objects) != len(expected) or {(o.get('source_bucket'), o.get('source_key')) for o in objects} != expected):
        raise ValueError('The reviewed acquisition must contain every exact bundle member, including FASTA index and dictionary')
    result = []
    for obj in objects:
        identity = (obj['source_bucket'], obj['source_key'])
        reviewed = REVIEWED_OBJECTS[identity]
        if reviewed['source_kind'] == 'public':
            _public_identity(obj, acquired=True)
        expected_key = 'operations/checkpoints/references/'+obj['source_bucket']+'/'+obj['source_key']
        if obj.get('bucket') != source_bucket or obj.get('key') != expected_key:
            raise ValueError('Source is outside the reviewed platform acquisition prefix')
        if not obj.get('version_id') or obj['version_id'] == 'null' or obj.get('checksum_type') != 'FULL_OBJECT':
            raise ValueError('Every source requires an immutable version and full-object SHA-256')
        try:
            raw = base64.b64decode(obj['checksum_sha256'], validate=True)
        except (KeyError, ValueError) as error:
            raise ValueError('Invalid source checksum') from error
        if len(raw) != 32 or type(obj.get('bytes')) is not int or obj['bytes'] <= 0:
            raise ValueError('Invalid source length or SHA-256')
        if reviewed['source_kind'] == 'derived':
            if obj['bytes'] > DICTIONARY_MAX_BYTES or obj.get('derivation', {}).get('algorithm') != DICTIONARY_ALGORITHM:
                raise ValueError('Derived dictionary lacks its reviewed bounded generation provenance')
            parents = obj['derivation'].get('parents')
            actual = {o['source_key']: {'version_id': o['version_id'], 'checksum_sha256': o['checksum_sha256']}
                      for o in objects if (o['source_bucket'], o['source_key']) in [('human-pangenomics', _T2T), ('human-pangenomics', _T2T+'.fai')]}
            if parents != actual or len(actual) != 2 or obj['derivation'].get('fai_validated') is not True:
                raise ValueError('Derived dictionary parent identities or FAI validation differ')
        digest = raw.hex()
        ident = 'ds_reference_'+hashlib.sha256((uid+':'+obj['source_bucket']+':'+obj['source_key']+':'+digest).encode()).hexdigest()[:40]
        name = obj['source_key'].rsplit('/', 1)[1]
        result.append({**obj, **reviewed, 'dataset_id': ident, 'sha256': digest, 'name': name,
                       'destination_key': f'tenants/{uid}/datasets/{ident}/{name}'})
    total = sum(o['bytes'] for o in result)
    if total != acquisition.get('total_bytes') or total > MAX_ACQUISITION_BYTES or 2*total > MAX_RETAINED_BYTES:
        raise ValueError('Reviewed publication requires matching bytes within the 6 GiB source / 12 GiB double-retained bound')
    return {'bundles': bundles, 'objects': result, 'bytes': total, 'additional_s3_monthly_usd_estimate': total/1024**3*.023,
            'maximum_double_retained_bytes': 2*total, 'double_retained_monthly_s3_usd_estimate': 2*total/1024**3*.023,
            'request_reservation_usd': .15, 'scope': 'Verified FASTA/FAI/dictionary or benchmark components; aligner indices, annotation caches and assay validation remain separate'}


def _verify_head(head, obj, *, destination=False):
    if head.get('ContentLength') != obj['bytes'] or head.get('ChecksumSHA256') != obj['checksum_sha256']:
        raise ValueError('Cloud reference length or full-object checksum differs from the acquired version')
    if head.get('ChecksumType', 'FULL_OBJECT') != 'FULL_OBJECT' or not head.get('VersionId') or head['VersionId'] == 'null':
        raise ValueError('Cloud reference must retain full-object checksum and immutable version identity')
    if not destination and head['VersionId'] != obj['version_id']:
        raise ValueError('Source version changed')
    if destination and head.get('Metadata', {}).get('sha256') != obj['sha256']:
        raise ValueError('Tenant reference is missing its admitted SHA-256 metadata')


def publish(actor, uid, acquisition, *, execute=False):
    if actor.get('role') != 'admin' or actor.get('frozen'):
        raise ValueError('An active administrator must authorize reference publication')
    user = db.query('SELECT id,frozen FROM users WHERE id=?', (uid,), one=True)
    if not user or user['frozen'] or not entitlements.research(uid):
        raise ValueError('The destination must be an active funded Research Pro workspace')
    prepared = plan(uid, acquisition)
    settings = data.settings(uid)
    if settings.get('research_profile', {}).get('state') != 'Ready' or not settings.get('s3_bucket') or not settings.get('kms_key_arn'):
        raise ValueError('The destination tenant cloud profile is not Ready')
    prepared['destination_bucket'] = settings['s3_bucket']
    additional = 0
    for obj in prepared['objects']:
        old = db.query('SELECT * FROM datasets WHERE id=?', (obj['dataset_id'],), one=True)
        if old and (old['user_id'] != uid or old['sha256'] != obj['sha256'] or old['bytes'] != obj['bytes'] or old['s3_key'] != obj['destination_key']):
            raise ValueError('Existing reference dataset identity differs; nothing is replaced')
        additional += 0 if old else obj['bytes']
    prepared['additional_dataset_bytes'] = additional
    if not execute:
        return {**prepared, 'state': 'planned', 'aws_mutations': False}
    operation = 'reference-components-'+hashlib.sha256(db.encode([uid, [(o['dataset_id'], o['version_id']) for o in prepared['objects']]]).encode()).hexdigest()[:40]
    lease_key = 'reference-publication:'+uid
    with db.tx() as c:
        now = time.time()
        lease = c.execute('SELECT value FROM controls WHERE key=?', (lease_key,)).fetchone()
        if lease and float(lease['value']) > now:
            raise ValueError('A reference publication is already in progress')
        exposure = entitlements.storage_exposure(c, uid)
        holds = int(c.execute("SELECT COALESCE(SUM(bytes),0) FROM output_holds WHERE user_id=? AND state='reserved'", (uid,)).fetchone()[0])
        if exposure+additional+holds > entitlements.limits(uid)['max_storage_bytes']:
            raise budget.BudgetFull('The funded tenant cannot retain the complete component set')
        budget.reserve(c, uid, operation, 'storage', prepared['request_reservation_usd'],
                       {'bytes': additional, 'kind': 'exact AWS-side reference copies',
                        'storage': 'Retained bytes count against the separately funded Pro storage capacity; no double-counted storage charge'})
        c.execute('INSERT INTO controls(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (lease_key, str(now+7200)))
        for obj in prepared['objects']:
            c.execute("INSERT INTO datasets VALUES(?,?,?,?,?,?,'Provisioning',?,NULL,?,?,?) ON CONFLICT(id) DO NOTHING",
                      (obj['dataset_id'], uid, None, obj['name'], obj['bytes'], obj['sha256'], obj['destination_key'],
                       db.encode({'format': obj['format'], 'reference_publication': operation}), now, now))
    # A long server-side CopyObject can legitimately exceed the API client's
    # ordinary timeout. Do not blindly retry an ambiguous mutation.
    client = aws_session().client('s3', config=Config(connect_timeout=5, read_timeout=900, retries={'total_max_attempts': 1}))
    copied = []
    try:
        for obj in prepared['objects']:
            source = client.head_object(Bucket=obj['bucket'], Key=obj['key'], VersionId=obj['version_id'], ChecksumMode='ENABLED')
            _verify_head(source, obj)
            destination = {'Bucket': settings['s3_bucket'], 'Key': obj['destination_key']}
            try:
                head = client.head_object(**destination, ChecksumMode='ENABLED')
            except ClientError as error:
                if error.response['Error']['Code'] not in ['404', 'NoSuchKey', 'NotFound']:
                    raise
                response = client.copy_object(
                    **destination, CopySource={'Bucket': obj['bucket'], 'Key': obj['key'], 'VersionId': obj['version_id']},
                    CopySourceIfMatch=source['ETag'], ServerSideEncryption='aws:kms', SSEKMSKeyId=settings['kms_key_arn'],
                    BucketKeyEnabled=True, ChecksumAlgorithm='SHA256', MetadataDirective='REPLACE',
                    Metadata={'sha256': obj['sha256'], 'tenant': uid, 'dataset': obj['dataset_id'], 'source-release': obj['release']})
                head = client.head_object(**destination, VersionId=response['VersionId'], ChecksumMode='ENABLED')
            _verify_head(head, obj, destination=True)
            copied.append({**obj, 'tenant_version_id': head['VersionId'],
                           'uri': 's3://'+settings['s3_bucket']+'/'+obj['destination_key']})
        by_identity = {(o['source_bucket'], o['source_key']): o for o in copied}
        with db.tx() as c:
            for obj in copied:
                metadata = {
                    'dataset_id': obj['dataset_id'], 'sha256': obj['sha256'], 'version_id': obj['tenant_version_id'],
                    'uploaded': True, 'assembly': obj['assembly'], 'release': obj['release'],
                    'source_identity': {'provider': obj['source_bucket'], 'key': obj['source_key'],
                                        'release': obj['release'], 'assembly': obj['assembly'],
                                        'source_kind': obj['source_kind'],
                                        'source_version': obj.get('source_version'), 'source_etag': obj.get('source_etag'),
                                        'copy_source_version_id': obj.get('copy_source_version_id'),
                                        'source_byte_equivalence': obj.get('source_byte_equivalence'),
                                        'acquired_version': obj['version_id'], 'sha256': obj['sha256']},
                    'label': obj['label'], 'verification_method': 's3-full-object-sha256',
                    'component_scope': prepared['scope'], 'secondary_objects': [],
                }
                companions = [(field, by_identity[identity]) for field, identity in
                              COMPANIONS.get((obj['source_bucket'], obj['source_key']), {}).items()]
                if obj['format'] == 'FASTA':
                    if {field for field, _ in companions} != {'index', 'dictionary'}:
                        raise ValueError('A Ready FASTA requires its complete exact FAI and sequence dictionary')
                    metadata['reference_digest'] = 'sha256:'+obj['sha256']
                if obj.get('reference_variant'):
                    metadata['reference_variant'] = obj['reference_variant']
                if obj.get('derivation'):
                    metadata['derivation'] = obj['derivation']
                for field, companion in companions:
                    metadata[field] = companion['uri']
                    metadata[field+'_sha256'] = companion['sha256']
                    metadata[field+'_version_id'] = companion['tenant_version_id']
                    metadata['secondary_objects'].append({'uri': companion['uri'], 'bytes': companion['bytes'],
                                                          'sha256': companion['sha256'], 'version_id': companion['tenant_version_id']})
                existing = c.execute('SELECT * FROM assets WHERE id=?', (obj['dataset_id'],)).fetchone()
                if existing and (existing['user_id'] != uid or existing['path'] != obj['uri']):
                    raise ValueError('An existing asset conflicts with this exact tenant reference')
                if existing:
                    prior = json.loads(existing['metadata'])
                    if prior.get('sha256') != obj['sha256'] or prior.get('version_id') != obj['tenant_version_id']:
                        raise ValueError('An existing asset has a different immutable reference identity')
                    metadata = {**prior, **metadata}
                c.execute("INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET metadata=excluded.metadata",
                          (obj['dataset_id'], uid, obj['uri'], obj['name'], obj['format'], obj['bytes'], time.time(),
                           obj['family'], obj['assembly'], db.encode(metadata)))
                c.execute("UPDATE datasets SET state='Ready',version_id=?,manifest=?,updated=? WHERE id=? AND user_id=?",
                          (obj['tenant_version_id'], db.encode({'format': obj['format'], **metadata}), time.time(), obj['dataset_id'], uid))
            db.audit(c, actor['id'], 'references.components.published', uid,
                     {'datasets': [o['dataset_id'] for o in copied], 'bytes': prepared['bytes'],
                      'verification': 'FULL_OBJECT SHA-256 and immutable versions', 'source_deleted': False})
        budget.reconcile(operation, uid, 'storage')
        return {'state': 'Ready', 'bytes': prepared['bytes'], 'additional_dataset_bytes': additional,
                'objects': [{k: o[k] for k in ['dataset_id', 'name', 'format', 'assembly', 'release', 'bytes', 'sha256', 'tenant_version_id']} for o in copied],
                'scope': prepared['scope']}
    finally:
        # Failed work remains Provisioning with its storage and cost hold. A
        # retry verifies any completed copies before issuing another mutation.
        with db.tx() as c:
            c.execute('DELETE FROM controls WHERE key=?', (lease_key,))
