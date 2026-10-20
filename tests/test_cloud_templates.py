"""Template contracts must be useful without inventing inputs or runtime readiness."""
from backend import config, demo, entitlements, graph, registry, template_catalog


def _catalogue():
    return {'nodes': [
        {'id': spec['id'], 'availability': {'state': 'Ready', 'reason': 'Verified fixture runtime'}}
        for spec in registry.BASE.values() if not spec.get('hidden')
    ]}


def _view(monkeypatch, *, limited=True, catalogue=None, **allowance):
    monkeypatch.setattr(config, 'CLOUD', True)
    limits = {**entitlements.EXPLORE, **allowance}
    return {t['id']: t for t in template_catalog.view(
        'not-read-from-database', catalogue or _catalogue(), limited=limited,
        settings={'research_profile': {'state': 'Ready', 'max_scratch_mb': 184320}}, allowance=limits)}


def test_all_templates_wire_required_ports_and_exact_formats():
    templates = registry.templates()
    assert len(templates) == 21
    assert templates[0]['id'] == 'quickstart'
    for template in templates:
        g = template['graph']
        graph.validate_shape(g)
        nodes = {n['id']: n for n in g['nodes']}
        for edge in g['edges']:
            source = next(p for p in registry.BASE[nodes[edge['source']]['tool']]['outputs'] if p['key'] == edge['sourcePort'])
            target = next(p for p in registry.BASE[nodes[edge['target']]['tool']]['inputs'] if p['key'] == edge['targetPort'])
            assert registry.compatible(source['type'], target['type']), (template['id'], edge)
        for node in nodes.values():
            for port in registry.BASE[node['tool']]['inputs']:
                connected = [e for e in g['edges'] if e['target'] == node['id'] and e['targetPort'] == port['key']]
                assert connected or not port['required'], (template['id'], node['id'], port)
                assert len(connected) <= 1 or port.get('many'), (template['id'], node['id'], port)


def test_seven_curated_explore_templates_are_bounded_and_ready(monkeypatch):
    templates = _view(monkeypatch)
    ready = {t['id'] for t in templates.values() if t['ready_to_run']}
    assert ready == {'quickstart', 'giab', 'rna-qc', 'native-qc', 'dna-engineering', 'clinvar', 'pileup'}
    monkeypatch.setattr(demo, 'DEMO', True)
    for ident in ready:
        t = templates[ident]
        demo.validate(t['graph'])
        assert t['access'] == 'explore'
        assert t['kind'] == 'curated-sample'
        assert not t['required_bindings']
        assert t['clinical_validated'] is False
    assert templates['germline']['availability']['state'] == 'Requires entitlement'


def test_runtime_blocker_prevents_false_sample_readiness(monkeypatch):
    catalogue = _catalogue()
    next(n for n in catalogue['nodes'] if n['id'] == 'fastp')['availability'] = {
        'state': 'Unavailable', 'reason': 'Immutable sample worker has not been published.'}
    t = _view(monkeypatch, catalogue=catalogue)['native-qc']
    assert not t['ready_to_run']
    assert t['availability']['state'] == 'Unavailable'
    assert any(b['tool'] == 'fastp' for b in t['availability']['blockers'] if 'tool' in b)


def test_research_templates_require_explicit_cloud_inputs(monkeypatch):
    templates = _view(monkeypatch, limited=False, max_vcpus=16, max_memory_mb=65536,
                      max_scratch_mb=2048000, max_node_seconds=50000)
    for t in templates.values():
        if t['kind'] != 'research-blueprint':
            continue
        assert not t['ready_to_run']
        assert t['required_bindings']
        by_id = {n['id']: n for n in t['graph']['nodes']}
        for binding in t['required_bindings']:
            assert not by_id[binding['node_id']]['params']['asset_id']
            assert binding['requires_cloud_verification']
            assert binding['formats']
    assert templates['research-read-qc']['availability']['state'] == 'Needs inputs'
    wes = templates['research-wes']
    assert {p['parameter'] for p in wes['required_parameters']} == {'kit_name', 'kit_version'}
    assert any(b['code'] == 'LARGE_SCRATCH_PROFILE' for b in wes['availability']['blockers'])
    assert {b['role'] for b in wes['required_bindings']} >= {'Capture-kit target BED', 'Indexed known-sites VCF'}


def test_allowance_and_runtime_limits_are_separate_from_tier(monkeypatch):
    templates = _view(monkeypatch, limited=False)
    assert templates['single-cell']['availability']['state'] == 'Requires configuration'
    assert any(b['code'] == 'ALLOWANCE_RESOURCES' for b in templates['single-cell']['availability']['blockers'])
    catalogue = _catalogue()
    next(n for n in catalogue['nodes'] if n['id'] == 'deepvariant')['availability'] = {
        'state': 'Unavailable', 'reason': 'The exact DeepVariant execution image is not qualified.'}
    dv = _view(monkeypatch, limited=False, catalogue=catalogue)['deepvariant']
    assert dv['availability']['state'] == 'Unavailable'
    assert 'not qualified' in dv['availability']['reason']


def test_somatic_filters_matching_stats_and_preserves_roles():
    t = next(t for t in registry.templates() if t['id'] == 'somatic')
    nodes = {n['id']: n for n in t['graph']['nodes']}
    caller = next(n for n in nodes.values() if n['tool'] == 'mutect2')
    filtering = next(n for n in nodes.values() if n['tool'] == 'mutect-filter')
    edges = t['graph']['edges']
    assert any(e['source'] == caller['id'] and e['sourcePort'] == 'statistics' and e['target'] == filtering['id'] and e['targetPort'] == 'statistics' for e in edges)
    exported = [e for e in edges if nodes[e['target']]['tool'] == 'utility.export' and e['sourcePort'] == 'variants']
    assert exported and all(e['source'] == filtering['id'] for e in exported)
    assert {n['params']['sample_id'] for n in nodes.values() if n['tool'] == 'bwa-mem2'} == {'TUMOR', 'NORMAL'}
    assert len([n for n in nodes.values() if n['tool'] == 'picard']) == 2


def test_giab_export_and_research_qc_do_not_silently_omit_data():
    templates = {t['id']: t for t in registry.templates()}
    assert any(e['sourcePort'] == 'variants' and e['target'] == 'n3' for e in templates['giab']['graph']['edges'])
    for ident in ['bulk-rna', 'research-read-qc']:
        qc = next(n for n in templates[ident]['graph']['nodes'] if n['tool'] == 'qc.summary')
        assert qc['params']['max_reads'] == 0
    # Every generated graph is independent; editing one never changes defaults.
    templates['quickstart']['graph']['nodes'][1]['params']['max_reads'] = 7
    assert next(t for t in registry.templates() if t['id'] == 'quickstart')['graph']['nodes'][1]['params']['max_reads'] == 100000
