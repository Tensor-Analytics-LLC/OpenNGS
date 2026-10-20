"""Cache rights disclosure changes no licence grants or source identities."""
import hashlib

from backend import asset_rights, db, registry
from backend.vep_runtime import CACHE_ARCHIVE_NAME, CACHE_ARCHIVE_SHA256


def cache(*, digest=CACHE_ARCHIVE_SHA256, name=CACHE_ARCHIVE_NAME, metadata=None):
    return {'name': name, 'family': 'vep', 'format': 'DATABASE', 'sha256': digest,
            'metadata': {'version_id': 'existing-immutable-version', **(metadata or {})}}


def previous_identity(row):
    """The former generic REVIEW_FAMILIES branch's unchanged identity inputs."""
    meta = row['metadata']
    release = str(meta.get('release') or meta.get('version') or row['name'])
    return hashlib.sha256(db.encode([asset_rights.POLICY, row['sha256'], meta['version_id'],
                                    'vep', release, 'Review required']).encode()).hexdigest()


def test_exact_original_cache_has_release_specific_terms_and_preserves_existing_identity():
    row = cache();rights = asset_rights.describe(row)
    assert rights['label'] == 'Review required' and rights['required'] is True
    assert rights['identity'] == previous_identity(row)
    assert rights['release'] == CACHE_ARCHIVE_NAME
    assert rights['terms_url'] == 'https://sep2025.archive.ensembl.org/info/about/legal/disclaimer.html'
    assert rights['permitted_uses'] == ['reviewed-source-terms', 'provider-authorized']
    assert 'code is open source' in rights['reason']
    assert 'third-party annotations' in rights['reason']
    assert 'does not request SIFT, PolyPhen or known-variant annotations' in rights['reason']
    assert 'paid' not in rights['reason'].lower() and 'purchase' not in rights['reason'].lower()
    assert 'tool_license' not in rights and 'license_required' not in rights
    assert registry.BASE['vep']['license'] == 'FOSS'


def test_note_and_terms_change_does_not_invalidate_an_existing_cache_acknowledgment():
    row = cache(metadata={'release': '115', 'version': 'user-original-version'})
    rights = asset_rights.describe(row)
    assert rights['release'] == '115' and rights['identity'] == previous_identity(row)
    identity = previous_identity(row)
    disclosed = asset_rights.disclosure('existing-owner', row,
        grants={identity: {'permitted_use': 'reviewed-source-terms'}}, sources={})
    assert disclosed['execution_allowed'] is True
    assert len(disclosed['requirements']) == 1
    assert disclosed['requirements'][0]['acknowledged'] is True
    assert disclosed['requirements'][0]['acknowledged_use'] == 'reviewed-source-terms'
    missing = asset_rights.disclosure('existing-owner', row, grants={}, sources={})
    assert missing['execution_allowed'] is False


def test_unknown_cache_and_same_basename_do_not_acquire_a_release_specific_permission():
    for name in [CACHE_ARCHIVE_NAME, 'custom-cache.tar.gz']:
        row = cache(digest='a' * 64, name=name);rights = asset_rights.describe(row)
        assert rights['required'] is True and rights['label'] == 'Review required'
        assert rights['terms_url'] == 'https://www.ensembl.org/info/about/legal/disclaimer.html'
        assert rights['release'] == name and rights['identity'] == previous_identity(row)
    row = cache(digest='a' * 64, metadata={'release': 'unknown-user-release'})
    assert asset_rights.describe(row)['release'] == 'unknown-user-release'


def test_hpo_public_terms_and_dbnsfp_restrictions_are_unchanged():
    hpo = asset_rights.describe({'name': 'hp.obo', 'family': 'hpo', 'format': 'DATABASE',
        'sha256': 'a5092cbdf605f568403cf7380d9173014015692433b2cc631bc5c1b053876b1b'})
    assert hpo['family'] == 'hpo' and hpo['required'] is False
    assert hpo['release'] == '2026-06-23' and hpo['label'] == 'Public resource terms'
    dbnsfp = asset_rights.describe({'name': 'dbNSFP4.9a.gz', 'family': 'dbnsfp', 'format': 'DATABASE', 'sha256': 'b' * 64})
    assert dbnsfp['required'] is True and dbnsfp['label'] == 'Academic / noncommercial'
    assert dbnsfp['terms_url'] == 'https://www.dbnsfp.org/license/'
    assert dbnsfp['permitted_uses'] == ['academic-noncommercial', 'provider-authorized']
