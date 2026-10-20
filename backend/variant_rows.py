"""Allele-aware normalization shared by isolated and trusted fixture readers."""
from .scientific import number
from . import db

def text(value):
    if isinstance(value,(list,tuple)):return '|'.join(str(v) for v in value if v is not None)
    return '' if value is None else str(value)

def allele_value(value,i):
    if isinstance(value,(list,tuple)):return value[min(i,len(value)-1)] if value else None
    return value

def annotation_schema(src):
    schema={}
    for key in ['CSQ','ANN']:
        if key in src.header.info:
            description=src.header.info[key].description
            if 'Format:' in description:schema[key]=[x.strip().strip("'\"") for x in description.split('Format:')[-1].strip().split('|')]
            elif key=='ANN':schema[key]=['Allele','Annotation','Annotation_Impact','Gene_Name','Gene_ID','Feature_Type','Feature_ID','Transcript_BioType','Rank','HGVS.c','HGVS.p']
    return schema

def rows(src):
    schema=annotation_schema(src)
    for rec in src:
        info={k:list(v) if isinstance(v,tuple) else v for k,v in rec.info.items()};call=next(iter(rec.samples.values()),{});gt=call.get('GT');zygosity='unknown'
        if gt and all(v is not None for v in gt):zygosity='hom_ref' if all(v==0 for v in gt) else 'hom_alt' if len(set(gt))==1 else 'het'
        for i,alt in enumerate(rec.alts or []):
            allele_zygosity=zygosity
            if gt and all(v is not None for v in gt):allele_zygosity='hom_ref' if i+1 not in gt else 'hemi_alt' if len(gt)==1 else 'hom_alt' if all(v==i+1 for v in gt) else 'het'
            annotations=[]
            for key,names in schema.items():
                entries=info.get(key,[]);entries=entries if isinstance(entries,list) else [entries]
                annotations.extend(dict(zip(names,v.split('|'))) for v in entries if v and v.split('|')[0] in [alt,alt.removeprefix(rec.ref[0]) or '-',str(i+1)])
            def find(*names):
                for name in names:
                    if name in info:
                        cardinality=src.header.info[name].number
                        return allele_value(info[name],i if cardinality=='A' else i+1) if cardinality in ['A','R'] else info[name]
                    for annotation in annotations:
                        if annotation.get(name) not in [None,'']:return annotation[name]
                return None
            clinical=text(find('CLNSIG','ClinVar','CLIN_SIG'));classification=text(find('ACMG','ACMG_CLASS','CLASSIFICATION'))
            if not classification:
                # A ClinVar label is displayed with provenance, not promoted to a new ACMG assessment.
                mapping={'Pathogenic':'P','Likely_pathogenic':'LP','Pathogenic/Likely_pathogenic':'P/LP','Uncertain_significance':'VUS','Likely_benign':'LB','Benign':'B','Benign/Likely_benign':'B/LB'}
                classification=mapping.get(clinical,'')
            ad=call.get('AD');af=number(allele_value(call.get('AF'),i))
            if af is None and ad and len(ad)>i+1 and all(v is not None for v in ad) and sum(ad)>0:af=ad[i+1]/sum(ad)
            splice=number(find('OPENNGS_SPLICEAI','SpliceAI_DS','SpliceAI_score','spliceai'))
            if splice is None and info.get('SpliceAI'):
                values=info['SpliceAI'] if isinstance(info['SpliceAI'],list) else [info['SpliceAI']]
                scores=[number(s) for v in values if v.split('|')[0]==alt for s in v.split('|')[2:6]];scores=[s for s in scores if s is not None];splice=max(scores) if scores else None
            gene=text(find('SYMBOL','Gene_Name','GENE','Gene'))
            if not gene and info.get('GENEINFO'):gene='|'.join(v.split(':')[0] for v in text(info['GENEINFO']).split('|'))
            consequence=text(find('Consequence','Annotation','CONSEQUENCE'))
            if not consequence and info.get('MC'):
                items=info['MC'] if isinstance(info['MC'],list) else [info['MC']];consequence='|'.join(v.split('|')[-1] for v in items)
            values=[rec.contig,rec.pos,rec.ref,alt,gene,consequence,clinical,classification,number(find('gnomAD_AF','gnomADg_AF','gnomADe_AF')),number(find('OPENNGS_CADD','CADD_PHRED','CADD')),number(find('OPENNGS_REVEL','REVEL','REVEL_score')),number(find('OPENNGS_AM','am_pathogenicity','AlphaMissense','AlphaMissense_score')),splice,number(call.get('DP')),number(call.get('GQ')),af,rec.qual,allele_zygosity,text(find('INHERITANCE','Inheritance')),text(find('OPENNGS_ONCOKB','ONCOKB','ONCOKB_LEVEL')),number(find('COSMIC_COUNT')),text(find('HPO','HPO_TERMS')),db.encode({'INFO':info,'sample':next(iter(rec.samples),None),'genotype':list(gt) if gt else None,'annotation_source':'VCF INFO/CSQ/ANN; no inferred pathogenicity','annotations':annotations}),str(rec)]
            yield values
