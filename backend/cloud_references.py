"""Publish already acquired exact public references into a funded tenant cache.

Operator-only function: no HTTP endpoint accepts arbitrary buckets or manifests.
Copying and checksum verification happen entirely inside AWS. Dataset rows are
not Ready until every primary/companion object has its immutable verified copy.
"""
import base64
import hashlib
import json
import os
import time

from botocore.config import Config
from botocore.exceptions import ClientError
from . import budget, data, db, entitlements
from .config import aws_session

_BROAD = 'hg38/v0/'
_GIAB = 'release/AshkenazimTrio/HG002_NA24385_son/NISTv4.2.1/GRCh38/'
_VCF = 'HG002_GRCh38_1_22_v4.2.1_benchmark.vcf.gz'
REVIEWED = {
    ('broad-references', _BROAD+'Homo_sapiens_assembly38.fasta'): ('reference', 'FASTA', 'hg38/v0', 'Broad assembly38 reference'),
    ('broad-references', _BROAD+'Homo_sapiens_assembly38.fasta.fai'): ('reference', 'INDEX', 'hg38/v0', 'Broad assembly38 FASTA index'),
    ('broad-references', _BROAD+'Homo_sapiens_assembly38.dict'): ('reference', 'INDEX', 'hg38/v0', 'Broad assembly38 sequence dictionary'),
    ('giab', _GIAB+_VCF): ('giab', 'INDEXED_VCF', '4.2.1', 'GIAB HG002 autosomal truth variants'),
    ('giab', _GIAB+_VCF+'.tbi'): ('giab', 'INDEX', '4.2.1', 'GIAB HG002 truth VCF index'),
    ('giab', _GIAB+'HG002_GRCh38_1_22_v4.2.1_benchmark_noinconsistent.bed'): ('giab', 'BED', '4.2.1', 'GIAB HG002 confident benchmark regions'),
}


def plan(uid, acquisition):
    if acquisition.get('completed') is not True:
        raise ValueError('Only a completed exact reference-acquisition record can be published')
    source_bucket = os.environ.get('NGS_BUCKET')
    if not source_bucket:
        raise ValueError('The platform source cache is not configured')
    objects = acquisition.get('objects', [])
    if len(objects) != len(REVIEWED) or {(o.get('source_bucket'), o.get('source_key')) for o in objects} != set(REVIEWED):
        raise ValueError('The reviewed acquisition must contain exactly the six Broad/GIAB objects')
    result = []
    for obj in objects:
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
        digest = raw.hex()
        ident = 'ds_reference_'+hashlib.sha256((uid+':'+obj['source_bucket']+':'+obj['source_key']+':'+digest).encode()).hexdigest()[:40]
        family, fmt, release, label = REVIEWED[(obj['source_bucket'], obj['source_key'])]
        name = obj['source_key'].rsplit('/', 1)[1]
        result.append({**obj, 'dataset_id': ident, 'sha256': digest, 'name': name, 'label': label,
                       'family': family, 'format': fmt, 'release': release,
                       'destination_key': f'tenants/{uid}/datasets/{ident}/{name}'})
    total = sum(o['bytes'] for o in result)
    if total != acquisition.get('total_bytes') or total > 4*1024**3:
        raise ValueError('The reviewed publication permits at most 4 GiB and requires matching aggregate bytes')
    return {'objects': result, 'bytes': total, 'additional_s3_monthly_usd_estimate': total/1024**3*.023,
            'request_reservation_usd': .15, 'scope': 'Verified component bundles; not full WGS/WES reference/annotation readiness'}


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
        by_name = {o['name']: o for o in copied}
        with db.tx() as c:
            for obj in copied:
                metadata = {
                    'dataset_id': obj['dataset_id'], 'sha256': obj['sha256'], 'version_id': obj['tenant_version_id'],
                    'uploaded': True, 'assembly': 'GRCh38', 'release': obj['release'],
                    'source_identity': {'provider': obj['source_bucket'], 'key': obj['source_key'],
                                        'release': obj['release'], 'assembly': 'GRCh38',
                                        'acquired_version': obj['version_id'], 'sha256': obj['sha256']},
                    'label': obj['label'], 'verification_method': 's3-full-object-sha256',
                    'component_scope': prepared['scope'], 'secondary_objects': [],
                }
                companions = []
                if obj['format'] == 'FASTA':
                    companions = [('index', by_name['Homo_sapiens_assembly38.fasta.fai']),
                                  ('dictionary', by_name['Homo_sapiens_assembly38.dict'])]
                    metadata['reference_digest'] = 'sha256:'+obj['sha256']
                elif obj['format'] == 'INDEXED_VCF':
                    companions = [('index', by_name[_VCF+'.tbi'])]
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
                           obj['family'], 'GRCh38', db.encode(metadata)))
                c.execute("UPDATE datasets SET state='Ready',version_id=?,manifest=?,updated=? WHERE id=? AND user_id=?",
                          (obj['tenant_version_id'], db.encode({'format': obj['format'], **metadata}), time.time(), obj['dataset_id'], uid))
            db.audit(c, actor['id'], 'references.components.published', uid,
                     {'datasets': [o['dataset_id'] for o in copied], 'bytes': prepared['bytes'],
                      'verification': 'FULL_OBJECT SHA-256 and immutable versions', 'source_deleted': False})
        budget.reconcile(operation, uid, 'storage')
        return {'state': 'Ready', 'bytes': prepared['bytes'], 'additional_dataset_bytes': additional,
                'objects': [{k: o[k] for k in ['dataset_id', 'name', 'format', 'release', 'bytes', 'sha256', 'tenant_version_id']} for o in copied],
                'scope': prepared['scope']}
    finally:
        # Failed work remains Provisioning with its storage and cost hold. A
        # retry verifies any completed copies before issuing another mutation.
        with db.tx() as c:
            c.execute('DELETE FROM controls WHERE key=?', (lease_key,))
