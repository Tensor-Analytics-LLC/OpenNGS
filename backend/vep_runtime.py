"""Immutable Ensembl VEP 115.2 offline runtime and complete cache contract."""
import json
import re

FAMILY = 'ensembl-vep-offline-v1'
VERSION = '115.2'
CACHE_VERSION = 115
ASSEMBLY = 'GRCh38'
UPSTREAM_IMAGE = 'ensemblorg/ensembl-vep@sha256:ed6b660e278458109afc00d5e730aee240a3bf77e817796386033e1a18963152'
FIXTURE_MANIFEST_SHA256 = 'c3746f262ec3548e6ebab67a3711e4c48555d2475e0f0df3b4452a97a614c7f8'
CACHE_MANIFEST_SHA256 = 'e59ba882e3c1ca16cd8de4b82f1f988a0c4ea97639707867fbae2cd170754fa8'
CACHE_INFO_SHA256 = 'aad41cfa88980f51487b1bf55f08060a1bb9086aa7e62252e8b3741c0999474f'
CACHE_ARCHIVE_SHA256 = '5871a5e34527cce76f7ac75399c33e306bf3be8083c63fe7d3459c56338f98de'
CACHE_ARCHIVE_BYTES = 25272957721
CACHE_EXTRACTED_BYTES = 25286767048
CACHE_MEMBER_COUNT = 13922
CACHE_ARCHIVE_NAME = 'homo_sapiens_vep_115_GRCh38.tar.gz'
REQUIREMENTS_SHA256 = 'b51b3d3c92617f8ccd11f41b8356ef8b548f3fc50339f58054e3f2c244d090af'
WORKER_APT_LOCK_SHA256 = '9d2f7364c719caf9dfbf8d32a1946682ef323a9963a7d0e9b15135ef53b44585'
CASES = {'release-115-cache', 'spliced-gff-control', 'wrong-cache-version', 'wrong-assembly', 'missing-cache'}


def cache_binding(asset):
    """Describe only the exact complete original cache archive, never a shard."""
    meta = asset.get('metadata', {})
    digest = asset.get('sha256') or meta.get('sha256')
    if not digest and asset.get('digest', '').startswith('sha256:'):
        digest = asset['digest'][7:]
    if asset.get('name') != CACHE_ARCHIVE_NAME or digest != CACHE_ARCHIVE_SHA256 or asset.get('bytes') != CACHE_ARCHIVE_BYTES:
        raise ValueError('VEP requires the complete exact original release-115 GRCh38 cache archive')
    if asset.get('assembly') not in [None, ASSEMBLY] or meta.get('assembly') not in [None, ASSEMBLY]:
        raise ValueError('VEP cache assembly must be GRCh38')
    version = asset.get('version_id') or meta.get('version_id')
    if asset.get('path', '').startswith('s3://') and (not version or version == 'null'):
        raise ValueError('VEP cloud cache requires an immutable verified version')
    if meta.get('migration_companions_pending') or asset.get('state') not in [None, 'Ready']:
        raise ValueError('VEP production cache is still awaiting verified migration')
    return {'cache_version': CACHE_VERSION, 'assembly': ASSEMBLY, 'sha256': CACHE_ARCHIVE_SHA256,
            'bytes': CACHE_ARCHIVE_BYTES, 'version_id': version, 'member_manifest_sha256': CACHE_MANIFEST_SHA256,
            'complete_members': CACHE_MEMBER_COUNT, 'extracted_bytes': CACHE_EXTRACTED_BYTES,
            'required_scratch_bytes': 80 * 1024**3, 'scope': 'Complete offline cache; exact reference FASTA and FAI remain required inputs'}


def validate_release(value):
    if (value.get('family'), value.get('version'), value.get('upstream_image'), value.get('cache_version'), value.get('assembly')) != (FAMILY, VERSION, UPSTREAM_IMAGE, CACHE_VERSION, ASSEMBLY):
        raise ValueError('VEP runtime must match the reviewed official release-115.2 image and GRCh38 cache')
    if value.get('tools') != ['vep'] or not re.fullmatch(r'[0-9]{12}\.dkr\.ecr\.us-east-1\.amazonaws\.com/openngs-demo@sha256:[a-f0-9]{64}', value.get('image', '')):
        raise ValueError('Only VEP may use this immutable private worker image')
    for key, expected in [('fixture_manifest_sha256', FIXTURE_MANIFEST_SHA256), ('cache_manifest_sha256', CACHE_MANIFEST_SHA256)]:
        if value.get(key) != expected:
            raise ValueError('VEP fixture or complete cache membership provenance differs')
    if not re.fullmatch('[a-f0-9]{64}', value.get('source_sha256', '')) or not re.fullmatch('[a-f0-9]{64}', value.get('requirements_sha256', '')):
        raise ValueError('VEP source and worker dependency provenance are required')
    if value['requirements_sha256'] != REQUIREMENTS_SHA256:
        raise ValueError('VEP worker dependencies differ from the reviewed hash lock')
    if value.get('worker_apt_lock_sha256') != WORKER_APT_LOCK_SHA256:
        raise ValueError('VEP Python worker apt versions differ from the reviewed lock')
    proof = value.get('qualification', {});cases = proof.get('cases', [])
    if proof.get('version') != VERSION or proof.get('upstream_image') != UPSTREAM_IMAGE or proof.get('network') != 'none':
        raise ValueError('VEP qualification must execute the pinned runtime without network access')
    if proof.get('worker_python_version') != '3.10.12' or proof.get('worker_apt_lock_sha256') != WORKER_APT_LOCK_SHA256 or proof.get('worker_package_versions', {}).get('python3.10') != '3.10.12-1~22.04.18':
        raise ValueError('Qualification must attest the exact Python worker interpreter and apt lock')
    if not re.fullmatch('[a-f0-9]{64}', proof.get('worker_installed_packages_sha256', '')):
        raise ValueError('Qualification must preserve the installed worker package lock digest')
    if {c.get('id') for c in cases} != CASES or len(cases) != len(CASES) or any(c.get('status') != 'passed' for c in cases):
        raise ValueError('Every VEP positive and native rejection control must pass')
    for case in cases:
        checks = case.get('assertions', {})
        if case['id'] in ['release-115-cache', 'spliced-gff-control']:
            if not case.get('outputs') or checks.get('annotated_records', 0) <= 0 or not checks.get('gene_symbols'):
                raise ValueError('VEP positive controls require actual annotated VCFs and gene symbols')
            if checks.get('indexed_records') != checks['annotated_records']:
                raise ValueError('VEP index must retrieve every annotated control record')
            if any(not re.fullmatch('[a-f0-9]{64}', o.get('sha256', '')) or type(o.get('bytes')) is not int or o['bytes'] <= 0 for o in case['outputs']):
                raise ValueError('VEP output bytes and SHA-256 evidence are required')
        elif checks.get('nonzero_exit') is not True:
            raise ValueError('Native VEP must reject each incompatible cache control')
    cache = next(c for c in cases if c['id'] == 'release-115-cache')['assertions']
    if cache.get('annotated_records') != 10 or 'MRPL39' not in cache['gene_symbols'] or cache.get('cache_info_sha256') != CACHE_INFO_SHA256:
        raise ValueError('Original release-115 cache must annotate every bounded human control')
    gff = next(c for c in cases if c['id'] == 'spliced-gff-control')['assertions']
    if gff.get('expected_consequences') != {'stop': 'stop_gained', 'missense': 'missense_variant', 'synonymous': 'synonymous_variant'}:
        raise ValueError('VEP must recover the independently known spliced coding consequences')
    if gff.get('automatic_offline') is not True:
        raise ValueError('Independent GFF control must attest native automatic offline mode')
    if not proof.get('readonly_root') or not proof.get('input_mount_readonly'):
        raise ValueError('VEP requires execution in the production filesystem isolation profile')
    return value


def published():
    from . import db
    row = db.query('SELECT value FROM controls WHERE key=?', ('runtime-release:' + FAMILY,), one=True)
    if row:
        try:
            return validate_release(json.loads(row['value']))
        except (ValueError, TypeError, KeyError):
            return None
    return None
