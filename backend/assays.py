"""Assay-specific interval, coverage and mitochondrial coordinate operators."""
import collections,hashlib,json
from pathlib import Path

TOOLS={'wes.capture','wes.coverage','mito.shift-reference','mito.reconcile','mito.regions'}

def execute(job):
    import pysam
    from .portable import artifact,report,sha
    from .scientific import bed_regions
    work=Path(job['workdir']);work.mkdir(parents=True,exist_ok=True);p=job.get('params',{});inputs=job.get('inputs',{});tool=job['tool']
    def first(key):
        if not inputs.get(key):raise ValueError('Required assay input missing: '+key)
        return inputs[key][0]
    def fasta():
        ref=first('reference');return pysam.FastaFile(ref['path'],filepath_index=ref.get('metadata',{}).get('index'))
    if tool=='mito.regions':
        contig=p.get('contig','chrM');regions=bed_regions(first('regions')['path'])
        if not any(chrom!=contig for chrom in regions):raise ValueError('The NUMT-aware profile requires an explicitly reviewed nuclear candidate interval set')
        with fasta() as ref:
            lengths=dict(zip(ref.references,ref.lengths))
            if contig not in lengths:raise ValueError('Mitochondrial contig is absent from the exact genome')
            for chrom,rows in regions.items():
                if chrom not in lengths or any(b>lengths[chrom] for a,b in rows):raise ValueError('Candidate interval does not fit the exact reference')
            regions[contig]=[(0,lengths[contig])];out=work/'mitochondrial-numt-candidates.bed'
            with out.open('w') as dest:
                for chrom in ref.references:
                    for a,b in regions.get(chrom,[]):dest.write(f'{chrom}\t{a}\t{b}\n')
        return {'regions':[artifact(out,'BED',assembly=first('reference').get('metadata',{}).get('assembly'),source_regions_sha256=sha(first('regions')['path']))],'report':[report(work,'mitochondrial-candidate-regions.json',{'contig':contig,'source_sha256':sha(first('regions')['path']),'includes_complete_mitochondrial_contig':True,'nuclear_regions':sum(len(rows) for chrom,rows in regions.items() if chrom!=contig),'method':'Explicit source intervals plus the complete mitochondrial contig; no inferred NUMT catalogue','clinical_validation':False})]}
    if tool=='wes.capture':
        if not p.get('kit_name','').strip() or not p.get('kit_version','').strip():raise ValueError('Specify the capture-kit name and exact version')
        regions=bed_regions(first('targets')['path']);padding=int(p.get('padding_bp',100))
        if not 0<=padding<=10000:raise ValueError('Capture padding must be 0–10000 bases')
        with fasta() as ref:
            lengths=dict(zip(ref.references,ref.lengths));padded=collections.defaultdict(list)
            for chrom,rows in regions.items():
                if chrom not in lengths:raise ValueError('Capture contig is absent from the exact reference: '+chrom)
                for a,b in rows:
                    if b>lengths[chrom]:raise ValueError('Capture interval exceeds the reference length')
                    padded[chrom].append((max(0,a-padding),min(lengths[chrom],b+padding)))
            outputs=[]
            for filename,mapping in [('targets.bed',regions),('calling-padded.bed',padded)]:
                target=work/filename
                with target.open('w') as out:
                    for chrom in ref.references:
                        merged=[]
                        for a,b in sorted(mapping.get(chrom,[])):
                            if merged and a<=merged[-1][1]:merged[-1]=(merged[-1][0],max(b,merged[-1][1]))
                            else:merged.append((a,b))
                        for a,b in merged:out.write(f'{chrom}\t{a}\t{b}\n')
                outputs.append(target)
        context={k:v for k,v in first('reference').get('metadata',{}).items() if k in ['assembly','contig_style','reference_digest']}
        meta={**context,'capture_kit':p['kit_name'],'capture_kit_version':p['kit_version'],'source_targets_sha256':sha(first('targets')['path']),'padding_bp':padding}
        return {'targets':[artifact(outputs[0],'BED',**meta)],'calling':[artifact(outputs[1],'BED',**meta)],'report':[report(work,'capture-manifest.json',{'kit':p['kit_name'],'version':p['kit_version'],'padding_bp':padding,'target_bases':sum(b-a for rows in regions.values() for a,b in rows),'targets_sha256':sha(outputs[0]),'calling_sha256':sha(outputs[1]),'coordinate_system':'zero-based half-open BED','clinical_validation':False})]}
    if tool=='wes.coverage':
        regions=bed_regions(first('targets')['path']);item=first('alignment');meta=item.get('metadata',{});total=depth_sum=0;thresholds={1:0,10:0,20:0,30:0};per_target=[]
        minimum_bq=int(p.get('minimum_base_quality',20));minimum_mq=int(p.get('minimum_mapping_quality',20))
        def keep(read):return not (read.is_unmapped or read.is_duplicate or read.is_secondary or read.is_supplementary or read.is_qcfail) and read.mapping_quality>=minimum_mq
        with pysam.AlignmentFile(item['path'],'rb',index_filename=meta.get('index')) as bam:
            for chrom,rows in regions.items():
                if chrom not in bam.references:raise ValueError('Capture contig is absent from the alignment: '+chrom)
                for a,b in rows:
                    if b>bam.get_reference_length(chrom):raise ValueError('Target exceeds alignment reference length')
                    target_sum=0
                    for start in range(a,b,100000):
                        arrays=bam.count_coverage(chrom,start,min(b,start+100000),quality_threshold=minimum_bq,read_callback=keep)
                        for values in zip(*arrays):
                            depth=sum(values);target_sum+=depth
                            for threshold in thresholds:thresholds[threshold]+=depth>=threshold
                    total+=b-a;depth_sum+=target_sum
                    per_target.append((chrom,a,b,target_sum/(b-a)))
        table=work/'target-coverage.tsv'
        with table.open('w') as out:
            out.write('chrom\tstart\tend\tmean_depth\n')
            for chrom,a,b,mean in per_target:out.write(f'{chrom}\t{a}\t{b}\t{mean:.6f}\n')
        value={'method':'Full target-base A/C/G/T coverage after declared read/base filters','target_bases':total,'mean_depth':depth_sum/max(1,total),'fraction_at_depth':{str(k):v/max(1,total) for k,v in thresholds.items()},'minimum_base_quality':minimum_bq,'minimum_mapping_quality':minimum_mq,'excluded_reads':'unmapped,duplicate,secondary,supplementary,QC-fail','overlapping_mates':'Counted as reads; not fragment-deduplicated coverage','capture_kit':first('targets').get('metadata',{}).get('capture_kit'),'targets_sha256':sha(first('targets')['path']),'not_picard_hs_metrics':True,'clinical_validation':False}
        return {'report':[report(work,'capture-coverage.json',value)],'table':[artifact(table,'TSV')]}
    if tool=='mito.shift-reference':
        contig=p.get('contig','chrM');shift=int(p.get('shift_bases',8000))
        with fasta() as ref:
            if contig not in ref.references:raise ValueError('Mitochondrial contig is absent from the exact whole-genome reference')
            sequence=ref.fetch(contig).upper()
        length=len(sequence)
        if not 0<shift<length:raise ValueError('Reference shift must be strictly inside the mitochondrial sequence length')
        outputs={}
        for port,name,seq in [('original','mitochondrial.fa',sequence),('shifted','mitochondrial-shifted.fa',sequence[shift:]+sequence[:shift])]:
            path=work/name;path.write_text('>'+contig+'\n'+'\n'.join(seq[i:i+80] for i in range(0,length,80))+'\n');pysam.faidx(str(path));dictionary=path.with_suffix('.dict');dictionary.write_text(f'@HD\tVN:1.6\tSO:unsorted\n@SQ\tSN:{contig}\tLN:{length}\tM5:{hashlib.md5(seq.encode()).hexdigest()}\n')
            outputs[port]=[artifact(path,'FASTA',index=str(path)+'.fai',index_sha256=sha(str(path)+'.fai'),index_paths=[str(path)+'.fai',str(dictionary)],circular=True,shift_bases=shift if port=='shifted' else 0,reference_digest='md5:'+hashlib.md5(seq.encode()).hexdigest(),assembly='mitochondrial-shifted' if port=='shifted' else first('reference').get('metadata',{}).get('assembly','declared'),contig_style='chr' if contig.startswith('chr') else 'custom')]
        chain=work/'shifted-to-original.chain';chain.write_text(f'chain 1 {contig} {length} + {shift} {length} {contig} {length} + 0 {length-shift} 1\n{length-shift}\n\nchain 1 {contig} {length} + 0 {shift} {contig} {length} + {length-shift} {length} 2\n{shift}\n\n')
        outputs['chain']=[artifact(chain,'CHAIN',source='mitochondrial-shifted',target=first('reference').get('metadata',{}).get('assembly'),shift_bases=shift)]
        outputs['report']=[report(work,'mitochondrial-reference.json',{'contig':contig,'length':length,'shift_bases':shift,'coordinate_map':'original_0based = (shifted_0based + shift_bases) mod length','whole_genome_reference_sha256':first('reference').get('digest'),'NUMT_warning':'Use exact whole-genome alignment and explicit nuclear candidate intervals; mitochondrial-only alignment cannot establish absence of NUMT artifacts','clinical_validation':False})]
        return outputs
    if tool=='mito.reconcile':
        window=int(p.get('boundary_bases',150));contig=p.get('contig','chrM');records=[];counts=collections.Counter()
        with fasta() as ref:
            length=ref.get_reference_length(contig)
            if not 1<=window<length//2:raise ValueError('Invalid circular-boundary window')
            with pysam.VariantFile(first('original')['path']) as original,pysam.VariantFile(first('shifted_back')['path']) as rotated:
                if list(original.header.samples)!=list(rotated.header.samples):raise ValueError('Mitochondrial callset samples do not match')
                header=original.header.copy()
                for name,field in rotated.header.info.items():
                    if name in header.info and (header.info[name].type!=field.type or header.info[name].number!=field.number):raise ValueError('Incompatible INFO schema')
                    if name not in header.info:header.add_meta('INFO',items=[('ID',name),('Number',field.number),('Type',field.type),('Description',field.description)])
                for name,field in rotated.header.formats.items():
                    if name in header.formats and (header.formats[name].type!=field.type or header.formats[name].number!=field.number):raise ValueError('Incompatible FORMAT schema')
                header.merge(rotated.header)
                for handle,is_rotated in [(original,False),(rotated,True)]:
                    for record in handle:
                        if record.contig!=contig:raise ValueError('Unexpected contig in mitochondrial callset')
                        if record.ref.upper()!=ref.fetch(contig,record.start,record.stop).upper():raise ValueError('Mitochondrial REF allele does not match the original reference')
                        boundary=record.start<window or record.stop>length-window
                        if boundary!=is_rotated:counts['excluded_overlapping_source']+=1;continue
                        copy=record.copy();copy.translate(header);records.append(copy);counts['shifted_boundary' if is_rotated else 'original_interior']+=1
                out=work/'mitochondrial-reconciled.vcf'
                with pysam.VariantFile(str(out),'w',header=header) as dest:
                    for record in sorted(records,key=lambda x:(x.start,x.ref,x.alts or ())):dest.write(record)
        return {'variants':[artifact(out,'VCF',**{k:v for k,v in first('reference').get('metadata',{}).items() if k in ['assembly','reference_digest']},mitochondrial=True)],'report':[report(work,'circular-callset-reconciliation.json',{'boundary_bases':window,'counts':dict(counts),'method':'Filtered original-reference calls in interior; separately filtered, explicitly lifted calls at circular boundaries. REF alleles checked against the original reference.','limitation':'Not a NUMT classifier, contamination model, haplogroup inference or clinical heteroplasmy validation','clinical_validation':False})]}
    raise ValueError('Unsupported assay operation')
