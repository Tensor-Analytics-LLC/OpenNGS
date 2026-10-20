"""Measured, bounded research operations shared by desktop and published cloud workers."""
import bisect
import collections
import csv
import gzip
import itertools
import json
import math
from pathlib import Path

TOOLS={'annotation.merge','contig.rename','contig.rename.bed','reference.dictionary','bam.sam','bam.cram','cram.bam','cram.index','vcf.bcf','bcf.vcf','gtf.bed','bed.gtf','vcf.scatter','vcf.gather','qc.gate','cohort.bind','vcf.ensemble','variant.guard','benchmark.exact','rna.fdr'}

def number(value):
    if isinstance(value,(tuple,list)):value=next((v for v in value if v is not None),None)
    try:
        n=float(value)
        return n if math.isfinite(n) else None
    except (ValueError,TypeError):return None

def wilson(success,total):
    if not total:return None
    z=1.959963984540054;p=success/total;den=1+z*z/total
    mid=(p+z*z/(2*total))/den;half=z*math.sqrt(p*(1-p)/total+z*z/(4*total*total))/den
    return [max(0,mid-half),min(1,mid+half)]

def bed_regions(path):
    regions=collections.defaultdict(list)
    opener=gzip.open if str(path).endswith('.gz') else open
    with opener(path,'rt') as f:
        for line in f:
            if not line.strip() or line.startswith(('#','track','browser')):continue
            a=line.rstrip().split('\t');start,end=int(a[1]),int(a[2])
            if start<0 or end<=start:raise ValueError('Invalid BED region')
            regions[a[0]].append((start,end))
    for chrom,rows in regions.items():
        merged=[]
        for start,end in sorted(rows):
            if merged and start<=merged[-1][1]:merged[-1]=(merged[-1][0],max(end,merged[-1][1]))
            else:merged.append((start,end))
        regions[chrom]=merged
    if not regions:raise ValueError('Confident regions must be explicit and nonempty')
    return regions

def execute(job):
    import pysam
    from .portable import artifact,report
    tool=job['tool'];p=job.get('params',{});inputs=job.get('inputs',{});work=Path(job['workdir'])
    def first(key):
        rows=inputs.get(key,[])
        if not rows:raise ValueError('Required input missing: '+key)
        return rows[0]
    def path(key):return first(key)['path']
    def meta(key):return {k:v for k,v in first(key).get('metadata',{}).items() if k in ['assembly','contig_style','reference_digest','public','sample_id']}
    if tool=='annotation.merge':
        annotations=collections.defaultdict(dict);sources=[]
        for item in inputs['reports']:
            if item['bytes']>8*1024**2:raise ValueError('Annotation report exceeds the bounded join size')
            value=item.get('metadata',{}).get('report') or json.loads(Path(item['path']).read_text());sources.append(item.get('digest'))
            if value.get('provider')=='OncoKB':
                for row in value.get('results',[]):
                    v=row['variant'];key=(v['chromosome'].removeprefix('chr'),int(v['position']),v['ref'],v['alt']);body=row['annotation'];level=body.get('highestSensitiveLevel') or body.get('highestResistanceLevel') or body.get('highestDiagnosticImplicationLevel') or body.get('highestPrognosticImplicationLevel')
                    if level:annotations[key]['OPENNGS_ONCOKB']=str(level)
            else:
                field=next((value for name,value in [('alphamissense','OPENNGS_AM'),('spliceai','OPENNGS_SPLICEAI'),('cadd','OPENNGS_CADD'),('revel','OPENNGS_REVEL')] if name in item['name'].lower()),None)
                if not field:raise ValueError('Unsupported report schema; publish an explicit allele-annotation adapter')
                for row in value.get('matches',[]):
                    lower={k.lower().lstrip('#'):v for k,v in row.items()};chrom=lower.get('contig') or lower.get('chrom') or lower.get('chr') or lower.get('chromosome');position=lower.get('position') or lower.get('pos') or lower.get('grch38_pos') or lower.get('hg19_pos');ref=lower.get('ref');alts=lower.get('alts') or [lower.get('alt')];score=number(row.get('selected_score'))
                    if not chrom or not position or not ref or score is None:raise ValueError('Report has no explicit allele key and numeric selected score')
                    for alt in alts:
                        if alt:annotations[(str(chrom).removeprefix('chr'),int(position),ref,alt)][field]=score
        out=work/'annotated.vcf';annotated=0;fields=['OPENNGS_AM','OPENNGS_SPLICEAI','OPENNGS_CADD','OPENNGS_REVEL','OPENNGS_ONCOKB']
        with pysam.VariantFile(path('variants')) as src:
            header=src.header.copy()
            for field in fields:
                if field in header.info:raise ValueError('Annotation field already exists; choose a new versioned annotation workflow')
                header.add_line(f'##INFO=<ID={field},Number=A,Type={"String" if field=="OPENNGS_ONCOKB" else "Float"},Description="Value joined from a bound source report; no independent clinical validation">')
            with pysam.VariantFile(str(out),'w',header=header) as dest:
                for rec in src:
                    rec.translate(header);keys=[(rec.contig.removeprefix('chr'),rec.pos,rec.ref,alt) for alt in rec.alts or []];found=False
                    for field in fields:
                        values=tuple(annotations[k].get(field) for k in keys)
                        if any(v is not None for v in values):rec.info[field]=values;found=True
                    annotated+=found;dest.write(rec)
        return {'variants':[artifact(out,'VCF',**meta('variants'),annotation_sources=sources)],'report':[report(work,'annotation-join.json',{'annotated_records':annotated,'source_report_digests':sources,'method':'Exact allele join; original annotations preserved','clinical_validation':False})]}
    if tool in ['bam.sam','bam.cram','cram.bam','cram.index']:
        src=path('alignment')
        if tool=='cram.index':
            out=work/'indexed.cram'
            import shutil
            shutil.copyfile(src,out);pysam.index(str(out))
            return {'alignment':[artifact(out,'INDEXED_CRAM',**meta('alignment'),index=str(out)+'.crai')]}
        fmt={'bam.sam':('w','sam','SAM'),'bam.cram':('wc','cram','CRAM'),'cram.bam':('wb','bam','BAM')}[tool]
        kw={'reference_filename':path('reference')} if 'cram' in tool else {}
        out=work/('converted.'+fmt[1])
        with pysam.AlignmentFile(src,'rc' if tool=='cram.bam' else 'rb',**kw) as source,pysam.AlignmentFile(str(out),fmt[0],template=source,**kw) as dest:
            for record in source:dest.write(record)
        return {'alignment':[artifact(out,fmt[2],**meta('alignment'))]}
    if tool=='reference.dictionary':
        import hashlib
        out=work/'reference.dict'
        with pysam.FastxFile(path('reference')) as src,out.open('w') as dest:
            dest.write('@HD\tVN:1.6\tSO:unsorted\n')
            for rec in src:dest.write(f'@SQ\tSN:{rec.name}\tLN:{len(rec.sequence)}\tM5:{hashlib.md5(rec.sequence.upper().encode()).hexdigest()}\n')
        return {'dictionary':[artifact(out,'REFERENCE_DICTIONARY',**meta('reference'))]}
    if tool in ['vcf.bcf','bcf.vcf']:
        out=work/('converted.bcf' if tool=='vcf.bcf' else 'converted.vcf')
        with pysam.VariantFile(path('variants')) as src,pysam.VariantFile(str(out),'wb' if tool=='vcf.bcf' else 'w',header=src.header) as dest:
            for rec in src:dest.write(rec)
        return {'variants':[artifact(out,'BCF' if tool=='vcf.bcf' else 'VCF',**meta('variants'))]}
    if tool in ['contig.rename','contig.rename.bed']:
        source=first('data');style=p.get('style','chr');mapping=json.loads(p.get('mapping','{}'))
        if not isinstance(mapping,dict) or any(not isinstance(k,str) or not isinstance(v,str) or not v or any(c.isspace() for c in v) for k,v in mapping.items()):raise ValueError('Contig mapping must be a JSON object of nonempty names')
        def rename(name):
            if style=='mapping':return mapping.get(name,name)
            if style=='numeric':return 'MT' if name in ['chrM','M','MT'] else name.removeprefix('chr')
            return 'chrM' if name in ['M','MT','chrM'] else name if name.startswith('chr') else 'chr'+name
        out=work/('renamed.bed' if source['format']=='BED' else 'renamed.vcf')
        if source['format']=='BED':
            seen={}
            with open(source['path']) as src,out.open('w') as dest:
                for line in src:
                    if not line.strip() or line.startswith(('#','track','browser')):continue
                    a=line.rstrip().split('\t');new=rename(a[0])
                    if new in seen and seen[new]!=a[0]:raise ValueError('Contig mapping creates a many-to-one collision')
                    seen[new]=a[0];a[0]=new;dest.write('\t'.join(a)+'\n')
        else:
            import re
            with pysam.VariantFile(source['path']) as src,out.open('w') as dest:
                names={c:rename(c) for c in src.header.contigs}
                if len(set(names.values()))!=len(names):raise ValueError('Contig mapping creates a many-to-one collision')
                for line in str(src.header).splitlines():
                    if line.startswith('##contig='):line=re.sub(r'(?<=ID=)[^,>]+',lambda m:rename(m[0]),line,count=1)
                    dest.write(line+'\n')
                for rec in src:
                    a=str(rec).rstrip().split('\t');a[0]=rename(a[0])
                    # Breakend mate contigs must be renamed as well.
                    a[4]=re.sub(r'([\[\]])([^\[\]:]+):(\d+)([\[\]])',lambda m:m[1]+rename(m[2])+':'+m[3]+m[4],a[4]);dest.write('\t'.join(a)+'\n')
        return {'data':[artifact(out,'BED' if source['format']=='BED' else 'VCF',**{**meta('data'),'contig_style':style},transformation='Contig names only; coordinates unchanged')]}
    if tool in ['gtf.bed','bed.gtf']:
        out=work/('features.bed' if tool=='gtf.bed' else 'intervals.gtf')
        opener=gzip.open if path('intervals').endswith('.gz') else open
        with opener(path('intervals'),'rt') as src,out.open('w') as dest:
            for i,line in enumerate(src):
                if not line.strip() or line.startswith(('#','track','browser')):continue
                a=line.rstrip().split('\t')
                if tool=='gtf.bed':
                    if len(a)<9:raise ValueError('GTF/GFF3 requires nine columns')
                    if p.get('feature') and a[2]!=p['feature']:continue
                    start,end=int(a[3]),int(a[4])
                    if start<1 or end<start:raise ValueError('Invalid feature interval')
                    dest.write(f'{a[0]}\t{start-1}\t{end}\t{a[2]}\t0\t{a[6]}\n')
                else:
                    start,end=int(a[1]),int(a[2])
                    if start<0 or end<=start:raise ValueError('Invalid BED interval')
                    dest.write(f'{a[0]}\tOpenNGS\texon\t{start+1}\t{end}\t.\t{a[5] if len(a)>5 else "."}\t.\tgene_id "interval_{i}"; transcript_id "interval_{i}";\n')
        return {'intervals':[artifact(out,'BED' if tool=='gtf.bed' else 'GTF',**meta('intervals'))]}
    if tool=='qc.gate':
        source=first('report');body=source.get('metadata',{}).get('report')
        if body is None:
            if Path(source['path']).stat().st_size>8*1024**2:raise ValueError('QC report exceeds JSON evaluation ceiling')
            body=json.loads(Path(source['path']).read_text())
        value=body
        for key in p.get('metric','').split('.'):
            try:value=value[int(key)] if isinstance(value,list) else value[key]
            except (ValueError,IndexError,KeyError,TypeError):raise ValueError('QC metric path is absent: '+p.get('metric',''))
        value=number(value)
        if value is None:raise ValueError('QC guard requires a finite numeric metric')
        threshold=p.get('threshold',80);op=p.get('operator','>=');ok={'>=':value>=threshold,'>':value>threshold,'<=':value<=threshold,'<':value<threshold}[op]
        decision=report(work,'qc-decision.json',{'metric':p['metric'],'value':value,'operator':op,'threshold':threshold,'passed':ok,'scope':'Assay-configured research guard'})
        if not ok and p.get('halt'):raise ValueError(f'QC guard failed: {value} {op} {threshold}')
        return {'passed':inputs['data'] if ok else [],'rejected':[] if ok else inputs['data'],'decision':[decision]}
    if tool=='cohort.bind':
        members=json.loads(p.get('members','[]'));rows=inputs['samples']
        if not isinstance(members,list) or len(members)!=len(rows):raise ValueError('Provide one explicit member per connected artifact')
        ids=[m.get('sample_id') for m in members]
        if any(not isinstance(x,str) or not x for x in ids) or len(set(ids))!=len(ids):raise ValueError('Sample IDs must be nonempty and distinct')
        indices=[m.get('artifact_index') for m in members]
        if set(indices)!=set(range(len(rows))):raise ValueError('Artifact indices must bind each input exactly once')
        for m in members:
            if m.get('role') not in ['mother','father','proband','tumor','normal','sample']:raise ValueError('Unsupported sample role')
            for field in ['mother','father']:
                if m.get(field) and (m[field] not in ids or m[field]==m['sample_id']):raise ValueError('Invalid pedigree parent')
        return {'cohort':rows,'manifest':[report(work,'sample-manifest.json',{'members':members,'input_digests':[r.get('digest') for r in rows],'kinship_validated':False})]}
    if tool=='rna.fdr':
        source=path('table');delimiter=',' if source.endswith('.csv') else '\t'
        with open(source) as f:
            reader=csv.DictReader(f,delimiter=delimiter);fields=reader.fieldnames or [];rows=list(itertools.islice(reader,1000001))
        if len(rows)>1000000 or p.get('p_column','pvalue') not in fields:raise ValueError('Missing p-value column or table exceeds one million rows')
        values=[number(r[p.get('p_column','pvalue')]) for r in rows]
        if any(v is None or not 0<=v<=1 for v in values):raise ValueError('All raw p-values must be finite and in [0,1]')
        order=sorted(range(len(rows)),key=lambda i:values[i]);adjusted=[1.]*len(rows);running=1.
        for rank in range(len(order),0,-1):
            i=order[rank-1];running=min(running,values[i]*len(rows)/rank);adjusted[i]=running
        out=work/'fdr-filtered.tsv';count=0
        with out.open('w') as f:
            writer=csv.DictWriter(f,fieldnames=[x for x in fields if x!='OpenNGS_BH_FDR']+['OpenNGS_BH_FDR'],delimiter='\t');writer.writeheader()
            for row,q in zip(rows,adjusted):
                if q<p.get('fdr',.05):writer.writerow({**row,'OpenNGS_BH_FDR':q});count+=1
        return {'table':[artifact(out,'TSV')],'report':[report(work,'fdr-summary.json',{'tested':len(rows),'retained':count,'strict_fdr_threshold':p.get('fdr',.05),'method':'Benjamini-Hochberg; supplied p-values, no model fitted'})]}
    if tool=='variant.guard':
        out=work/'guarded.vcf';counts=collections.Counter()
        with pysam.VariantFile(path('variants')) as src,pysam.VariantFile(str(out),'w',header=src.header) as dest:
            sample=p.get('sample') or next(iter(src.header.samples),None)
            if sample not in src.header.samples:raise ValueError('Selected sample is absent from VCF')
            for rec in src:
                counts['input']+=1;call=rec.samples[sample];af=number(call.get('AF'))
                ad=call.get('AD')
                if af is None and ad and all(x is not None for x in ad) and sum(ad)>0:af=sum(ad[1:])/sum(ad)
                values={'quality':rec.qual,'mq':number(rec.info.get('MQ')),'dp':number(call.get('DP')),'gq':number(call.get('GQ')),'vaf':af};reasons=[]
                for key,value in values.items():
                    threshold=p.get('minimum_'+key,{'quality':30,'mq':40,'dp':10,'gq':20,'vaf':0}[key])
                    if threshold<=0:continue
                    if value is None:reasons.append('missing_'+key)
                    elif value<threshold:reasons.append('low_'+key)
                if reasons:counts.update(reasons)
                else:dest.write(rec);counts['retained']+=1
        return {'variants':[artifact(out,'VCF',**meta('variants'))],'report':[report(work,'quality-guard.json',{'counts':dict(counts),'thresholds':p,'missing_metrics':'reject when threshold enabled','sample':sample})]}
    if tool=='vcf.scatter':
        outputs=[];handles={}
        try:
            with pysam.VariantFile(path('variants')) as src:
                for rec in src:
                    if rec.contig not in handles:
                        if len(handles)>=512:raise ValueError('Scatter exceeds 512 contigs')
                        out=work/f'contig-{len(handles)}.vcf';handles[rec.contig]=(out,pysam.VariantFile(str(out),'w',header=src.header))
                    handles[rec.contig][1].write(rec)
        finally:
            for out,handle in handles.values():handle.close()
        for chrom,(out,_) in handles.items():outputs.append(artifact(out,'VCF',**meta('variants'),contig=chrom))
        return {'shards':outputs}
    if tool in ['vcf.gather','vcf.ensemble']:
        rows=inputs['shards' if tool=='vcf.gather' else 'callsets'];header=None;samples=None;seen_chroms=set();records={};support=collections.defaultdict(set)
        assemblies={r.get('metadata',{}).get('assembly') for r in rows}-{None}
        if len(assemblies)>1:raise ValueError('Callsets have incompatible assemblies')
        if tool=='vcf.ensemble' and len({r['path'] for r in rows})!=len(rows):raise ValueError('Repeated bindings to the same file do not provide independent callset support')
        for caller,item in enumerate(rows):
            local_chroms=set()
            with pysam.VariantFile(item['path']) as src:
                if header is None:header=src.header.copy();samples=list(src.header.samples);schema=str(src.header)
                elif list(src.header.samples)!=samples:raise ValueError('Callsets require identical ordered samples')
                if tool=='vcf.gather' and str(src.header)!=schema:raise ValueError('Shard header schemas differ')
                for rec in src:
                    local_chroms.add(rec.contig)
                    if tool=='vcf.ensemble' and len(rec.alts or [])!=1:raise ValueError('Consensus requires normalized biallelic records')
                    key=(rec.contig,rec.pos,rec.ref,tuple(rec.alts or []));support[key].add(caller)
                    records.setdefault(key,str(rec))
                    if len(records)>2000000:raise ValueError('In-memory merge ceiling exceeded; use a native cohort tool')
            if tool=='vcf.gather' and seen_chroms&local_chroms:raise ValueError('Gather shards overlap in contig; use a reviewed variant merge instead')
            seen_chroms|=local_chroms
        if header is None:raise ValueError('No variant inputs')
        if tool=='vcf.ensemble':header.add_line('##INFO=<ID=OPENNGS_SUPPORT,Number=1,Type=Integer,Description="Distinct input callsets with an identical normalized allele">')
        out=work/'merged.vcf';retained=0;order={c:i for i,c in enumerate(header.contigs)}
        with out.open('w') as dest:
            dest.write(str(header))
            for key in sorted(records,key=lambda k:(order.get(k[0],999999),k[0],k[1],k[2],k[3])):
                if tool=='vcf.ensemble' and len(support[key])<p.get('minimum_callers',2):continue
                line=records[key]
                if tool=='vcf.ensemble':
                    a=line.rstrip().split('\t');a[7]=(a[7]+';' if a[7]!='.' else '')+'OPENNGS_SUPPORT='+str(len(support[key]));line='\t'.join(a)+'\n'
                dest.write(line);retained+=1
        result={'variants':[artifact(out,'VCF',assembly=next(iter(assemblies),None))]}
        if tool=='vcf.ensemble':result['report']=[report(work,'consensus.json',{'input_callsets':len(rows),'retained':retained,'method':'Exact allele support; genotype retained from first supporting file','haplotype_equivalence':False})]
        return result
    if tool=='benchmark.exact':
        regions=bed_regions(path('regions'));starts={c:[r[0] for r in rows] for c,rows in regions.items()}
        ceiling=p.get('maximum_alleles',2000000);excluded=collections.Counter()
        def alleles(source):
            values=set()
            with pysam.VariantFile(source) as src:
                for rec in src:
                    if rec.contig not in regions:excluded['outside_regions']+=1;continue
                    i=bisect.bisect_right(starts[rec.contig],rec.start)-1
                    if i<0 or not (regions[rec.contig][i][0]<=rec.start and rec.stop<=regions[rec.contig][i][1]):excluded['outside_regions']+=1;continue
                    if set(rec.filter)-{'PASS','.'}:excluded['filtered']+=1;continue
                    sample=p.get('sample') or next(iter(src.header.samples),None)
                    gt=rec.samples[sample].get('GT') if sample else None
                    if gt is not None and (not gt or any(x is None for x in gt)):excluded['nocall']+=1;continue
                    for j,alt in enumerate(rec.alts or [],1):
                        if gt is not None and j not in gt:continue
                        if any(c not in 'ACGT' for c in rec.ref+alt):excluded['unsupported_symbolic']+=1;continue
                        if len(rec.ref)!=1 and len(alt)!=1:raise ValueError('Exact benchmark requires decomposed, normalized small alleles. Use hap.py for complex representations.')
                        values.add((rec.contig,rec.pos,rec.ref,alt))
                        if len(values)>ceiling:raise ValueError('Allele comparison ceiling exceeded')
            return values
        truth=alleles(path('truth'));query=alleles(path('query'));metrics={}
        if not truth:raise ValueError('No supported truth alleles overlap the confident regions; verify assembly, sample and contig names')
        for category in ['SNV','INDEL']:
            kind=lambda k:('SNV' if len(k[2])==len(k[3])==1 else 'INDEL')==category
            t={k for k in truth if kind(k)};q={k for k in query if kind(k)};tp=len(t&q);fp=len(q-t);fn=len(t-q);precision=tp/len(q) if q else None;recall=tp/len(t) if t else None
            ti=sum((r+a) in ['AG','GA','CT','TC'] for _,_,r,a in q) if category=='SNV' else 0;tv=len(q)-ti if category=='SNV' else 0
            metrics[category]={'TP':tp,'FP':fp,'FN':fn,'precision':precision,'recall':recall,'f1':2*tp/(2*tp+fp+fn) if (2*tp+fp+fn) else None,'precision_wilson_95':wilson(tp,len(q)),'recall_wilson_95':wilson(tp,len(t)),'query_ti_tv':ti/tv if tv else None}
        return {'report':[report(work,'exact-benchmark.json',{'method':'Exact normalized allele presence; genotype concordance and haplotype equivalence not assessed','metrics':metrics,'excluded':dict(excluded),'region_bases':sum(b-a for rows in regions.values() for a,b in rows),'independent_validation':False,'interval_assumption':'Wilson intervals treat alleles as independent; linked variants violate this assumption. No statistical-significance or clinical-certification claim.','inputs':{k:first(k).get('digest') for k in ['query','truth','regions']}})]}
    raise ValueError('Unknown scientific operation: '+tool)
