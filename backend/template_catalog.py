"""Cloud template discovery; execution authority remains in server preflight.

No private asset is guessed or auto-bound to a research blueprint. Readiness
describes the saved starting graph, not an allowance reservation or assay claim.
"""
import copy

from . import config, data, demo, entitlements

_BINDINGS = {'asset.file', 'reference.asset', 'database.asset'}
_VERIFIED_SAMPLES = {'quickstart', 'giab', 'rna-qc', 'native-qc', 'dna-engineering', 'clinvar', 'pileup'}
_ROLES = {
    'deepvariant': {0: 'Paired DNA reads', 1: 'Exact reference genome'},
    'somatic': {0: 'Tumor paired reads', 1: 'Matched normal paired reads', 2: 'Exact reference genome'},
    'long-read': {0: 'Nanopore or PacBio reads', 1: 'Exact reference genome'},
    'bulk-rna': {0: 'Paired RNA reads', 1: 'Transcript FASTA or complete Salmon index'},
    'single-cell': {0: 'AnnData raw-count matrix'},
    'giab-validation': {0: 'Query variant callset', 1: 'Matching GIAB truth callset',
                        2: 'Confident-region BED', 3: 'Exact benchmark reference'},
    'research-read-qc': {0: 'Paired reads to assess and trim'},
    'research-variant-review': {0: 'Variant callset to review'},
    'research-cohort': {0: 'Sample 1 gVCF', 1: 'Sample 2 gVCF', 2: 'Exact reference genome'},
    'research-metagenomics': {0: 'Reads to classify', 1: 'Complete Kraken2 database bundle'},
    'research-wgs': {0: 'Paired DNA reads', 1: 'Exact whole-genome reference',
                     2: 'Indexed known-sites VCF', 3: 'Indexed allele-annotation VCF'},
    'research-wes': {0: 'Paired exome reads', 1: 'Exact whole-genome reference',
                     2: 'Indexed known-sites VCF', 3: 'Indexed allele-annotation VCF',
                     16: 'Capture-kit target BED'},
    'research-mtdna': {0: 'Indexed whole-genome alignment', 1: 'Exact whole-genome reference',
                       2: 'Reviewed mitochondrial and nuclear candidate BED'},
}


def describe(template):
    """Attach stable scientific/input metadata without a database lookup."""
    from .registry import BASE
    result = copy.deepcopy(template)
    nodes = {n['id']: n for n in result['graph']['nodes']}
    bindings = []
    parameters = []
    for index, node in enumerate(nodes.values()):
        spec = BASE[node['tool']]
        if node['tool'] in _BINDINGS:
            constraints = []
            for edge in result['graph']['edges']:
                if edge['source'] == node['id']:
                    target = BASE[nodes[edge['target']]['tool']]
                    port = next(p for p in target['inputs'] if p['key'] == edge['targetPort'])
                    if port['type'] != 'ANY':
                        constraints.append(set(port['type'].split('|')))
            formats = sorted(set.intersection(*constraints)) if constraints else []
            if node['tool'] == 'reference.asset':
                formats = ['FASTA']
            bindings.append({
                'node_id': node['id'], 'parameter': 'asset_id',
                'role': _ROLES.get(result['id'], {}).get(index, spec['name']),
                'formats': formats,
                'requires_cloud_verification': True,
                'requires_companion_files': any(t in formats for t in ['INDEXED_BAM', 'INDEXED_CRAM', 'INDEXED_VCF', 'INDEX', 'DATABASE']),
            })
        for parameter in spec['parameters']:
            if parameter.get('required') and node.get('params', {}).get(parameter['key']) in ['', None]:
                parameters.append({'node_id': node['id'], 'parameter': parameter['key'], 'label': parameter['label']})
    sample = not bindings and any(n['tool'] in demo.SOURCES for n in nodes.values())
    resources = [n['resources'] for n in nodes.values()]
    result.update(
        kind='curated-sample' if sample else 'research-blueprint',
        access='explore' if all(n['tool'] in demo.TOOLS for n in nodes.values()) else 'research-pro',
        required_bindings=bindings, required_parameters=parameters,
        clinical_validated=False, validation_state='sample-run-verified' if result['id'] in _VERIFIED_SAMPLES else 'representative-validation-required',
        resource_requirements={
            'peak_vcpus': max(r['threads'] for r in resources),
            'peak_memory_mb': max(r['memory_mb'] for r in resources),
            'peak_scratch_mb': max(r.get('scratch_mb', 2048) for r in resources),
            'maximum_node_seconds': max(r['timeout_seconds'] for r in resources),
            'sizing_note': 'Starting allocations must fit the complete input, exact dependencies and funded worker profile. The application never shrinks scientific inputs to fit.',
        })
    if result['id'] in _VERIFIED_SAMPLES:
        result['validation_status'] = 'The constituent curated cloud workflow has passed sample execution; template edits are checked at submission. This is software/sample evidence, not clinical validation.'
    elif not result.get('validation_status'):
        result['validation_status'] = 'Small-data operator evidence is available separately; this exact graph and representative full-scale performance require validation.'
    return result


def view(uid, catalogue, *, settings=None, allowance=None, limited=None):
    """Return templates with input/runtime blockers for the current workspace.

    Accept an already-built node catalogue to avoid repeated capability scans.
    No AWS calls, secret reads, writes or budget reservations occur here.
    """
    from .registry import templates
    settings = data.settings(uid) if settings is None else settings
    allowance = entitlements.limits(uid) if allowance is None else allowance
    limited = entitlements.restricted(uid) if limited is None else limited
    nodes = {n['id']: n for n in catalogue['nodes']}
    result = []
    capacity = settings.get('research_profile', {})
    for template in templates():
        blockers = []
        def block(code, message, **extra):
            row = {'code': code, 'message': message, **extra}
            if row not in blockers:
                blockers.append(row)
        pro_required = limited and template['access'] == 'research-pro'
        if pro_required:
            block('RESEARCH_ALLOWANCE', 'An administrator must activate a funded Research Pro allowance and compatible cloud workers.')
        for binding in template['required_bindings']:
            block('INPUT_REQUIRED', 'Choose ' + binding['role'].lower() + ' from verified cloud datasets.', node_id=binding['node_id'])
        for parameter in template['required_parameters']:
            block('PARAMETER_REQUIRED', 'Set ' + parameter['label'].lower() + '.', node_id=parameter['node_id'])
        for node in template['graph']['nodes']:
            if node['tool'] in _BINDINGS:
                continue
            available = nodes.get(node['tool'], {}).get('availability')
            if not available:
                block('CAPABILITY_UNKNOWN', 'The tool is not in the current published catalogue.', tool=node['tool'])
            elif available.get('state') != 'Ready':
                block('NODE_UNAVAILABLE', available.get('reason', 'This tool is not ready.'), tool=node['tool'])
        if not pro_required:
            for field, resource in [('max_vcpus', 'peak_vcpus'), ('max_memory_mb', 'peak_memory_mb'),
                                    ('max_scratch_mb', 'peak_scratch_mb'), ('max_node_seconds', 'maximum_node_seconds')]:
                if template['resource_requirements'][resource] > allowance[field]:
                    block('ALLOWANCE_RESOURCES', 'This starting allocation exceeds your assigned ' + field.replace('_', ' ') + '.',
                          resource=field, required=template['resource_requirements'][resource], allowed=allowance[field])
        if config.CLOUD and not limited and capacity.get('state') != 'Ready':
            block('CAPACITY_NOT_READY', capacity.get('reason', 'Funded cloud execution capacity is not ready.'))
        if template['resource_requirements']['peak_scratch_mb'] > capacity.get('max_scratch_mb', 184320):
            block('LARGE_SCRATCH_PROFILE', 'This full-input blueprint needs the exclusive-machine profile. Its current runtime supports tenant-bucket inputs; migrated shared-cache inputs need a refreshed machine image. Select a compatible funded profile before running.')
        if pro_required:
            state = 'Requires entitlement'
            note = 'Research Pro · funded allowance required'
        elif any(b['code'] in ['NODE_UNAVAILABLE', 'CAPABILITY_UNKNOWN', 'CAPACITY_NOT_READY'] for b in blockers):
            state = 'Unavailable'
            note = 'Cloud runtime or dependency preparation required'
        elif any(b['code'] in ['ALLOWANCE_RESOURCES', 'LARGE_SCRATCH_PROFILE'] for b in blockers):
            state = 'Requires configuration'
            note = 'Worker sizing or allowance needs configuration'
        elif blockers:
            state = 'Needs inputs'
            note = 'Select ' + str(len(template['required_bindings'])) + ' cloud input' + ('s' if len(template['required_bindings']) != 1 else '')
        else:
            state = 'Ready'
            note = 'Ready to run · allowance checked on submission'
        template.update(ready_to_run=not blockers)
        template['availability'] = {
            'state': state, 'note': note, 'reason': ' '.join(b['message'] for b in blockers) or note,
            'blockers': blockers, 'ready_to_run': not blockers,
            'admission_note': 'Run checks exact input compatibility, current quotas, storage and cost reservations before execution.',
        }
        result.append(template)
    return result
