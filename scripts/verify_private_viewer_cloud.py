"""Bounded AWS proof for the isolated private viewer.

Uses only the existing synthetic validation tenant. Never grants an allowance,
changes an owner's data, prints credentials/URLs, or calls a native genomic
parser on the application host. Default is a read-only readiness report.
The --execute mode has a persistent $1 pre-tax conservative reservation ceiling.
"""
import argparse
import csv
import gzip
import hashlib
import io
import json
import math
import re
import time
from pathlib import Path

import httpx

from scripts.verify_research_cloud import BASE, login

MAXIMUM_USD = 1.0
PRIVATE = Path('.state/private-viewer-validation.json')
EVIDENCE = Path('evidence/deployment/private-viewer-cloud.json')
CSV_BYTES = b'gene,expression,pvalue\nGENE_A,10,0.01\nGENE_B,20,0.5\nGENE_C,5,0.001\n'
TERMINAL = {'failed', 'cancelled', 'interrupted'}


def safe_error(value):
    return re.sub(r'https?://\S+', '[redacted URL]', str(value))[:1500]


def checked(response):
    if not 200 <= response.status_code < 300:
        try:
            detail = response.json().get('detail', {})
            message = detail if isinstance(detail, str) else detail.get('message', response.reason_phrase)
        except (ValueError, AttributeError):
            message = response.reason_phrase
        raise RuntimeError(f'API {response.status_code}: {safe_error(message)}')
    return response.json()


def save(checkpoint, evidence):
    PRIVATE.parent.mkdir(exist_ok=True)
    PRIVATE.write_text(json.dumps(checkpoint, indent=2))
    PRIVATE.chmod(0o600)
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(evidence, indent=2))


def graph_for(aid, operation, allowance, params=None, reference_id=None):
    sources = [('source', aid)] + ([('reference', reference_id)] if reference_id else [])
    nodes = [{'id': name, 'tool': 'asset.file', 'params': {'asset_id': source},
              'position': {'x': 0, 'y': i*180}} for i, (name, source) in enumerate(sources)]
    nodes.append({'id': 'prepare', 'tool': 'results.prepare', 'params': {'operation': operation, **(params or {})},
                  'position': {'x': 300, 'y': 0},
                  'resources': {'threads': 2, 'memory_mb': 4096,
                                'scratch_mb': min(32768, allowance['max_scratch_mb']),
                                'timeout_seconds': min(900, allowance['max_node_seconds'])}})
    edges = [{'id': 'e'+str(i), 'source': name, 'sourcePort': 'data', 'target': 'prepare',
              'targetPort': 'data' if name == 'source' else 'reference'} for i, (name, _) in enumerate(sources)]
    return {'name': 'Private results · '+operation, 'nodes': nodes, 'edges': edges,
            'maximum_output_bytes': min(64*1024**2, allowance['max_storage_bytes'])}


def reserve(checkpoint, key, maximum):
    if type(maximum) not in (int, float) or not math.isfinite(maximum) or maximum < 0:
        raise RuntimeError('Validation requires a finite non-negative cost reservation')
    if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0
           for v in checkpoint['reservations'].values()):
        raise RuntimeError('Stored validation reservations need reconciliation')
    old = checkpoint['reservations'].get(key)
    if old is not None:
        if maximum > old + 0.000001:
            raise RuntimeError('The execution estimate changed; review the stored validation reservation')
        return
    if sum(checkpoint['reservations'].values()) + maximum > MAXIMUM_USD:
        raise RuntimeError('The $1 pre-tax viewer validation ceiling cannot cover another operation')
    checkpoint['reservations'][key] = maximum


def upload_csv(client, checkpoint, evidence):
    digest = hashlib.sha256(CSV_BYTES).hexdigest()
    reserve(checkpoint, 'csv-upload', .02)
    save(checkpoint, evidence)
    transfer = checked(client.post('/api/operations/transfers', json={
        'name': 'viewer-known-three-rows.csv', 'bytes': len(CSV_BYTES), 'sha256': digest,
        'operation_id': 'viewer_csv_'+digest[:32]}))
    if transfer['state'] == 'uploading':
        completed = {p['number'] for p in transfer['parts']}
        if 1 not in completed:
            signed = checked(client.post('/api/operations/transfers/'+transfer['id']+'/parts/1', json={}))
            # A new client prevents authentication/CSRF headers reaching S3.
            response = httpx.put(signed['url'], content=CSV_BYTES, timeout=60)
            if response.status_code != 200:
                raise RuntimeError('Synthetic CSV part upload failed with HTTP '+str(response.status_code))
        checked(client.post('/api/operations/transfers/'+transfer['id']+'/complete', json={}))
    deadline = time.monotonic()+180
    while transfer['state'] != 'complete' and time.monotonic() < deadline:
        time.sleep(2)
        transfer = checked(client.get('/api/operations/transfers/'+transfer['id']))
        if transfer['state'] in {'cancelled', 'failed', 'action-required'}:
            raise RuntimeError('Synthetic CSV transfer requires reconciliation: '+transfer['state'])
    if transfer['state'] != 'complete':
        raise RuntimeError('Synthetic CSV integrity verification remains pending; rerun to resume')
    evidence['csv_dataset'] = {'id': transfer['dataset_id'], 'bytes': len(CSV_BYTES), 'sha256': digest,
                               'expected_rows': 3, 'state': 'Ready'}
    return transfer['dataset_id']


def prepare(client, checkpoint, evidence, allowance, key, aid, operation, endpoint, params=None,
            payload=None, reference_id=None):
    record = checkpoint['requests'].get(key)
    if record:
        initial = checked(client.get('/api/viewer/'+record['request_id']))
    else:
        estimate = checked(client.post('/api/operations/estimate', json={
            'graph': graph_for(aid, operation, allowance, params, reference_id)}))
        evidence.setdefault('estimates', {})[key] = estimate
        if not estimate['fits_allowance']:
            reasons = [x.get('message', '') for x in estimate['preflight'].get('issues', [])]
            raise RuntimeError('Validation admission does not fit the existing allowance: '+('; '.join(reasons) or json.dumps(estimate['remaining'])))
        reserve(checkpoint, key, estimate['estimate']['pretax_usd'])
        save(checkpoint, evidence)  # Unknown POST outcomes retain the local reservation.
        initial = checked(client.post(endpoint, json=payload or {}))
        if not initial.get('request_id') or not initial.get('run_id'):
            raise RuntimeError('Private source did not use durable isolated execution')
        record = {'request_id': initial['request_id'], 'run_id': initial['run_id']}
        checkpoint['requests'][key] = record
        save(checkpoint, evidence)
    began = time.monotonic()
    history, previous = [], None
    deadline = began+1800
    result = initial
    while True:
        if result['state'] != previous:
            previous = result['state']
            history.append({'state': previous, 'seconds': round(time.monotonic()-began, 2)})
            print(json.dumps({'case': key, 'state': previous, 'run': record['run_id']}), flush=True)
        if result['state'] == 'ready':
            break
        if result['state'] in TERMINAL:
            raise RuntimeError(key+': '+safe_error(result.get('error') or result['state']))
        if time.monotonic() >= deadline:
            raise RuntimeError(key+' remains '+result['state']+'; accepted work preserved, rerun to resume')
        time.sleep(5)
        result = checked(client.get('/api/viewer/'+record['request_id']))
    run = checked(client.get('/api/runs/'+record['run_id']))
    if run['run']['state'] != 'succeeded':
        raise RuntimeError('Ready viewer response lacks a succeeded execution run')
    evidence.setdefault('executions', {})[key] = {
        **record, 'state': result['state'], 'queue_worker_states': history,
        'observed_seconds': round(time.monotonic()-began, 2),
        'attempts': [{k: a.get(k) for k in ['node_id', 'state', 'provider_id', 'metrics', 'error']} for a in run['attempts']],
        'resource_limits': graph_for(aid, operation, allowance, params, reference_id)['nodes'][-1]['resources'],
        'artifacts': [{k: a.get(k) for k in ['id', 'name', 'format', 'bytes', 'digest']} for a in run['artifacts']]}
    save(checkpoint, evidence)
    return result


def source_rows(client, artifact):
    if artifact['bytes'] > 1024**2:
        raise RuntimeError('Validation accepts at most 1 MiB of existing synthetic VCF data')
    response = client.get('/api/artifacts/'+artifact['id'])
    if response.status_code in {302, 303, 307, 308}:
        # Never carry application authentication headers to an object URL.
        response = httpx.get(response.headers['location'], timeout=30)
    if response.status_code != 200 or len(response.content) > 1024**2:
        raise RuntimeError('Bounded synthetic source export failed')
    raw = response.content
    if hashlib.sha256(raw).hexdigest() != artifact['digest'].removeprefix('sha256:'):
        raise RuntimeError('Synthetic source digest mismatch')
    text = (gzip.decompress(raw) if raw.startswith(b'\x1f\x8b') else raw).decode()
    rows = []
    for line in text.splitlines():
        if line and not line.startswith('#'):
            fields = line.split('\t')
            rows.extend((fields[0], int(fields[1]), fields[3], alt) for alt in fields[4].split(',') if alt != '.')
    if not 1 <= len(rows) <= 200:
        raise RuntimeError('Choose an existing nonempty synthetic VCF with at most 200 alleles')
    return rows


def main():
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument('--execute', action='store_true', help='Run within the existing synthetic grant and $1 sub-budget')
    execute = args.parse_args().execute
    secret = json.loads(Path('.state/research-validation.json').read_text())
    if not secret['credentials']['email'].endswith('@openngs.invalid'):
        raise RuntimeError('Only the synthetic validation account may be used')
    checkpoint = json.loads(PRIVATE.read_text()) if PRIVATE.exists() else {
        'tenant': secret['user_id'], 'reservations': {}, 'requests': {}}
    if checkpoint['tenant'] != secret['user_id']:
        raise RuntimeError('Validation checkpoint belongs to a different account')
    evidence = {'started': time.time(), 'maximum_pretax_usd': MAXIMUM_USD,
                'scope': 'Small synthetic private VCF/CSV/alignment integration; not full-scale or clinical validation',
                'scientifically_validated_full_assay': False}
    client = login(secret['credentials'])
    foreign = None
    try:
        allowance = checked(client.get('/api/account/entitlements'))
        capacity = checked(client.get('/api/account/capacity'))
        profile = capacity.get('runtime_profiles', {}).get('results.prepare')
        evidence['readiness'] = {'tier': allowance['access'], 'tenant_budget_usd': allowance['monthly_budget_usd'],
                                 'capacity_state': capacity['state'], 'viewer_profile_published': bool(profile)}
        if not profile or capacity['state'] != 'Ready':
            raise RuntimeError('Awaiting qualified results.prepare runtime and Ready validation-tenant capacity')
        evidence['runtime_image'] = profile['image']
        if '@sha256:' not in profile['image']:
            raise RuntimeError('Viewer execution image is not immutable')
        if not execute:
            evidence['state'] = 'Ready for bounded execution; no jobs submitted'
            return
        if allowance['access'] != 'research-pro' or allowance['monthly_budget_usd'] > 4:
            raise RuntimeError('Expected the existing $4 synthetic grant; no allowance change is authorized by this script')
        assets = checked(client.get('/api/assets'))['assets']
        run = next((r for r in checked(client.get('/api/runs'))
                    if r['state'] == 'succeeded' and r['graph']['name'].startswith('Synthetic mitochondrial command qualification')), None)
        if not run:
            raise RuntimeError('Existing synthetic mitochondrial qualification output is required')
        artifacts = checked(client.get('/api/runs/'+run['id']))['artifacts']
        variant = next(a for a in artifacts if a['name'] == 'mitochondrial-reconciled.vcf')
        expected = source_rows(client, variant)
        indexed = prepare(client, checkpoint, evidence, allowance, 'variant-index', variant['id'],
                          'variant-index', '/api/results/'+variant['id']+'/index')
        result = checked(client.post('/api/results/'+variant['id']+'/query', json={'filters': {}, 'limit': 200}))
        actual = [(r['chrom'], r['pos'], r['ref'], r['alt']) for r in result['rows']]
        assert actual == expected and indexed['rows'] == result['total'] == len(expected)
        rejected = checked(client.post('/api/results/'+variant['id']+'/query',
                                       json={'filters': {'chrom': 'not_in_synthetic_reference'}}))
        assert rejected['matched'] == 0
        exported = client.post('/api/results/'+variant['id']+'/export/tsv', json={'filters': {'chrom': expected[0][0]}})
        if exported.status_code != 200:
            raise RuntimeError('Filtered TSV export failed')
        exported_rows = list(csv.DictReader(io.StringIO(exported.text), delimiter='\t'))
        filtered_expected = [r for r in expected if r[0] == expected[0][0]]
        assert [(r['chrom'], int(r['pos']), r['ref'], r['alt']) for r in exported_rows] == filtered_expected
        evidence['variant_checks'] = {'source_id': variant['id'], 'source_bytes': variant['bytes'],
                                      'source_sha256': variant['digest'], 'exact_alleles': len(expected),
                                      'query_exact': True, 'absent_contig_matches': 0, 'export_exact': True}
        header = prepare(client, checkpoint, evidence, allowance, 'header', variant['id'],
                         'header', '/api/assets/'+variant['id']+'/inspect')
        assert header['metadata']['header_inspected'] is True
        assert expected[0][0] in header['metadata']['contig_preview']
        evidence['header_checks'] = {'inspected': True, 'expected_contig_found': True,
                                     'contig_count': header['metadata']['contig_count']}
        table = upload_csv(client, checkpoint, evidence)
        omics = prepare(client, checkpoint, evidence, allowance, 'omics', table, 'omics',
                        '/api/omics/'+table, {'x': 'expression', 'y': 'pvalue'},
                        {'x': 'expression', 'y': 'pvalue'})
        assert [(p['label'], p['x'], p['y']) for p in omics['points']] == [
            ('GENE_A', 10, .01), ('GENE_B', 20, .5), ('GENE_C', 5, .001)]
        evidence['omics_checks'] = {'actual_points': 3, 'all_values_exact': True, 'method': omics['method']}
        alignment = next(a for a in assets if a['name'] == 'mito-engineering.bam')
        reference = next(a for a in assets if a['name'] == 'mito-engineering-genome.fa')
        local = Path('evidence/deployment/mito-fixture/mito-engineering.bam')
        assert alignment['metadata']['sha256'] == hashlib.sha256(local.read_bytes()).hexdigest()
        region = {'chrom': 'engineering_contig', 'start': 900, 'end': 1100}
        pileup = prepare(client, checkpoint, evidence, allowance, 'pileup', alignment['id'], 'pileup',
                         '/api/tracks/pileup', region,
                         {'alignment_id': alignment['id'], 'reference_id': reference['id'], **region},
                         reference['id'])
        assert len(pileup['reads']) == 100 and max(pileup['coverage']) == 100 and not pileup['truncated']
        assert len(pileup['reference']) == len(pileup['coverage']) == 200
        evidence['pileup_checks'] = {'reads': 100, 'maximum_coverage': 100, 'bases': 200,
                                     'truncated': False, 'transport': pileup['transport']}
        # Read-only probes from another authenticated tenant, including admin,
        # must not bypass the ordinary per-tenant ownership boundary.
        owner = json.loads(Path('.state/owner-access.json').read_text())
        foreign = login({k: owner[k] for k in ['email', 'password']})
        denied = []
        for method, path, body in [
            ('GET', '/api/viewer/'+checkpoint['requests']['variant-index']['request_id'], None),
            ('POST', '/api/results/'+variant['id']+'/query', {'filters': {}}),
            ('POST', '/api/results/'+variant['id']+'/export/tsv', {'filters': {}})]:
            response = foreign.request(method, path, **({'json': body} if body is not None else {}))
            assert response.status_code in {403, 404, 422}
            denied.append(response.status_code)
        evidence['isolation_checks'] = {'cross_tenant_status_codes': denied, 'all_denied': True}
        evidence['billing'] = {'conservative_pretax_reserved_usd': sum(checkpoint['reservations'].values()),
                               'per_operation': checkpoint['reservations'],
                               'note': 'Conservative admission upper bounds, not the finalized AWS invoice; no grants changed.'}
        evidence['state'] = 'Passed AWS isolated private viewer integration'
    except Exception as error:
        evidence.update(state='Blocked or failed; no claim of successful validation', error=safe_error(error))
        print(json.dumps({'state': evidence['state'], 'error': evidence['error']}), flush=True)
        raise SystemExit(1)
    finally:
        evidence['finished'] = time.time()
        save(checkpoint, evidence)
        client.close()
        if foreign:
            foreign.close()
        print(json.dumps({'evidence': str(EVIDENCE), 'state': evidence.get('state'),
                          'reserved_pretax_usd': round(sum(checkpoint['reservations'].values()), 6)}), flush=True)


if __name__ == '__main__':
    main()
