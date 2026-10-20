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
    result = []
    for row in list_assets(uid, primary_only=True):
        item = {k: row[k] for k in ['id', 'name', 'format', 'assembly', 'bytes', 'family', 'data_rights', 'presentation']}
        item.update({k: row['metadata'].get(k) for k in ['release','version_id','sha256']})
        result.append(item)
    return result


def _candidates(node, assets):
    family = node.get('asset_family')
    assembly = next((p.get('default') for p in node['parameters'] if p['key'] == 'assembly'), None)
    rows = [a for a in assets if (not family or a['family'] == family) and (not family or not assembly or a['assembly'] in [None, assembly])]
    if node['group'] == 'References':
        rows = [a for a in rows if a['format'] == 'FASTA']
    elif node['id'] == 'database.asset':
        rows = [a for a in rows if a['format'] in ['DATABASE', 'INDEXED_VCF', 'VCF', 'BGZIP_VCF', 'TSV', 'CSV', 'ARCHIVE']]
    elif family and node['outputs'][0]['type'] == 'VCF':
        rows = [a for a in rows if a['format'] in ['VCF', 'BGZIP_VCF', 'INDEXED_VCF']]
    elif family:
        rows = [a for a in rows if a['format'] != 'INDEX']
    return rows


def _availability(node, evidence, settings, limited, credentials, shared):
    tier = 'Explore samples' if node['id'] in demo.SOURCES else 'Explore + Research Pro' if node['id'] in demo.TOOLS else 'Research Pro'
    sample = evidence.get('sample_execution')
    validation = sample.get('scope') if sample else 'Awaiting representative execution and scientific validation.'
    if evidence.get('status') == 'implemented-verified':
        validation = 'Operator tests passed; representative assay accuracy and full-scale performance remain separate.'
    extended = shared.get('extended') or {}
    case = next((c for c in extended.get('qualification', {}).get('cases', []) if c.get('tool') == node['id'] and c.get('status') == 'passed'), None)
    if case:
        validation = 'Reviewed isolated execution case passed; this is not full-scale or clinical assay validation.'
    blockers = []
    def block(kind, code, message):
        blockers.append({'kind': kind, 'code': code, 'message': message})
    state = 'Ready'
    if node.get('quarantined'):
        state = 'Unavailable'
        block('security', 'QUARANTINED', str(node['quarantined']))
    needs_rights = node['license'] in ['Academic', 'Review required', 'Commercial BYOL'] and node['id'] not in settings.get('rights_approved', [])
    needs_secret = node.get('credential') and (node['credential'], node['id']) not in credentials
    if needs_rights or needs_secret:
        if state == 'Ready':
            state = 'Requires entitlement'
        if needs_rights:
            block('licence', 'TOOL_RIGHTS', 'Administrator review of this tool or model licence is required.')
        if needs_secret:
            block('credential', 'VAULT_BINDING', 'Bind a valid ' + node['credential'] + ' key or licence token in Vault.')
    source = node['id'] in _BINDINGS or node.get('asset_family')
    capacity = settings.get('research_profile', {})
    if source:
        if not node.get('asset_candidates'):
            if state == 'Ready':
                state = 'Requires entitlement' if limited else 'Unavailable'
            block('dependency', 'SOURCE_DATA', 'Choose a verified cloud dataset with the required assembly and companion files.')
        elif node.get('asset_authorized_count') == 0:
            if state == 'Ready':
                state = 'Requires entitlement'
            block('licence', 'DATA_RIGHTS', 'Review the selected release’s data rights before execution.')
    elif node['id'] in demo.SOURCES:
        ready = any(d['id'] == 'curated-samples-v1' and d['state'] == 'Ready' for d in shared['dependencies'])
        if config.CLOUD and not ready:
            if state == 'Ready':
                state = 'Provisioning'
            block('dependency', 'SAMPLE_PROVISIONING', 'Curated sample verification is in progress.')
    elif evidence.get('execution_kind') == 'artifact-routing':
        # These reviewed runner operations route authenticated records, not
        # genomic bytes. A container/assay proof is the wrong readiness test.
        # Quarantine, rights and tier blockers above/below still apply.
        contract = evidence.get('orchestration_contract') or {}
        if evidence.get('status') != 'implemented-verified' or contract.get('version') != 1:
            if state == 'Ready':
                state = 'Unavailable'
            block('validation', 'ROUTING_UNVERIFIED', 'The artifact-routing contract has not been verified.')
        validation = evidence.get('validation_scope', 'Artifact-routing contract only; no scientific inference.')
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
                block('runtime', 'RUNTIME_UNAVAILABLE', 'A compatible published execution runtime is not available.')
            if not limited and capacity.get('state') != 'Ready' and not node.get('api'):
                if state == 'Ready':
                    state = 'Provisioning' if capacity.get('state') == 'Provisioning' else 'Unavailable'
                block('runtime', 'CAPACITY_NOT_READY', capacity.get('reason', 'Funded worker capacity is not ready.'))
        else:
            runtime_ready = bool(node.get('builtin') or node.get('api') or _DIGEST.fullmatch(settings.get('image_approvals', {}).get(node['id'], '') or '') or node.get('approved') and _DIGEST.fullmatch(node.get('image', '') or ''))
            if not runtime_ready:
                if state == 'Ready':
                    state = 'Unavailable'
                block('runtime', 'RUNTIME_APPROVAL', 'Approve an immutable compatible runtime image.')
        if not verified and not node.get('api'):
            if state == 'Ready':
                state = 'Unavailable'
            block('validation', 'EXECUTION_UNVERIFIED', 'A representative execution case is still pending.')
        if node.get('api'):
            if state == 'Ready':
                state = 'Requires entitlement'
            block('licence', 'PROVIDER_TERMS', 'Provider agreement, endpoint verification and data-processing consent are required.')
    if limited and node['id'] not in demo.TOOLS:
        if state == 'Ready':
            state = 'Requires entitlement'
        block('allowance', 'RESEARCH_ALLOWANCE', 'This workload needs an administrator-assigned Research Pro allowance.')
    reason = ' '.join(item['message'] for item in blockers) or 'Ready for compatible inputs within your allowance.'
    return {'state': state, 'reason': reason, 'blockers': blockers, 'tier': tier, 'validation': validation}


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
            node['asset_authorized_count'] = sum(a.get('data_rights', {}).get('execution_allowed', True) for a in candidates)
            node['asset_candidates'] = candidates[:30]
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
