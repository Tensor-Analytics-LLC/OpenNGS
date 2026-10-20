"""Evidence-scoped catalogue and dependency readiness; declarations are not support."""
import ast
import hashlib
import json
import os
from pathlib import Path
from . import db,registry
from .config import ROOT

VERIFIED_OPERATORS={'qc.summary','bam.sort','bam.index','bed.intervals','intervals.bed','mt.qc','variant.guard','benchmark.exact','rna.fdr','vcf.ensemble','qc.gate','vcf.scatter','vcf.gather','annotation.merge'}

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
    profiles=native_profiles()-REQUIRES_PUBLISHED;rows=[]
    for n in registry.catalogue(uid):
        third=n['license'] in ['Commercial BYOL','Paid API BYOK','Academic','Review required']
        executable=bool(n.get('builtin') or n['id'] in profiles or n.get('api'))
        if third:status='requires-entitlement';reason='Third-party rights, credentials or permitted database/model assets must be supplied and verified'
        elif not executable:status='unsupported';reason='Requires an explicitly published and validated vendor/tool wrapper; the generic catalogue declaration has no executable native profile'
        elif n['id'] in VERIFIED_OPERATORS:status='implemented-verified';reason='Known-count or round-trip operator tests; representative full-scale clinical workflow validation is not established'
        else:status='awaiting-validation';reason='Executable implementation exists; representative dataset and runtime validation are pending'
        rows.append({'id':n['id'],'name':n['name'],'category':n['group'],'status':status,'reason':reason,'implementation':'portable' if n.get('builtin') else 'native-command' if n['id'] in profiles else 'provider-contract' if n.get('api') else None,'declared_image':n.get('image'),'verified_hosted_image':os.getenv('NGS_DEMO_IMAGE') if n['id'] in {'fastqc','fastp','minimap2','samtools','bcftools','bowtie2','hisat2','freebayes'} else None,'input_contracts':n['inputs'],'output_contracts':n['outputs'],'resources':n['resources'],'license':n['license'],'documentation':n.get('docs'),'validation_scope':'Research operator behavior; no clinical certification'})
    return {'schema':1,'capabilities':rows,'counts':{s:sum(x['status']==s for x in rows) for s in ['implemented-verified','awaiting-validation','requires-entitlement','unsupported']},'note':'Image pull/build success is not scientific validation. Readiness requires all exact dependencies and a published execution profile.'}

def dependencies():
    rows=[]
    definitions=[('grch38-core','GRCh38','FASTA, FAI, sequence dictionary, aligner index','exact assembly/contig convention and digest'),('grch37-core','GRCh37','FASTA, FAI, sequence dictionary, aligner index','exact release and contig convention'),('t2t-core','T2T-CHM13','v2.0 FASTA, FAI, dictionary, matching indices','T2T coordinates cannot substitute for GRCh38'),('gatk-bqsr','GRCh38','known-sites VCFs, tabix, matching reference dictionary','per-release dbSNP/Mills/indel resources'),('vep-cache','GRCh38','Ensembl VEP executable/cache/FASTA release bundle','match VEP and cache versions; permitted terms'),('wes-capture','assay-specific','capture-kit manufacturer/version, target BED, padded intervals and coverage definition','must be supplied per kit; not inferred from a WGS template'),('mitochondrial','GRCh38/rCRS','exact whole-genome nuclear+mitochondrial reference, shifted mt reference, original-to-shifted coordinate map','NUMT-aware alignment and mitochondrial caller/filter profile required'),('giab-full','GRCh38','query reads, complete HG002 truth VCF, confident BED, stratifications and exact reference','same individual and release throughout'),('licensed-annotations','release-specific','HGMD/COSMIC and other restricted databases','vendor authorization and exact release')]
    for ident,assembly,files,reason in definitions:
        record=db.query('SELECT value FROM controls WHERE key=?',('reference-bundle:'+ident,),one=True)
        observed=json.loads(record['value']) if record else {}
        state=observed.get('state','Requires entitlement' if ident=='licensed-annotations' else 'Unavailable')
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
      {'id':'mtdna','status':'blocked','reason':'Allele-fraction QC is implemented; complete NUMT-aware, shifted-reference heteroplasmy calling is not validated','clinical_validation':False}]
