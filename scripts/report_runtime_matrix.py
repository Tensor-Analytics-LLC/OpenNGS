"""Export source/version/qualification facts without private URLs or credentials."""
import ast
import json
from pathlib import Path
from backend.registry import BASE
from backend.runtime_catalog import validate_release,EXTENDED_TOOLS,EXTENDED_PACKAGES,CORE_PACKAGES,CORE_PACKAGE_TOOLS,ADJUNCT_TOOLS,BLOCKERS
from backend.secondary_runtime import validate_release as validate_secondary,ENVIRONMENTS,TOOLS

def main():
    extension=validate_release(json.loads(Path('evidence/deployment/extended-release.json').read_text()))
    path=Path('evidence/deployment/secondary-release.json');secondary=validate_secondary(json.loads(path.read_text())) if path.exists() else None
    verified={}
    def add(release,proof,tools,packages,family):
        for c in proof.get('cases',[]):
            if c['tool'] in tools:
                verified[c['tool']]={'status':'executed-small-control','image':release['image'],'source_sha256':release['source_sha256'],
                    'runtime_family':family,'packages':packages(c['tool']),'scope':c.get('scope',proof.get('scope',release.get('validation_scope'))),
                    'assertions':c['assertions'],'runtime_seconds':c.get('seconds'),'output_count':len(c['outputs'])}
    add(extension,extension['qualification'],extension['tools'],lambda t:{EXTENDED_TOOLS[t]:EXTENDED_PACKAGES[EXTENDED_TOOLS[t]]},'extended-cpu-v1')
    add(extension,extension.get('core_qualification',{}),extension.get('refreshed_core_tools',[]),lambda t:{CORE_PACKAGE_TOOLS[t]:CORE_PACKAGES[CORE_PACKAGE_TOOLS[t]]},'core-cpu-refresh-v1')
    add(extension,extension.get('adjunct_qualification',{}),extension.get('adjunct_tools',[]),lambda t:{ADJUNCT_TOOLS[t]['name']:ADJUNCT_TOOLS[t]['version']},'extended-core-contracts-v1')
    add(extension,extension.get('portable_qualification',{}),extension.get('portable_tools',[]),lambda t:{'pysam':'0.24.1','python':'3.12'},'portable-refresh-v1')
    if secondary:add(secondary,secondary['qualification'],secondary['tools'],lambda t:ENVIRONMENTS[TOOLS[t]]['packages'],'secondary-cpu-v1')
    for filename, tool, family, positive_id in [
            ('deepvariant-release.json','deepvariant','deepvariant-cpu-v1','wgs-truth-control'),
            ('vep-release.json','vep','ensembl-vep-offline-v1','release-115-cache')]:
        path=Path('evidence/deployment')/filename
        if not path.exists():continue
        from backend import deepvariant_runtime,vep_runtime
        release=(deepvariant_runtime if tool=='deepvariant' else vep_runtime).validate_release(json.loads(path.read_text()))
        proof=release['qualification'];positive=next(c for c in proof['cases'] if c['id']==positive_id)
        verified[tool]={'status':'executed-small-control','image':release['image'],'source_sha256':release['source_sha256'],
            'runtime_family':family,'packages':{tool:release['version']},
            'scope':positive['assertions'].get('scope',proof.get('scope','Bounded original-cache annotation and known synthetic controls; not full-cache or clinical validation')),
            'assertions':positive['assertions'],'runtime_seconds':positive.get('seconds'),'output_count':len(positive['outputs']),
            'passed_controls':[c['id'] for c in proof['cases']],
            'required_dependencies':'Exact selected inputs and complete reference/cache bundles must independently pass live preflight'}
    formats_path=Path('evidence/deployment/portable-formats-release.json')
    formats=None
    if formats_path.exists():
        from backend.runtime_catalog import validate_format_release
        formats=validate_format_release(json.loads(formats_path.read_text()))
        for case in formats['qualification']['cases']:
            verified[case['tool']]={'status':'executed-small-control','image':formats['image'],
                'source_sha256':formats['source_sha256'],'runtime_family':formats['family'],
                'packages':formats['qualification']['versions'],'upstream_base_image':formats['base_image'],
                'scope':formats['qualification']['scope'],'assertions':case['assertions'],
                'runtime_seconds':case['seconds'],'output_count':len(case['outputs']),
                'required_dependencies':'Exact authorized inputs and compatible per-tier worker routes must independently pass live preflight'}
    rows=[]
    for node in sorted(BASE.values(),key=lambda n:n['id']):
        if node.get('hidden'):continue
        proof=verified.get(node['id'])
        if proof:status='implemented-small-control-verified';reason=proof['scope']
        elif node['license'] in ['Academic','Commercial BYOL','Paid API BYOK','Review required']:status='requires-entitlement-or-published-contract';reason=BLOCKERS.get(node['id'],node.get('rights_note','Exact credentials, permitted data and runtime must be verified'))
        else:status='awaiting-representative-runtime-validation';reason=BLOCKERS.get(node['id'],'Existing source/operator implementation remains subject to input dependencies and execution validation')
        if node['id'].startswith('resource.') or node['id'] in {'asset.file','reference.asset','database.asset'}:
            status='implemented-explicit-data-binding';reason='Binds an exact authorized asset; neither provisioning nor data availability is implied'
        rows.append({'id':node['id'],'name':node['name'],'status':status,'reason':reason,'license_category':node['license'],
            'inputs':node['inputs'],'outputs':node['outputs'],'registry_reference_image':node.get('image'),
            'runtime_evidence':proof,'required_reference_or_database_ports':[p for p in node['inputs'] if p['key'] in ['reference','database','known_sites','targets','chain','regions','stratifications']],
            'clinical_or_full_scale_validated':False,'documentation':node.get('docs')})
    cloud=Path('evidence/deployment/extended-cloud-validation.json');observed=json.loads(cloud.read_text()) if cloud.exists() else {}
    report={'schema':1,'public_catalogue_count':len(rows),'native_small_control_verified':sum(bool(r['runtime_evidence']) and not BASE[r['id']].get('builtin') for r in rows),
        'portable_refreshed_contracts':extension.get('portable_tools',[]),
        'portable_format_contracts':formats['tools'] if formats else [],'capabilities':rows,
        'cross_bucket_cloud_validation':{k:observed.get(k) for k in ['run_id','state','assertions','elapsed_seconds','scope']},
        'note':'Actual execution images/packages supersede illustrative registry tags. Qualified command contracts do not prove full assay accuracy or provision missing dependencies.'}
    raw=json.dumps(report,indent=2)
    Path('evidence/deployment/runtime-capability-matrix.json').write_text(raw)
    Path('docs/runtime-capabilities.json').write_text(raw)
    print(json.dumps({k:report[k] for k in ['public_catalogue_count','native_small_control_verified','portable_refreshed_contracts']}))

if __name__=='__main__':main()
