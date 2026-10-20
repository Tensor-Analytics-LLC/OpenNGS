"""Branch-local build, contig and index contracts, independent of file-extension matching."""
import json
from . import db,registry

ALIASES={'hg19':'GRCh37','b37':'GRCh37','grch37':'GRCh37','hg38':'GRCh38','grch38':'GRCh38','chm13':'T2T-CHM13','t2t-chm13':'T2T-CHM13'}
def assembly(value):return ALIASES.get(str(value).lower(),value) if value else None

def check(graph,uid,specs,order):
    nodes={n['id']:n for n in graph['nodes']};descriptors={};issues=[];warnings=[]
    for nid in order:
        n=nodes[nid];p=n.get('params',{});spec=specs.get(n['tool'])
        if not spec:continue
        incoming=[(e,descriptors.get(e['source'],{}).get('ports',{}).get(e['sourcePort'],descriptors.get(e['source'],{}))) for e in graph['edges'] if e['target']==nid]
        known=[(e,d) for e,d in incoming if d.get('assembly')]
        is_transform=n['tool'] in ['liftover.vcf','crossmap.bed','mito.realign','mito.liftover']
        builds={assembly(d['assembly']) for _,d in known}
        if len(builds)>1 and not is_transform:
            issues.append({'code':'ASSEMBLY_MISMATCH','node_id':nid,'message':f'{spec["name"]} joins incompatible reference assemblies: '+', '.join(sorted(builds))+'. Bind the correct reference or an explicit chain/target liftover.','fix':None})
        styles={d.get('contig_style') for _,d in incoming}-{None,'unknown','mixed'}
        if len(styles)>1 and not n['tool'].startswith('contig.rename'):
            issues.append({'code':'CONTIG_MISMATCH','node_id':nid,'message':f'{spec["name"]} joins chr-prefixed and numeric contig namespaces. Add an explicit contig renamer after verifying reference identity.','fix':None})
        references={d.get('reference_digest') for _,d in incoming}-{None}
        if len(references)>1 and not is_transform:issues.append({'code':'REFERENCE_IDENTITY','node_id':nid,'message':'Bound reference sequence identities differ; matching assembly labels do not make sequences interchangeable.','fix':None})
        descriptor={'assembly':next(iter(builds),None),'contig_style':next(iter(styles),None),'reference_digest':next(iter(references),None)}
        if n['tool'] in ['asset.file','reference.asset','database.asset'] or spec.get('asset_family'):
            aid=p.get('asset_id');row=db.query('SELECT * FROM assets WHERE id=? AND user_id=?',(aid,uid),one=True) or db.query('SELECT * FROM artifacts WHERE id=? AND user_id=?',(aid,uid),one=True)
            if row:
                from .migration import resolve_asset
                row=resolve_asset(uid,row)
                meta=json.loads(row['metadata']);descriptor={k:meta.get(k) for k in ['assembly','contig_style','reference_digest']};descriptor['assembly']=assembly(row.get('assembly') or meta.get('assembly') or p.get('assembly'))
                if not meta.get('header_inspected'):warnings.append(spec['name']+': reference context includes declared/indexed metadata; inspect the actual header before scientific use.')
        if n['tool']=='sample.giab':descriptor={'assembly':'GRCh38','contig_style':'chr'}
        if n['tool'] in ['sample.fastq','sample.somatic','reference.tiny']:descriptor={'assembly':'engineering','contig_style':'custom'}
        if n['tool'].startswith('contig.rename'):descriptor['contig_style']=p.get('style','chr')
        if n['tool'] in ['liftover.vcf','crossmap.bed']:
            source=assembly(p.get('source_assembly'));target=assembly(p.get('target_assembly'))
            for e,d in known:
                expected=target if e['targetPort']=='reference' else source
                if assembly(d['assembly'])!=expected:issues.append({'code':'LIFTOVER_DIRECTION','node_id':nid,'message':f'{e["targetPort"]} build disagrees with the explicit source→target liftover direction.','fix':None})
            descriptor={'assembly':target,'contig_style':None,'reference_digest':None}
        if n['tool'] in ['mito.realign','mito.liftover']:
            descriptor=next((dict(d) for e,d in incoming if e['targetPort']=='reference'),{})
        if n['tool']=='mito.shift-reference':
            original={**descriptor,'reference_digest':None}
            descriptor={'ports':{'original':original,'shifted':{**original,'assembly':'mitochondrial-shifted'},'chain':{},'report':{}}}
        if n['tool'].startswith('reference.') and p.get('assembly'):descriptor['assembly']=assembly(p['assembly'])
        if p.get('assembly') and descriptor.get('assembly') and assembly(p['assembly'])!=descriptor['assembly']:
            issues.append({'code':'ANNOTATION_BUILD','node_id':nid,'message':'Node assembly parameter disagrees with its upstream data/reference build.','fix':None})
        descriptors[nid]=descriptor
    return issues,list(dict.fromkeys(warnings)),descriptors

def runtime(spec,inputs):
    for port in spec['inputs']:
        for item in inputs.get(port['key'],[]):
            if not registry.compatible(item['format'],port['type']) and 'COLLECTION' not in port['type'].split('|'):
                raise ValueError('Runtime input '+port['key']+' is '+item['format']+'; contract requires '+port['type'])
    # Recheck context after conditional/collection routing, whose static ANY ports cannot prove semantics.
    values=[item for rows in inputs.values() for item in rows]
    if spec['id'] not in ['liftover.vcf','crossmap.bed','mito.realign','mito.liftover']:
        builds={assembly(i.get('metadata',{}).get('assembly')) for i in values}-{None}
        if len(builds)>1:raise ValueError('Runtime artifacts have incompatible genome assemblies')
