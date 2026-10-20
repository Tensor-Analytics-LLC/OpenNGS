"""Image-bound execution evidence for the separately isolated WGS model."""
import json
import re
from . import db

FAMILY = 'deepvariant-cpu-v1'
VERSION = '1.8.0'
UPSTREAM_IMAGE = 'google/deepvariant@sha256:eb223b3c487be43d34cc7b08c906b9c558d195716e10672db1bd6e910dc3a00a'
MODELS = ['WGS']
REQUIREMENTS_SHA256 = '872e8373c992fbec75240f24b913f1f809e06e74359cc741aa288e4fcabb378f'
FIXTURE_MANIFEST_SHA256 = '4deb6e89609337e469427de222613edb2efb1e40c60f000a25da7ba6e05fb41d'
REFERENCE_SHA256 = 'b532f011328adfbc7cf92e37923d6035f251b78ff52e1d570dfa10697cca7fe5'
LEGACY_CASES = {'wgs-truth-control', 'missing-index', 'reference-mismatch', 'multi-sample', 'unsupported-model'}
CASES = LEGACY_CASES | {'missing-gzi'}
ISOLATION = {'network': 'none', 'root_filesystem': 'read-only', 'capabilities': 'all dropped',
             'uid': 1000, 'memory_mb': 4096, 'fresh_mounts': ['/work', '/inputs', '/tmp'], 'tmp_executable': True}
MODEL_FILES = {
    'wgs': {
        'saved_model.pb': (4508419, '52fd5818ef0c6c92a78a9dfafec7022ce2902d1ebd4783aecea0f9f72828c488'),
        'variables/variables.data-00000-of-00001': (87491787, '0352e5a1f329d08c0dba4fbae0fda63ec447fa474843e4f9763f8135a1b06a21')},
    'smallmodels/wgs': {
        'saved_model.pb': (161918, '90d5a24dcfb768cb3a1089c64605e2dd69e3c56626cc5df00d13faf4c83cb5b6'),
        'variables/variables.data-00000-of-00001': (2475415, 'dc65736c61aa9f230816d7b4d6a395d02fb596fd7474fa2a41781d67daac8fad')},
}


def validate_release(value, *, require_isolation=True):
    if value.get('family') != FAMILY or value.get('version') != VERSION or value.get('upstream_image') != UPSTREAM_IMAGE:
        raise ValueError('DeepVariant must match the reviewed immutable upstream CPU runtime')
    if value.get('models') != MODELS or value.get('tools') != ['deepvariant']:
        raise ValueError('Only the executed WGS model may be published')
    if not re.fullmatch(r'[0-9]{12}\.dkr\.ecr\.us-east-1\.amazonaws\.com/openngs-demo@sha256:[a-f0-9]{64}', value.get('image', '')):
        raise ValueError('DeepVariant requires an immutable private worker image')
    for key in ['source_sha256', 'requirements_sha256', 'fixture_manifest_sha256']:
        if not re.fullmatch('[a-f0-9]{64}', value.get(key, '')):
            raise ValueError('DeepVariant source, dependency and fixture provenance are required')
    if value['requirements_sha256'] != REQUIREMENTS_SHA256 or value['fixture_manifest_sha256'] != FIXTURE_MANIFEST_SHA256:
        raise ValueError('DeepVariant dependency and public fixture identities differ from the reviewed contract')
    proof = value.get('qualification', {})
    cases = proof.get('cases', [])
    if proof.get('upstream_image') != UPSTREAM_IMAGE or proof.get('version') != VERSION:
        raise ValueError('Qualification must execute the reviewed upstream version')
    observed_cases = {row.get('id') for row in cases}
    if observed_cases not in [LEGACY_CASES, CASES] or len(cases) != len(observed_cases):
        raise ValueError('DeepVariant requires every positive and rejection control')
    if any(row.get('status') != 'passed' or not row.get('assertions') for row in cases):
        raise ValueError('Every DeepVariant control must pass')
    if any(row['assertions'].get('nonzero_exit') is not True for row in cases if row['id'] != 'wgs-truth-control'):
        raise ValueError('Negative DeepVariant controls must actually reject the incompatible input')
    positive = next(row for row in cases if row['id'] == 'wgs-truth-control')
    bgzf = observed_cases == CASES
    if not bgzf and (any(positive['assertions'].get(key) for key in
                        ['reference_bgzf', 'explicit_reference_gzi', 'reference_lossless'])
                     or positive['assertions'].get('input_staging_symlinks') == 5
                     or proof.get('prepared_reference')):
        raise ValueError('BGZF qualification requires the additional missing-GZI rejection control')
    if bgzf and (positive['assertions'].get('reference_bgzf') is not True
                 or positive['assertions'].get('explicit_reference_gzi') is not True
                 or positive['assertions'].get('reference_lossless') is not True
                 or positive['assertions'].get('input_staging_symlinks') != 5):
        raise ValueError('BGZF qualification requires the lossless reference and its explicit fifth GZI link')
    if bgzf:
        reference = proof.get('prepared_reference', {})
        if (reference.get('source_sha256') != REFERENCE_SHA256
                or reference.get('uncompressed_sha256') != REFERENCE_SHA256
                or any(not re.fullmatch('[a-f0-9]{64}', reference.get(key, '')) for key in ['sha256', 'gzi_sha256'])):
            raise ValueError('BGZF qualification must preserve the pinned original reference and record its GZI digest')
    if not positive.get('outputs') or positive['assertions'].get('truth_allele_matches', 0) < 10:
        raise ValueError('DeepVariant requires actual model output overlapping known truth alleles')
    if positive['assertions'].get('indexed_records') != positive['assertions'].get('variant_records'):
        raise ValueError('DeepVariant requires a verified complete output index')
    names = {row.get('name') for row in positive['outputs']}
    if names != {'calls.vcf.gz', 'calls.vcf.gz.tbi', 'calls.runtime.json'} or any(
            not isinstance(row.get('bytes'), int) or row['bytes'] <= 0 or not re.fullmatch('[a-f0-9]{64}', row.get('sha256', ''))
            for row in positive['outputs']):
        raise ValueError('DeepVariant requires digests and sizes for actual VCF, index and execution metadata')
    if not proof.get('models', {}).get('WGS') or not proof.get('dependency_lock_sha256') == value['requirements_sha256']:
        raise ValueError('Executed models and exact worker dependencies must be recorded')
    models=proof['models']['WGS']
    for name,expected in MODEL_FILES.items():
        files=models.get(name,[]) if isinstance(models,dict) else []
        observed={row.get('path'):row for row in files if isinstance(row,dict)}
        if any(observed.get(path,{}).get('bytes')!=size or observed.get(path,{}).get('sha256')!=sha
               for path,(size,sha) in expected.items()):
            raise ValueError('The executed large and small WGS model weights must match the pinned upstream image')
    if require_isolation:
        isolated=value.get('isolation_qualification',{})
        if (isolated.get('image')!=value['image'] or isolated.get('restrictions')!=ISOLATION
                or not re.fullmatch('[a-f0-9]{64}',isolated.get('source_sha256',''))
                or isolated.get('cases')!=proof['cases']):
            raise ValueError('Published DeepVariant requires exact-image execution with read-only root and fresh isolated mounts')
        if positive['assertions'].get('input_staging_symlinks') != (5 if bgzf else 4):
            raise ValueError('DeepVariant must demonstrate canonical input links without duplicating large alignments')
    return value


def published():
    row = db.query('SELECT value FROM controls WHERE key=?', ('runtime-release:' + FAMILY,), one=True)
    if row:
        try:
            return validate_release(json.loads(row['value']))
        except (ValueError, TypeError, KeyError):
            return None
    return None
