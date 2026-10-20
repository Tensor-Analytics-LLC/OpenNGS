"""Discover every public contract without turning discovery into execution authority."""
import copy
import json
import os
import re
import threading
import time

from . import capabilities, config, data, db, demo, entitlements, registry, runtime_catalog

_CACHE = {}
_LOCK = threading.Lock()
_DIGEST = re.compile(r'.+@sha256:[a-f0-9]{64}$')
_BINDINGS = {'asset.file', 'reference.asset', 'database.asset'}
_DEPENDENCIES = {
    'resource.grch37': ['grch37-core'], 'resource.grch38': ['grch38-fasta', 'grch38-core'],
    'resource.chm13': ['t2t-core'], 'resource.giab': ['giab-truth-hg002-v4.2.1'],
    'gatk-bqsr': ['gatk-bqsr'], 'vep': ['vep-cache'], 'wes.capture': ['wes-capture'],
    'resource.omim': ['licensed-annotations'], 'resource.cosmic': ['licensed-annotations'],
}


def _shared():
    """Parse immutable command profiles once per cache interval, not once per user."""
    with _LOCK:
        if time.monotonic() - _CACHE.get('time', -1000) >= 20:
            matrix = capabilities.matrix(None)
            _CACHE.update(time=time.monotonic(), observed=time.time(),
                          nodes=registry.catalogue(None),
                          evidence={row['id']: row for row in matrix['capabilities']},
                          dependencies=capabilities.dependencies(),
                          extended=runtime_catalog.published())
        return copy.deepcopy(_CACHE)


def invalidate():
    with _LOCK:
        _CACHE.clear()


def _assets(uid):
    from .asset_listing import list_assets
    # Original workflow IDs survive cloud ingestion and content deduplication.
    # Never expose raw source paths, manifests or signed URLs in discovery.
    return [{k: row[k] for k in ['id', 'name', 'format', 'assembly', 'bytes', 'family', 'data_rights']}
            for row in list_assets(uid)]


def _candidates(node, assets):
    family = node.get('asset_family')
    assembly = next((p.get('default') for p in node['parameters'] if p['key'] == 'assembly'), None)
    rows = [a for a in assets if (not family or a['family'] == family) and (not family or not assembly or a['assembly'] in [None, assembly])]
    if node['group'] == 'References':
        rows = [a for a in rows if a['format'] == 'FASTA']
    elif node['id'] == 'database.asset':
        rows = [a for a in rows if a['format'] in ['DATABASE', 'INDEX', 'INDEXED_VCF', 'VCF', 'BGZIP_VCF', 'TSV', 'CSV', 'ARCHIVE']]
    elif family and node['outputs'][0]['type'] == 'VCF':
        rows = [a for a in rows if a['format'] in ['VCF', 'BGZIP_VCF', 'INDEXED_VCF']]
    elif family:
        rows = [a for a in rows if a['format'] != 'INDEX']
    return rows


def _availability(node, evidence, settings, limited, credentials, shared):
    tier = 'Explore samples' if node['id'] in demo.SOURCES else 'Explore + Research Pro' if node['id'] in demo.TOOLS else 'Research Pro / self-hosted'
    sample = evidence.get('sample_execution')
    validation = sample.get('scope') if sample else 'Awaiting representative execution and scientific validation.'
    if evidence.get('status') == 'implemented-verified':
        validation = 'Operator tests passed; representative assay accuracy and full-scale performance remain separate.'
    extended = shared.get('extended') or {}
    case = next((c for c in extended.get('qualification', {}).get('cases', []) if c.get('tool') == node['id'] and c.get('status') == 'passed'), None)
    if case:
        validation = 'Reviewed isolated execution case passed; this is not full-scale or clinical assay validation.'
    blockers = []
    state = 'Ready'
    if node.get('quarantined'):
        state = 'Unavailable'
        blockers.append('Quarantined: ' + str(node['quarantined']))
    needs_rights = node['license'] in ['Academic', 'Review required', 'Commercial BYOL'] and node['id'] not in settings.get('rights_approved', [])
    needs_secret = node.get('credential') and (node['credential'], node['id']) not in credentials
    if needs_rights or needs_secret:
        if state == 'Ready':
            state = 'Requires entitlement'
        if needs_rights:
            blockers.append('Explicit third-party rights approval is required for this tool, model or database release.')
        if needs_secret:
            blockers.append('A non-revoked ' + node['credential'] + ' credential must be bound to this node in Vault.')
    source = node['id'] in _BINDINGS or node.get('asset_family')
    capacity = settings.get('research_profile', {})
    if source:
        if not node.get('asset_candidates'):
            if state == 'Ready':
                state = 'Requires entitlement' if limited else 'Unavailable'
            blockers.append('Bind an authorized, verified AWS dataset and its exact assembly/release companions.' if config.CLOUD else 'Bind an authorized local asset and its exact release companions.')
        elif all(not a.get('data_rights', {}).get('execution_allowed', True) for a in node['asset_candidates']):
            if state == 'Ready':
                state = 'Requires entitlement'
            blockers.append('The available data releases require administrator acknowledgment of existing use rights against their exact digest and version.')
    elif node['id'] in demo.SOURCES:
        ready = any(d['id'] == 'curated-samples-v1' and d['state'] == 'Ready' for d in shared['dependencies'])
        if config.CLOUD and not ready:
            if state == 'Ready':
                state = 'Provisioning'
            blockers.append('Curated sample manifest and worker image verification is pending.')
    else:
        verified = bool(sample or case or evidence.get('status') == 'implemented-verified')
        if config.CLOUD:
            if limited and node['id'] in demo.TOOLS:
                image = os.getenv('NGS_DEMO_IMAGE', '')
            elif not limited:
                route = capacity.get('runtime_profiles', {}).get(node['id'], {})
                image = route.get('image') or (capacity.get('image') if node.get('builtin') or node['id'] in capacity.get('tools', []) else '')
            else:
                image = evidence.get('hosted_runtime_image') or (extended.get('image') if case else os.getenv('NGS_DEMO_IMAGE', '') if node.get('builtin') else '')
            runtime_ready = bool(_DIGEST.fullmatch(image or ''))
            if not runtime_ready:
                if state == 'Ready':
                    state = 'Unavailable'
                blockers.append('No reviewed, digest-pinned execution runtime is published for this tool in the current workspace.')
            if not limited and capacity.get('state') != 'Ready' and not node.get('api'):
                if state == 'Ready':
                    state = 'Provisioning' if capacity.get('state') == 'Provisioning' else 'Unavailable'
                blockers.append(capacity.get('reason', 'Funded worker capacity is not Ready.'))
        else:
            runtime_ready = bool(node.get('builtin') or node.get('api') or _DIGEST.fullmatch(settings.get('image_approvals', {}).get(node['id'], '') or '') or node.get('approved') and _DIGEST.fullmatch(node.get('image', '') or ''))
            if not runtime_ready:
                if state == 'Ready':
                    state = 'Unavailable'
                blockers.append('Approve an immutable compatible runtime image before execution.')
        if not verified and not node.get('api'):
            if state == 'Ready':
                state = 'Unavailable'
            blockers.append('Executable contract is declared; a representative execution case for this published runtime is still pending.')
        if node.get('api'):
            if state == 'Ready':
                state = 'Requires entitlement'
            blockers.append('Provider agreement, endpoint verification and permitted data-processing consent are checked for each execution.')
    if limited and node['id'] not in demo.TOOLS:
        if state == 'Ready':
            state = 'Requires entitlement'
        blockers.append('Explore shows this node for workflow design. Executing it requires a funded Research Pro allowance or a self-hosted deployment.')
    if not blockers:
        blockers.append('Available for compatible inputs within your dataset, resource and spending allowances; graph preflight validates every bound dependency.')
    return {'state': state, 'reason': ' '.join(blockers), 'tier': tier, 'validation': validation}


def view(uid):
    shared = _shared()
    settings = data.settings(uid)
    limited = entitlements.restricted(uid)
    nodes = shared['nodes']
    for row in db.query('SELECT body,approved FROM custom_tools WHERE user_id=?', (uid,)):
        node = json.loads(row['body'])
        node['approved'] = bool(row['approved'])
        nodes.append(node)
    # No encrypted values are fetched for catalogue display.
    credentials = set()
    for row in db.query('SELECT provider,binding FROM secrets WHERE user_id=? AND revoked=0', (uid,)):
        credentials.update((row['provider'], ident) for ident in json.loads(row['binding']))
    user = db.query('SELECT role FROM users WHERE id=?', (uid,), one=True)
    assets = _assets(uid) if not limited or user and user['role'] == 'admin' else []
    visible = []
    for node in nodes:
        if node.get('hidden'):
            continue
        if node['id'] in _BINDINGS or node.get('asset_family'):
            node['data_license_note'] = 'Data rights vary. The FOSS binding code does not license the selected database, scores or reference content.'
            candidates = _candidates(node, assets)
            node['asset_candidate_count'] = len(candidates)
            node['asset_candidates'] = candidates[:30]
            if len(candidates) == 1:
                next(p for p in node['parameters'] if p['key'] == 'asset_id')['default'] = node['asset_candidates'][0]['id']
        evidence = shared['evidence'].get(node['id'], {})
        for field in ['runtime_provenance','runtime_package']:
            if evidence.get(field):
                node[field] = evidence[field]
        node['availability'] = _availability(node, evidence, settings, limited, credentials, shared)
        node['support_note'] = node['availability']['reason']
        node['dependency_ids'] = _DEPENDENCIES.get(node['id'], [])
        visible.append(node)
    # Dependency metadata excludes private release manifests and presigned URLs.
    dependencies = [{k: v for k, v in row.items() if k in {'id', 'name', 'assembly', 'state', 'requirements', 'reason', 'bytes'}} for row in shared['dependencies']]
    return {'nodes': visible, 'dependencies': dependencies, 'full_catalogue_count': len(visible), 'readiness_updated_at': shared['observed']}
