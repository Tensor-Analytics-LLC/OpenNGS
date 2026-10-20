"""Content-specific rights review, independent of the FOSS binding-node licence."""
import hashlib
import json
import re
import time
from urllib.parse import urlsplit

from . import db

POLICY = 1
PUBLIC = {
    'reference': 'https://www.ncbi.nlm.nih.gov/datasets/docs/v2/policies/',
    'clinvar': 'https://www.ncbi.nlm.nih.gov/clinvar/docs/maintenance_use/',
    'gnomad': 'https://gnomad.broadinstitute.org/terms',
    'giab': 'https://www.nist.gov/programs-projects/genome-bottle',
    'ensembl': 'https://www.ensembl.org/info/about/legal/disclaimer.html',
}
REVIEW_FAMILIES = {'alphamissense', 'revel', 'cadd', 'exomiser', 'omim', 'dbnsfp', 'spliceai', 'vep'}


def initialize():
    with db.tx() as c:
        c.execute('CREATE TABLE IF NOT EXISTS data_rights_grants(user_id TEXT NOT NULL,identity TEXT NOT NULL,body TEXT NOT NULL,revoked INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(user_id,identity))')


def metadata(row):
    value = row.get('metadata') or {}
    return json.loads(value) if isinstance(value, str) else value


def describe(row):
    meta = metadata(row)
    name = row.get('name', '')
    family = row.get('family') or meta.get('family') or ''
    text = (name+' '+family+' '+str(meta.get('original_name', ''))).lower()
    digest = (row.get('digest') or meta.get('sha256') or '').removeprefix('sha256:')
    version = meta.get('version_id') or ''
    release = str(meta.get('release') or meta.get('version') or name)
    state, terms, note, uses = 'Review required', '', 'Review the exact database release and intended use before execution.', ['reviewed-source-terms', 'provider-authorized']
    required = False
    if 'dbnsfp' in text:
        required = True
        release_match = re.search(r'dbnsfp[._-]?([0-9]+(?:\.[0-9]+)*[ac]?)', text)
        release = release_match.group(1) if release_match else release
        family = 'dbnsfp'
        terms = 'https://www.dbnsfp.org/license/'
        note = 'dbNSFP branches and upstream score components have separate rights. The academic branch is noncommercial; a download does not grant commercial rights.'
        if release.endswith('a'):
            state, uses = 'Academic / noncommercial', ['academic-noncommercial', 'provider-authorized']
    elif 'spliceai' in text:
        required, family, state = True, 'spliceai', 'Academic / noncommercial'
        terms = 'https://github.com/Illumina/SpliceAI'
        note = 'Precomputed annotations are offered for academic/not-for-profit use. Other use needs provider authorization; the binding node licence does not license the scores.'
        uses = ['academic-noncommercial', 'provider-authorized']
    elif 'omim' in text or 'mim2gene' in text:
        required, family = True, 'omim'
        terms = 'https://www.omim.org/help/agreement'
        note = 'Review the precise OMIM file terms. Software incorporation, redistribution and commercial use can require provider licensing; public download alone is not authorization.'
        uses = ['provider-authorized', 'reviewed-source-terms']
    elif family in PUBLIC:
        state, terms = 'Public resource terms', PUBLIC[family]
        note = 'Retain the exact release and source attribution. Public resource terms are separate from the FOSS binding-node licence.'
    elif family in REVIEW_FAMILIES or row.get('format') == 'DATABASE' and 'data_rights' not in meta:
        required = True
    else:
        state, note = 'User / workflow data', 'Dataset ownership, consent and execution allowances still apply.'
    immutable = bool(re.fullmatch('[0-9a-f]{64}', digest))
    identity = hashlib.sha256(db.encode([POLICY, digest, version, family, release, state]).encode()).hexdigest() if immutable else None
    return {'label': state, 'required': required, 'identity': identity, 'sha256': digest if immutable else None,
            'version_id': version or None, 'family': family, 'release': release, 'terms_url': terms,
            'reason': note, 'permitted_uses': uses if required else [], 'policy_version': POLICY}


def grant_map(uid):
    return {r['identity']: json.loads(r['body']) for r in db.query('SELECT identity,body FROM data_rights_grants WHERE user_id=? AND revoked=0', (uid,))}


def known_sources(uid):
    """Batch the owner's known restricted source identities, including renames."""
    families = sorted(REVIEW_FAMILIES)
    rows = db.query('SELECT name,family,format,metadata FROM assets WHERE user_id=? AND (family IN ('+
                    ','.join('?' for _ in families)+") OR format='DATABASE' OR LOWER(name) LIKE '%dbnsfp%' OR LOWER(name) LIKE '%spliceai%' OR LOWER(name) LIKE '%mim2gene%')",
                    (uid,*families))
    # Migration source records are server-verified originals. Keep their family
    # and release even if an asset is later renamed or its display family changes.
    originals = db.query("""SELECT m.source_record,d.sha256,d.version_id
      FROM migration_assets m JOIN datasets d ON d.id=m.dataset_id AND d.user_id=m.user_id
      WHERE m.user_id=? AND m.asset_id=m.canonical_asset_id AND d.state='Ready'""",(uid,))
    for original in originals:
        source = json.loads(original['source_record'])
        source['metadata'] = {**metadata(source),'sha256':original['sha256'],'version_id':original['version_id']}
        rows.append(source)
    result = {}
    for row in rows:
        value = describe(row)
        if value['required'] and value['sha256']:
            result.setdefault(value['sha256'],{})[value['identity']] = value
    return {digest:list(values.values()) for digest,values in result.items()}


def content_requirements(row, sources):
    base = describe(row)
    values = [base] if base['required'] else []
    for known in sources.get(base['sha256'],[]):
        value = dict(known)
        value['version_id'] = base['version_id']
        value['identity'] = hashlib.sha256(db.encode([POLICY,value['sha256'],value['version_id'] or '',
                                                     value['family'],value['release'],value['label']]).encode()).hexdigest()
        values.append(value)
    return values


def inherited_rights(uid, row, seen=None, sources=None):
    meta = metadata(row)
    inherited = list(meta.get('data_rights', []))
    rid = row.get('run_id')
    if not rid:
        return inherited
    seen = set() if seen is None else set(seen)
    if rid in seen or len(seen) >= 32:
        raise ValueError('Data-rights provenance exceeds its safe traversal bound')
    seen.add(rid)
    run = db.query('SELECT graph FROM runs WHERE id=? AND user_id=?', (rid, uid), one=True)
    if not run:
        raise ValueError('Data-rights provenance run is unavailable')
    result = inherited
    for source in json.loads(run['graph']).get('_execution', {}).get('assets', {}).values():
        result.extend(content_requirements(source,sources or {}))
        result.extend(inherited_rights(uid, source, seen, sources))
    return result


def disclosure(uid, row, grants=None, sources=None):
    item = describe(row)
    sources = known_sources(uid) if sources is None else sources
    inherited = inherited_rights(uid, row, sources=sources)
    grants = grant_map(uid) if grants is None else grants
    requirements = content_requirements(row,sources)
    requirements.extend(inherited)
    unique = {r.get('identity') or r.get('release'): r for r in requirements}
    item['requirements'] = [{**r, 'acknowledged': bool(r.get('identity') and r['identity'] in grants),
                            'acknowledged_use': grants.get(r.get('identity'), {}).get('permitted_use')} for r in unique.values()]
    item['execution_allowed'] = all(r['acknowledged'] for r in item['requirements'])
    item['review_note'] = 'Acknowledgment records the administrator’s stated authorization; OpenNGS does not issue or verify a third-party licence.'
    return item


def require(uid, row):
    if row.get('id') and not row.get('run_id'):
        prior = db.query('SELECT * FROM artifacts WHERE id=? AND user_id=?', (row['id'],uid), one=True)
        if prior:
            row = prior
    result = disclosure(uid, row)
    missing = [r for r in result['requirements'] if not r['acknowledged']]
    if missing:
        raise ValueError('Data rights review required for '+', '.join(r['release'] for r in missing)+'. An administrator must record permitted use against the exact content digest and version.')
    return [{k: v for k, v in r.items() if k not in {'acknowledged', 'acknowledged_use'}} for r in result['requirements']]


def require_inputs(uid, inputs):
    obligations = {}
    for items in inputs.values():
        for row in items:
            for item in require(uid, row):
                obligations[item['identity']] = item
    return list(obligations.values())


def attach(uid, row):
    value = dict(row)
    value['metadata'] = {**metadata(row), 'data_rights': require(uid, row)}
    return value


def owned(uid, aid):
    row = db.query('SELECT * FROM assets WHERE user_id=? AND id=?', (uid, aid), one=True) or db.query('SELECT * FROM artifacts WHERE user_id=? AND id=?', (uid, aid), one=True)
    if not row:
        raise ValueError('Asset not found in this workspace')
    from .migration import resolve_asset
    return resolve_asset(uid, row)


def acknowledge(actor, uid, aid, payload):
    if actor['role'] != 'admin':
        raise ValueError('Administrator review is required')
    row = owned(uid, aid)
    result = disclosure(uid, row)
    identity = payload.get('identity')
    selected = next((r for r in result['requirements'] if r.get('identity') == identity and identity), None)
    if not selected:
        raise ValueError('Select a current immutable data-rights identity; changed content or version requires a new review')
    reason = str(payload.get('reason', '')).strip()
    use = payload.get('permitted_use')
    url = str(payload.get('terms_url', '')).strip()
    parsed = urlsplit(url)
    if not 20 <= len(reason) <= 2000 or use not in selected['permitted_uses']:
        raise ValueError('Specify a permitted use and at least 20 characters describing the authorization evidence')
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or len(url) > 2000:
        raise ValueError('Provide the reviewed HTTPS provider terms URL')
    if selected['terms_url'] and url != selected['terms_url']:
        raise ValueError('Review the declared provider terms URL for this release')
    if payload.get('confirm_authorization') is not True:
        raise ValueError('Explicit confirmation of existing authorization is required')
    body = {'actor': actor['id'], 'tenant': uid, 'asset_id': aid, 'identity': identity,
            'sha256': selected['sha256'], 'version_id': selected['version_id'], 'release': selected['release'],
            'permitted_use': use, 'terms_url': url, 'reason': reason, 'at': time.time()}
    with db.tx() as c:
        c.execute('INSERT INTO data_rights_grants VALUES(?,?,?,0) ON CONFLICT(user_id,identity) DO UPDATE SET body=excluded.body,revoked=0',
                  (uid, identity, db.encode(body)))
        db.audit(c, actor['id'], 'data.rights.acknowledged', aid, body)
    return disclosure(uid, row)


def revoke(actor, uid, identity, reason):
    if actor['role'] != 'admin' or not 10 <= len(reason) <= 2000:
        raise ValueError('Administrator and a reason of at least ten characters are required')
    with db.tx() as c:
        c.execute('UPDATE data_rights_grants SET revoked=1 WHERE user_id=? AND identity=?', (uid, identity))
        db.audit(c, actor['id'], 'data.rights.revoked', identity, {'tenant': uid, 'reason': reason})
    return {'revoked': True}


initialize()
