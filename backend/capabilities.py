"""Evidence-scoped catalogue and dependency readiness; declarations are not support."""
import ast
import hashlib
import json
import os
from pathlib import Path
from . import db,registry
from .config import ROOT

VERIFIED_OPERATORS={'qc.summary','bam.sort','bam.index','bed.intervals','intervals.bed','mt.qc','variant.guard','benchmark.exact','rna.fdr','vcf.ensemble','qc.gate','vcf.scatter','vcf.gather','annotation.merge'}
VERIFIED_ARTIFACT_ROUTING={
    'utility.export': 'Publish the complete ordered list of admitted artifact records without changing URI, SHA-256, companion versions or data-rights metadata.',
    'utility.gather': 'Collect the complete ordered list of admitted artifact records without merging genomic records or changing their identities.',
    'utility.condition': 'Route an admitted artifact using inclusive minimum/maximum declared-byte bounds; a failing branch returns no artifact.',
}

def native_profiles():
    tree=ast.parse((ROOT/'backend/compiler.py').read_text());found=set()
    for node in ast.walk(tree):
        if isinstance(node,ast.Compare) and isinstance(node.left,ast.Name) and node.left.id=='tid':
            for value in node.comparators:
                if isinstance(value,ast.Constant) and isinstance(value.value,str):found.add(value.value)
                elif isinstance(value,(ast.List,ast.Tuple)):found.update(x.value for x in value.elts if isinstance(x,ast.Constant) and isinstance(x.value,str))
    return found

def matrix(uid=None):
    from .compiler import REQUIRES_PUBLISHED
    from .runtime_catalog import published,EXTENDED_TOOLS,EXTENDED_PACKAGES,ADJUNCT_TOOLS,BLOCKERS,CORE_PACKAGES,CORE_PACKAGE_TOOLS
    profiles=native_profiles()-REQUIRES_PUBLISHED;rows=[]
    for n in registry.catalogue(uid):
        if n.get('hidden'):continue
        third=n['license'] in ['Commercial BYOL','Paid API BYOK','Academic','Review required']
        executable=bool(n.get('builtin') or n['id'] in profiles or n.get('api'))
        if third:status='requires-entitlement';reason='Third-party rights, credentials or permitted database/model assets must be supplied and verified'
        elif not executable:status='unsupported';reason='Requires an explicitly published and validated vendor/tool wrapper; the generic catalogue declaration has no executable native profile'
        elif n['id'] in VERIFIED_ARTIFACT_ROUTING:status='implemented-verified';reason='Artifact-routing contract tests passed. No genomic file parsing, scientific transformation or clinical inference is performed.'
        elif n['id'] in VERIFIED_OPERATORS:status='implemented-verified';reason='Known-count or round-trip operator tests; representative full-scale clinical workflow validation is not established'
        else:status='awaiting-validation';reason='Executable implementation exists; representative dataset and runtime validation are pending'
        rows.append({'id':n['id'],'name':n['name'],'category':n['group'],'status':status,'reason':reason,'implementation':'portable' if n.get('builtin') else 'native-command' if n['id'] in profiles else 'provider-contract' if n.get('api') else None,'declared_image':n.get('image'),'verified_hosted_image':os.getenv('NGS_DEMO_IMAGE') if n['id'] in {'fastqc','fastp','minimap2','samtools','bcftools','bowtie2','hisat2','freebayes'} else None,'input_contracts':n['inputs'],'output_contracts':n['outputs'],'resources':n['resources'],'license':n['license'],'documentation':n.get('docs'),'validation_scope':'Research operator behavior; no clinical certification'})
    from .provisioning import CORE_TOOLS
    runtime=db.query("SELECT value FROM controls WHERE key='research-runtime-release'",one=True)
    runtime=json.loads(runtime['value']) if runtime else {}
    cloud_evidence={'fastqc':'run_eaaafb925caa4a4c9c09700859e1bc55','fastp':'run_e106a3c7ab27488bbdaa6d2a339f659e','minimap2':'run_c39ae6f2722746cfb7ce11a99d5fc853','freebayes':'run_c39ae6f2722746cfb7ce11a99d5fc853','bam.sort':'run_c39ae6f2722746cfb7ce11a99d5fc853','bam.index':'run_c39ae6f2722746cfb7ce11a99d5fc853'}
    cloud_evidence.update({t:'run_b16dc51e09ec4a13b8e5a49c53a69e54' for t in ['bwa-mem2','picard','gatk-bqsr','gatk-apply-bqsr','haplotypecaller','bcftools.annotate']})
    cloud_evidence.update({t:'run_cf41a3ae0fd64cd69afdd19e4e8f6d68' for t in ['wes.capture','wes.coverage']})
    cloud_evidence.update({t:'run_2680c67d051e48beaa975bdf8cda7c23' for t in ['mito.extract','mito.shift-reference','mito.realign','gatk.mt.call','gatk.mt.filter','mito.liftover','mito.reconcile','mito.regions','mt.qc']})
    for row in rows:
        row.pop('verified_hosted_image',None)
        core_sample=row['id'] in cloud_evidence and cloud_evidence[row['id']] in {'run_b16dc51e09ec4a13b8e5a49c53a69e54','run_cf41a3ae0fd64cd69afdd19e4e8f6d68','run_2680c67d051e48beaa975bdf8cda7c23'}
        row['hosted_runtime_image']=runtime.get('image') if row['id'] in CORE_TOOLS or core_sample else os.getenv('NGS_DEMO_IMAGE') if row['id'] in cloud_evidence else None
        row['runtime_status']=('Published digest; small-data execution recorded' if row['id'] in cloud_evidence else 'Published digest; build/version checks only') if row['hosted_runtime_image'] else 'Requires a reviewed publication or portable worker contract'
        row['sample_execution']=({'run_id':cloud_evidence[row['id']],'scope':'Small synthetic/public integration data; no full-scale or assay accuracy claim'} if row['id'] in cloud_evidence else None)
        if row['id'] in BLOCKERS:
            row['runtime_blocker']=BLOCKERS[row['id']]
    from .secondary_runtime import published as published_secondary,TOOLS as SECONDARY_TOOLS,ENVIRONMENTS
    secondary=published_secondary()
    secondary_cases={c['tool']:c for c in secondary['qualification']['cases']} if secondary else {}
    from .deepvariant_runtime import published as published_deepvariant
    deepvariant=published_deepvariant()
    from .vep_runtime import published as published_vep
    vep=published_vep()
    from .format_contract import SCOPE as FORMAT_SCOPE
    from .runtime_catalog import published_formats
    formats=published_formats()
    format_cases={c['tool']:c for c in formats['qualification']['cases']} if formats else {}
    extension=published()
    cases={x['tool']:x for x in extension['qualification']['cases']} if extension else {}
    core_cases={x['tool']:x for x in extension.get('core_qualification',{}).get('cases',[])} if extension else {}
    adjunct_cases={x['tool']:x for x in extension.get('adjunct_qualification',{}).get('cases',[])} if extension else {}
    portable_cases={c['tool']:c for c in extension.get('portable_qualification',{}).get('cases',[])} if extension else {}
    for row in rows:
        if row['id']=='deepvariant' and deepvariant:
            case=next(c for c in deepvariant['qualification']['cases'] if c['id']=='wgs-truth-control')
            row.update(hosted_runtime_image=deepvariant['image'],runtime_family=deepvariant['family'],
                runtime_package={'name':'DeepVariant','version':deepvariant['version'],'models':deepvariant['models'],
                    'upstream_image':deepvariant['upstream_image'],'model_files':deepvariant['qualification']['models']['WGS']},
                runtime_status='Published digest; WGS inference and incompatible-input rejection controls passed',runtime_blocker=None,
                sample_execution={'build_id':deepvariant['build_id'],'scope':deepvariant['validation_scope'],
                    'assertions':case['assertions'],'outputs':case['outputs'],'seconds':case['seconds']})
        if row['id'] in SECONDARY_TOOLS:
            case=secondary_cases.get(row['id'])
            if secondary and row['id'] in secondary['tools']:
                row.update(hosted_runtime_image=secondary['image'],runtime_family='secondary-cpu-v1',
                    runtime_package={'environment':SECONDARY_TOOLS[row['id']],'packages':ENVIRONMENTS[SECONDARY_TOOLS[row['id']]]['packages']},
                    runtime_status='Published digest; scientific command executed on engineering controls',runtime_blocker=None,
                    sample_execution={'build_id':secondary['build_id'],'scope':secondary['validation_scope'],
                        'assertions':case['assertions'],'outputs':case['outputs'],'seconds':case['seconds']})
            elif case:
                row.update(runtime_status='Optional runtime execution did not pass',runtime_blocker=case.get('error','Execution qualification incomplete'))
        if extension and row['id'] in extension.get('portable_tools',[]):
            case=portable_cases[row['id']]
            row.update(hosted_runtime_image=extension['image'],runtime_family='portable-refresh-v1',
                runtime_status='Published digest; portable operator round-trip regression passed',runtime_blocker=None,
                runtime_regression={'build_id':extension['portable_qualification']['build_id'],'assertions':case['assertions'],
                    'outputs':case['outputs'],'seconds':case['seconds'],'scope':extension['portable_qualification']['scope']})
            if not row.get('sample_execution'):row['sample_execution']=row['runtime_regression']
        if extension and row['id'] in extension.get('adjunct_tools',[]):
            case=adjunct_cases[row['id']]
            row.update(hosted_runtime_image=extension['image'],runtime_family='extended-core-contracts-v1',
                runtime_package=ADJUNCT_TOOLS[row['id']],runtime_status='Published digest; native command contract executed on engineering controls',
                runtime_blocker=None,sample_execution={'build_id':extension['adjunct_qualification']['build_id'],
                    'scope':extension['adjunct_qualification']['scope'],'assertions':case['assertions'],
                    'outputs':case['outputs'],'seconds':case['seconds']})
        if extension and row['id'] in extension.get('refreshed_core_tools',[]):
            case=core_cases[row['id']]
            row['hosted_runtime_image']=extension['image']
            row['runtime_family']='core-cpu-refresh-v1'
            package=CORE_PACKAGE_TOOLS[row['id']]
            row['runtime_package']={'name':package,'version':CORE_PACKAGES[package]}
            row['runtime_status']='Published digest; inherited native core command regression passed'
            row['runtime_regression']={'build_id':extension['core_qualification']['build_id'],
                'scope':extension['core_qualification']['scope'],'seconds':case['seconds'],
                'assertions':case['assertions'],'outputs':case['outputs']}
            if not row.get('sample_execution'):
                row['sample_execution']=row['runtime_regression']
        if row['id'] not in EXTENDED_TOOLS:continue
        package=EXTENDED_TOOLS[row['id']]
        row['runtime_family']='extended-cpu-v1'
        row['runtime_package']={'name':package,'version':EXTENDED_PACKAGES[package]}
        case=cases.get(row['id'])
        if extension and row['id'] in extension['tools']:
            row['hosted_runtime_image']=extension['image']
            row['runtime_status']='Published digest; scientific command executed on a bounded engineering dataset'
            row['sample_execution']={'build_id':extension['build_id'],'source_sha256':extension['source_sha256'],
                'scope':extension['validation_scope'],'assertions':case['assertions'],'outputs':case['outputs'],'seconds':case['seconds']}
            row['runtime_blocker']=None
        elif case:
            row['runtime_status']='Installed in reviewed image; execution qualification did not pass'
            row['runtime_blocker']=case.get('error') or case.get('reason')
        else:
            row['runtime_status']='Version-pinned recipe; build and execution qualification pending'
    refreshed=set(extension.get('tools',[])+extension.get('refreshed_core_tools',[])+extension.get('adjunct_tools',[])+extension.get('portable_tools',[])) if extension else set()
    if secondary:refreshed.update(secondary['tools'])
    if deepvariant:refreshed.update(deepvariant['tools'])
    if vep:refreshed.update(vep['tools'])
    if formats:refreshed.update(formats['tools'])
    metadata_only={'asset.file','database.asset','reference.asset','utility.export','utility.gather','utility.condition'}
    for row in rows:
        if formats and row['id'] in formats['tools']:
            case=format_cases[row['id']]
            proof={'build_id':formats['build_id'],'source_sha256':formats['source_sha256'],
                'image':formats['image'],'scope':FORMAT_SCOPE,'assertions':case['assertions'],
                'outputs':case['outputs'],'seconds':case.get('seconds')}
            row.update(status='implemented-verified',implementation='portable',
                reason='Published runtime passed known-record, coordinate, encoding and incompatible-input controls on tiny engineering data',
                validation_scope=FORMAT_SCOPE,hosted_runtime_image=formats['image'],runtime_family=formats['family'],
                runtime_package={'name':'OpenNGS format operators','version':formats['version'],
                    'base_image':formats['base_image'],'versions':formats['qualification']['versions']},
                runtime_status='Published digest; all eight bounded format execution controls passed',
                runtime_blocker=None,sample_execution=proof,runtime_regression=proof)
        if row['id']=='vep' and vep:
            case=next(c for c in vep['qualification']['cases'] if c['id']=='release-115-cache')
            row.update(hosted_runtime_image=vep['image'],runtime_family=vep['family'],
                runtime_package={'name':'Ensembl VEP','version':vep['version'],'cache_version':vep['cache_version'],
                    'assembly':vep['assembly'],'upstream_image':vep['upstream_image']},
                runtime_status='Published digest; 10 human control variants, spliced coding controls and three incompatible-cache rejection cases passed',
                runtime_blocker=None,
                sample_execution={'build_id':vep.get('build_id'),'source_sha256':vep['source_sha256'],
                    'scope':'Bounded engineering validation: 10 original release-115 human variants and independently known spliced consequences. Complete production cache availability, full-scale accuracy and clinical validation remain separate.',
                    'assertions':case['assertions'],'outputs':case['outputs'],'seconds':case.get('seconds')})
        if row['id'] in VERIFIED_ARTIFACT_ROUTING:
            row.update(
                execution_kind='artifact-routing', implementation='authenticated-orchestration',
                hosted_runtime_image=None,
                runtime_status='API artifact-routing contract; no scientific worker is required',
                validation_scope='Identity-preserving orchestration only; not a scientific validation claim',
                orchestration_contract={
                    'version': 1, 'scope': VERIFIED_ARTIFACT_ROUTING[row['id']],
                    'checks': ['tests/test_artifact_routing.py', 'tests/test_studio.py::test_real_quality_pipeline_and_idempotency'],
                    'input_authority': 'Runner checks tenant ownership, immutable identities and data rights before routing. Publication registers outputs under the admitted run.',
                })
        row['runtime_provenance']={'published_image':row.get('hosted_runtime_image'),'package':row.get('runtime_package'),
            'registry_reference_image':row.get('declared_image'),
            'authoritative':'Accepted run snapshots preserve the exact published image and environment. Registry tags are upstream references, not proof of the version executed.'}
        if row['id'] in VERIFIED_ARTIFACT_ROUTING:
            row['runtime_provenance']['authoritative']='The deployed API routes admitted artifact metadata; no scientific container or genomic transformation is involved.'
        if row['id'] in refreshed:
            row['migrated_data_runtime']={'state':'qualified','source_grants_version':1,'reason':'A reviewed worker accepts only exact tenant-owned immutable migrated objects; tenant capacity and dependencies must also be ready'}
        elif row['id'] in metadata_only or row['id'].startswith(('sample.','resource.')) or row['id']=='reference.tiny':
            row['migrated_data_runtime']={'state':'not-applicable','reason':'Source or metadata-only node; downstream execution remains independently gated'}
        else:
            row['migrated_data_runtime']={'state':'requires-refreshed-runtime','reason':'Existing same-bucket behavior is unchanged. Migrated private inputs require a separately qualified source-grant worker route; admission defers this combination before AWS execution'}
    return {'schema':2,'capabilities':rows,'counts':{s:sum(x['status']==s for x in rows) for s in ['implemented-verified','awaiting-validation','requires-entitlement','unsupported']},'note':'Image pull/build success is not scientific validation. Readiness requires all exact dependencies and a published execution profile.'}

def dependencies():
    rows=[]
    definitions=[('grch38-core','GRCh38','FASTA, FAI, sequence dictionary, aligner index','exact assembly/contig convention and digest'),('grch37-core','GRCh37','FASTA, FAI, sequence dictionary, aligner index','exact release and contig convention'),('t2t-core','T2T-CHM13','v2.0 FASTA, FAI, dictionary, matching indices','T2T coordinates cannot substitute for GRCh38'),('gatk-bqsr','GRCh38','known-sites VCFs, tabix, matching reference dictionary','per-release dbSNP/Mills/indel resources'),('vep-cache','GRCh38','Ensembl VEP executable/cache/FASTA release bundle','match VEP and cache versions; permitted terms'),('wes-capture','assay-specific','capture-kit manufacturer/version, target BED, padded intervals and coverage definition','must be supplied per kit; not inferred from a WGS template'),('mitochondrial','GRCh38/rCRS','exact whole-genome nuclear+mitochondrial reference, shifted mt reference, original-to-shifted coordinate map','NUMT-aware alignment and mitochondrial caller/filter profile required'),('giab-full','GRCh38','query reads, complete HG002 truth VCF, confident BED, stratifications and exact reference','same individual and release throughout'),('licensed-annotations','release-specific','HGMD/COSMIC and other restricted databases','vendor authorization and exact release')]
    definitions[0:0]=[('grch38-fasta','GRCh38 / Broad hg38/v0','FASTA + FAI + sequence dictionary','Verified AWS component; does not include an aligner index'),('giab-truth-hg002-v4.2.1','GRCh38','HG002 v4.2.1 VCF + TBI + confident BED','Verified AWS component; matching reads and stratifications remain required')]
    for ident,assembly,files,reason in definitions:
        record=db.query('SELECT value FROM controls WHERE key=?',('reference-bundle:'+ident,),one=True)
        observed=json.loads(record['value']) if record else {}
        state=observed.get('state','Requires entitlement' if ident=='licensed-annotations' else 'Unavailable')
        if ident=='vep-cache':
            from .provisioning import validate_vep_bundle
            try:verified=validate_vep_bundle(observed)
            except (ValueError,TypeError,KeyError):verified=None
            state='Ready' if verified else 'Unavailable'
            reason='Complete original release-115 cache archive with immutable cloud version, all original members, and matching verified GRCh38 FASTA/FAI required. Bounded runtime controls do not establish production bundle readiness.'
            observed=({'owner_id':verified['owner_id'],'cache_version':verified['cache_version'],'assembly':verified['assembly'],
                'member_manifest_sha256':verified['complete_cache']['member_manifest_sha256'],
                'complete_members':verified['complete_cache']['members'],'image':verified['image'],
                'scope':'Private owner-only complete cloud bundle; no full-assay accuracy claim'} if verified else {})
        rows.append({'id':ident,'assembly':assembly,'requirements':files,'state':state,'reason':reason if not observed else observed.get('reason',reason),'manifest':observed or None})
    manifest=ROOT/'demo-data/manifest.json'
    proof=db.query("SELECT value FROM controls WHERE key='reference-bundle:curated-samples-v1'",one=True)
    proof=json.loads(proof['value']) if proof else {}
    sha=hashlib.sha256(manifest.read_bytes()).hexdigest() if manifest.exists() else None
    ready=proof.get('image')==os.getenv('NGS_DEMO_IMAGE') and proof.get('manifest_sha256')==sha and bool(proof)
    rows.insert(0,{'id':'curated-samples-v1','assembly':'mixed; explicit per source','state':'Ready' if ready else 'Provisioning','requirements':'Seven immutable source nodes and companion files inside the pinned cloud image','manifest_sha256':sha,'image':os.getenv('NGS_DEMO_IMAGE'),'bytes':sum(x['bytes'] for x in json.loads(manifest.read_text())['files']) if manifest.exists() else 0,'reason':'Small engineering/public slices; no full-size WGS/WES/mtDNA readiness claim','validation':proof})
    return rows

def workflow_status():
    return [{'id':'wgs','status':'blocked','reason':'Full reference/index, validated germline caller and annotation bundles have not been provisioned or tested at WGS scale','clinical_validation':False},
      {'id':'wes','status':'blocked','reason':'Requires exact capture-kit metadata/targets, target coverage and caller/annotation validation','clinical_validation':False},
      {'id':'mtdna','status':'blocked','reason':'Allele-fraction QC is implemented; shifted-reference caller/filter, liftover and reconciliation are implemented; representative heteroplasmy/NUMT performance is not validated','clinical_validation':False}]
