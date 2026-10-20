"""Compile published tools into argv. No user input becomes a host shell."""
import json
import shlex
from pathlib import Path

def compile_tool(spec,node,inputs,work):
    p={x['key']:x['default'] for x in spec['parameters']};p.update(node.get('params',{}))
    threads=str(node.get('resources',{}).get('threads',2));advanced=shlex.split(p.get('extra',''))+shlex.split(node.get('advanced',''))
    bindings=[];mapped={}
    for key,items in inputs.items():
        mapped[key]=[]
        for i,item in enumerate(items):
            source=item['path'];target=f"/inputs/{key}/{i}/{source.rsplit('/',1)[-1]}"
            bindings.append((source,target));record={'path':target,**item.get('metadata',{})}
            for field in ['mate','index','dictionary']:
                if record.get(field):
                    src=record[field];dst=f"/inputs/{key}/{i}/{src.rsplit('/',1)[-1]}";bindings.append((src,dst));record[field]=dst
            for idx in item.get('metadata',{}).get('index_paths',[]):
                bindings.append((idx,f"/inputs/{key}/{i}/{idx.rsplit('/',1)[-1]}"))
            mapped[key].append(record)
    def item(key):
        if not mapped.get(key):raise ValueError('Missing input '+key)
        return mapped[key][0]
    def path(key):return item(key)['path']
    def pair():
        r=item('reads')
        if not r.get('mate'):raise ValueError('Paired reads require an explicit mate binding')
        return r['path'],r['mate']
    steps=[];outs={}
    def step(argv,stdout=None):steps.append({'argv':[str(x) for x in argv],'stdout':stdout})
    def output(port,file,fmt,**metadata):outs.setdefault(port,[]).append({'file':file,'format':fmt,'metadata':metadata})
    tid=spec['id']
    if tid in ['gatk.mt.call','gatk.mt.filter','mito.liftover']:
        base=['gatk','--java-options',f"-Xmx{max(128,node.get('resources',{}).get('memory_mb',4096)-512)}m"]
        if tid=='gatk.mt.call':
            step([*base,'Mutect2','-R',path('reference'),'-I',path('alignment'),'--mitochondria-mode','--annotation','StrandBiasBySample','--read-filter','MateOnSameContigOrNoMappedMateReadFilter','--read-filter','MateUnmappedAndUnmappedReadFilter','--min-base-quality-score',p['minimum_base_quality'],'--max-reads-per-alignment-start',p['maximum_reads_per_start'],'--max-mnp-distance','0','-O','/work/mitochondrial.vcf',*advanced]);output('variants','mitochondrial.vcf','VCF',mitochondrial=True,unfiltered=True);output('statistics','mitochondrial.vcf.stats','REPORT')
        elif tid=='gatk.mt.filter':
            step([*base,'FilterMutectCalls','-R',path('reference'),'-V',path('variants'),'--stats',path('statistics'),'--mitochondria-mode','-O','/work/mitochondrial-filtered.vcf',*advanced]);output('variants','mitochondrial-filtered.vcf','VCF',mitochondrial=True)
        else:
            step([*base,'LiftoverVcf','-I',path('variants'),'-O','/work/shifted-back.vcf','--CHAIN',path('chain'),'--REJECT','/work/liftover-rejected.vcf','-R',path('reference'),*advanced]);output('variants','shifted-back.vcf','VCF',assembly=item('reference').get('assembly'),reference_digest=item('reference').get('reference_digest'));output('rejected','liftover-rejected.vcf','VCF')
    elif tid=='mito.extract':
        step(['samtools','view','--fetch-pairs','-L',path('regions'),'-b','-o','/work/selected.bam',path('alignment'),*advanced]);step(['samtools','view','-f','12','-b','-o','/work/unmapped-pairs.bam',path('alignment')]);step(['samtools','view','-f','4','-F','1','-b','-o','/work/unmapped-unpaired.bam',path('alignment')]);step(['samtools','cat','-o','/work/mitochondrial-candidates.bam','/work/selected.bam','/work/unmapped-pairs.bam','/work/unmapped-unpaired.bam']);output('alignment','mitochondrial-candidates.bam','BAM',numt_candidates_included=True,fully_unmapped_reads_retained=True)
    elif tid=='mito.realign':
        step(['cp',path('reference'),'/work/reference.fa']);step(['samtools','faidx','/work/reference.fa']);step(['bwa-mem2','index','/work/reference.fa']);step(['samtools','collate','-u','-O',path('alignment')],'collated.bam');step(['samtools','fastq','-n','/work/collated.bam'],'candidate-reads.fastq');step(['bwa-mem2','mem','-p','-t',threads,'-R','@RG\\tID:'+p['sample_id']+'\\tSM:'+p['sample_id']+'\\tPL:ILLUMINA',*advanced,'/work/reference.fa','/work/candidate-reads.fastq'],'realigned.sam');step(['samtools','sort','-@',threads,'-o','/work/realigned.bam','/work/realigned.sam']);step(['samtools','index','/work/realigned.bam']);output('alignment','realigned.bam','INDEXED_BAM',index=str(work/'realigned.bam.bai'),assembly=item('reference').get('assembly'),reference_digest=item('reference').get('reference_digest'))
    elif tid=='bcftools.annotate':
        import re
        if not re.fullmatch(r'INFO/[A-Za-z0-9_.]+(?:,INFO/[A-Za-z0-9_.]+)*',p['columns']):raise ValueError('Annotation columns must be explicit INFO field names')
        step(['bcftools','norm','-f',path('reference'),'-m','-any','-Oz','-o','/work/query-normalized.vcf.gz',path('variants')]);step(['bcftools','index','-t','/work/query-normalized.vcf.gz']);step(['bcftools','annotate','-a',path('database'),'-c',p['columns'],'-Oz','-o','/work/annotated.vcf.gz',*advanced,'/work/query-normalized.vcf.gz']);step(['bcftools','index','-t','/work/annotated.vcf.gz']);output('variants','annotated.vcf.gz','INDEXED_VCF',index=str(work/'annotated.vcf.gz.tbi'),annotation_fields=p['columns'])
    elif tid=='fastqc':
        reads=[r['path'] for r in mapped['reads']]+[r['mate'] for r in mapped['reads'] if r.get('mate')]
        step(['fastqc','--threads',threads,'--outdir','/work',*(['--nogroup'] if p.get('nogroup') else []),*advanced,*reads]);output('report','*_fastqc.html','REPORT')
    elif tid=='multiqc':
        step(['multiqc','/inputs','--outdir','/work','--filename','multiqc_report.html','--title',p['title'],'--force',*advanced]);output('report','multiqc_report.html','REPORT')
    elif tid=='fastp':
        r1,r2=pair();step(['fastp','-i',r1,'-I',r2,'-o','/work/trim_R1.fastq.gz','-O','/work/trim_R2.fastq.gz','--thread',threads,'--qualified_quality_phred',p['qualified_quality_phred'],'--length_required',p['length_required'],'--json','/work/fastp.json','--html','/work/fastp.html',*(['--detect_adapter_for_pe'] if p['detect_adapter_for_pe'] else []),*advanced]);output('reads','trim_R1.fastq.gz','FASTQ_PAIR',mate=str(work/'trim_R2.fastq.gz'));output('report','fastp.json','REPORT');output('report','fastp.html','REPORT')
    elif tid=='trimmomatic':
        a,b=pair();step(['trimmomatic','PE','-threads',threads,a,b,'/work/trim_R1.fastq.gz','/work/unpaired_R1.fastq.gz','/work/trim_R2.fastq.gz','/work/unpaired_R2.fastq.gz',f"SLIDINGWINDOW:{p['slidingwindow']}",f"MINLEN:{p['minlen']}",*advanced]);output('reads','trim_R1.fastq.gz','FASTQ_PAIR',mate=str(work/'trim_R2.fastq.gz'))
    elif tid in ['bwa-mem2','bowtie2','hisat2','minimap2','star']:
        read=item('reads');reads=[read['path']]+([read['mate']] if read.get('mate') else []);reference=path('reference')
        if tid=='bwa-mem2':
            # Indexes are written only in the bounded writable job directory.
            step(['cp',reference,'/work/reference.fa']);step(['bwa-mem2','index','/work/reference.fa']);step(['bwa-mem2','mem','-t',threads,'-R','@RG\\tID:'+p.get('sample_id','SAMPLE')+'\\tSM:'+p.get('sample_id','SAMPLE')+'\\tPL:ILLUMINA',*advanced,'/work/reference.fa',*reads],'aligned.sam');output('alignment','aligned.sam','SAM')
        elif tid in ['bowtie2','hisat2']:
            index='/work/reference_index';step([tid+'-build','-p',threads,reference,index]);step([tid,'-p',threads,'-x',index,*(['-1',reads[0],'-2',reads[1]] if len(reads)>1 else ['-U',reads[0]]),*advanced,'-S','/work/aligned.sam']);output('alignment','aligned.sam','SAM')
        elif tid=='minimap2':
            step(['minimap2','-ax',p['preset'],'-t',threads,*advanced,reference,*reads],'aligned.sam');output('alignment','aligned.sam','SAM')
        else:
            (work/'star-index').mkdir(exist_ok=True)
            step(['STAR','--runMode','genomeGenerate','--runThreadN',threads,'--genomeDir','/work/star-index','--genomeFastaFiles',reference,'--genomeSAindexNbases',str(p.get('genome_sa_index_nbases',6))])
            step(['STAR','--runThreadN',threads,'--genomeDir','/work/star-index','--readFilesIn',*reads,*(['--readFilesCommand','zcat'] if reads[0].endswith('.gz') else []),'--outFilterMultimapNmax',p['maximum_multimaps'],'--outSAMtype','BAM','SortedByCoordinate','--outFileNamePrefix','/work/',*advanced]);output('alignment','Aligned.sortedByCoord.out.bam','SORTED_BAM')
    elif tid=='salmon':
        a,b=pair();step(['salmon','quant','-i',path('reference'),'-l',p['libtype'],'-1',a,'-2',b,'-p',threads,'-o','/work/quant',*(['--validateMappings'] if p['validate_mappings'] else []),*advanced]);output('quantification','quant/quant.sf','QUANT')
    elif tid=='samtools':
        step(['samtools','flagstat','-@',threads,'-O','json',path('alignment'),*advanced],'flagstat.json');output('report','flagstat.json','REPORT')
    elif tid=='picard':
        step(['picard','MarkDuplicates','I='+path('alignment'),'O=/work/marked.bam','M=/work/duplicate_metrics.txt','REMOVE_DUPLICATES='+str(p['remove_duplicates']).lower(),*advanced]);output('alignment','marked.bam','SORTED_BAM');output('report','duplicate_metrics.txt','REPORT')
    elif tid in ['gatk-bqsr','haplotypecaller','mutect2']:
        reference=path('reference');base=['gatk','--java-options',f"-Xmx{max(128,node.get('resources',{}).get('memory_mb',4096)-512)}m"]
        if tid=='gatk-bqsr':step([*base,'BaseRecalibrator','-R',reference,'-I',path('alignment'),'--known-sites',path('known_sites'),'-O','/work/recal.table',*advanced]);output('report','recal.table','RECAL')
        elif tid=='haplotypecaller':
            intervals=path('intervals') if mapped.get('intervals') else p.get('interval')
            step([*base,'HaplotypeCaller','-R',reference,'-I',path('alignment'),'-O','/work/calls.vcf','--sample-ploidy',p['ploidy'],*(['-L',intervals] if intervals else []),*(['-ERC','GVCF'] if p['gvcf'] else []),*advanced]);output('variants','calls.vcf','VCF')
        else:
            step([*base,'Mutect2','-R',reference,'-I',path('tumor'),'-I',path('normal'),'-normal',p['normal_sample'],'-O','/work/unfiltered.vcf',*(['-L',p['interval']] if p.get('interval') else []),*advanced]);output('statistics','unfiltered.vcf.stats','REPORT');output('variants','unfiltered.vcf','VCF',unfiltered=True,warning='Run FilterMutectCalls and the appropriate contamination/orientation workflow before interpretation.')
    elif tid=='deepvariant':
        step(['/opt/deepvariant/bin/run_deepvariant','--model_type',p['model_type'],'--ref',path('reference'),'--reads',path('alignment'),'--output_vcf','/work/calls.vcf.gz','--num_shards',threads,*(['--regions',p['interval']] if p.get('interval') else []),*advanced]);output('variants','calls.vcf.gz','VCF')
    elif tid=='freebayes':
        step(['freebayes','-f',path('reference'),*(['-r',p['interval']] if p.get('interval') else []),*advanced,path('alignment')],'calls.vcf');output('variants','calls.vcf','VCF')
    elif tid=='bcftools':
        step(['bcftools','mpileup','-Ob','-f',path('reference'),'-o','/work/pileup.bcf',*(['-r',p['interval']] if p.get('interval') else []),path('alignment')]);step(['bcftools','call','-mv','-Ov','-o','/work/calls.vcf',*advanced,'/work/pileup.bcf']);output('variants','calls.vcf','VCF')
    elif tid=='sniffles2':
        step(['sniffles','--input',path('alignment'),'--vcf','/work/structural.vcf','--reference',path('reference'),'--threads',threads,'--minsupport',p['min_support'],*advanced]);output('variants','structural.vcf','VCF')
    elif tid in ['strelka2','manta']:
        exe='configureStrelkaGermlineWorkflow.py' if tid=='strelka2' else 'configManta.py'
        step([exe,'--bam',path('alignment'),'--referenceFasta',path('reference'),'--runDir','/work/workflow',*advanced]);step(['python','/work/workflow/runWorkflow.py','-m','local','-j',threads]);output('variants','workflow/results/variants/variants.vcf.gz' if tid=='strelka2' else 'workflow/results/variants/diploidSV.vcf.gz','VCF')
    elif tid=='cnvkit':
        step(['cnvkit.py','batch',path('alignment'),'--normal','--targets',path('targets'),'--fasta',path('reference'),'--output-dir','/work/cnv','--processes',threads,*advanced]);output('report','cnv/*.cns','CNV')
    elif tid=='vep':
        step(['vep','--input_file',path('variants'),'--output_file','/work/annotated.vcf','--vcf','--offline','--cache','--dir_cache',str(Path(path('database')).parent),'--assembly',p['assembly'],'--species',p['species'],'--fork',threads,*advanced]);output('variants','annotated.vcf','VCF')
    elif tid=='snpeff':
        step(['snpEff','-dataDir',str(Path(path('database')).parent),p['assembly'],path('variants'),*advanced],'annotated.vcf');output('variants','annotated.vcf','VCF')
    elif tid=='macs3':
        step(['macs3','callpeak','-t',path('treatment'),*(['-c',path('control')] if mapped.get('control') else []),'-g',p['genome_size'],'-q',p['qvalue'],'--outdir','/work','-n','peaks',*(['--broad'] if p['broad'] else []),*advanced]);output('peaks','peaks_peaks.broadPeak' if p['broad'] else 'peaks_peaks.narrowPeak','BED')
    elif tid=='kraken2':
        read=item('reads');step(['kraken2','--db',str(Path(path('database')).parent),'--threads',threads,'--confidence',p['confidence'],'--report','/work/kraken-report.tsv','--output','/work/classifications.tsv',*(['--paired',read['path'],read['mate']] if read.get('mate') else [read['path']]),*advanced]);output('report','kraken-report.tsv','REPORT')
    elif tid=='scanpy':
        code="import scanpy as s; a=s.read_h5ad(__import__('sys').argv[1]); s.pp.filter_cells(a,min_genes=1); s.pp.normalize_total(a,target_sum=1e4); s.pp.log1p(a); s.pp.highly_variable_genes(a,n_top_genes=int(__import__('sys').argv[2])); s.pp.pca(a); s.pp.neighbors(a,n_neighbors=int(__import__('sys').argv[3])); s.tl.umap(a,random_state=7341); s.tl.leiden(a,resolution=float(__import__('sys').argv[4]),random_state=7341); a.write_h5ad('/work/analyzed.h5ad')"
        step(['python','-c',code,path('counts'),p['n_top_genes'],p['n_neighbors'],p['resolution'],*advanced]);output('embedding','analyzed.h5ad','H5AD')
    elif tid=='varscan2':
        step(['samtools','mpileup','-f',path('reference'),path('alignment')],'pileup.tsv');step(['varscan','mpileup2cns','/work/pileup.tsv','--min-coverage',p['min_coverage'],'--min-var-freq',p['min_var_freq'],'--output-vcf','1',*advanced],'calls.vcf');output('variants','calls.vcf','VCF')
    elif tid=='delly':
        step(['delly','call','-g',path('reference'),'-o','/work/structural.bcf',*advanced,path('alignment')]);output('variants','structural.bcf','BCF')
    elif tid=='expansionhunter':
        step(['ExpansionHunter','--reads',path('alignment'),'--reference',path('reference'),'--variant-catalog',path('catalogue'),'--output-prefix','/work/repeats',*advanced]);output('variants','repeats.vcf','VCF');output('report','repeats.json','REPORT')
    elif tid=='whatshap':
        step(['whatshap','phase','--reference',path('reference'),'--output','/work/phased.vcf',*advanced,path('variants'),path('alignment')]);output('variants','phased.vcf','VCF')
    elif tid=='bismark':
        a,b=pair();step(['mkdir','-p','/work/genome']);step(['cp',path('reference'),'/work/genome/reference.fa']);step(['bismark_genome_preparation','--bowtie2','/work/genome']);step(['bismark','--genome','/work/genome','-1',a,'-2',b,'--output_dir','/work',*(['--non_directional'] if p['non_directional'] else []),*advanced]);output('alignment','*_bismark_bt2_pe.bam','BAM')
    elif tid=='gatk-apply-bqsr':
        step(['gatk','ApplyBQSR','-R',path('reference'),'-I',path('alignment'),'--bqsr-recal-file',path('recalibration'),'-O','/work/recalibrated.bam',*advanced]);output('alignment','recalibrated.bam','SORTED_BAM')
    elif tid=='mutect-filter':
        step(['gatk','FilterMutectCalls','-R',path('reference'),'-V',path('variants'),'--stats',path('statistics'),*(['--contamination-table',path('contamination')] if mapped.get('contamination') else []),*(['--orientation-bias-artifact-priors',path('orientation')] if mapped.get('orientation') else []),'-O','/work/filtered.vcf',*advanced]);output('variants','filtered.vcf','VCF')
    elif tid=='liftover.vcf':
        step(['picard','LiftoverVcf','I='+path('variants'),'O=/work/lifted.vcf','CHAIN='+path('chain'),'REJECT=/work/rejected.vcf','R='+path('reference'),*advanced]);output('variants','lifted.vcf','VCF',assembly=p['target_assembly']);output('rejected','rejected.vcf','VCF',assembly=p['source_assembly'])
    elif tid=='crossmap.bed':
        step(['CrossMap','bed',path('chain'),path('intervals'),'/work/lifted.bed',*advanced]);output('intervals','lifted.bed','BED',assembly=p['target_assembly'])
    elif tid=='bam.bigwig':
        step(['bamCoverage','--bam',path('alignment'),'--outFileName','/work/coverage.bw','--binSize',p['bin_size'],'--normalizeUsing',p['normalization'],*(['--effectiveGenomeSize',p['effective_genome_size']] if p['normalization']=='RPGC' else []),'--numberOfProcessors',threads,*advanced]);output('coverage','coverage.bw','BIGWIG')
    elif tid=='cohort.genotype':
        step(['gatk','CombineGVCFs','-R',path('reference'),*[a for r in mapped['gvcfs'] for a in ['-V',r['path']]],'-O','/work/combined.g.vcf']);step(['gatk','GenotypeGVCFs','-R',path('reference'),'-V','/work/combined.g.vcf','-O','/work/cohort.vcf',*advanced]);output('variants','cohort.vcf','VCF')
    elif tid=='benchmark.happy':
        step(['hap.py',path('truth'),path('query'),'-r',path('reference'),'-f',path('regions'),'-o','/work/benchmark','--engine',p['engine'],'--threads',threads,*(['--stratification',path('stratifications')] if mapped.get('stratifications') else []),*advanced]);output('summary','benchmark.summary.csv','REPORT');output('extended','benchmark.extended.csv','REPORT')
    elif tid.startswith('custom_'):
        mapping={'work':'/work'}
        for key,rows in mapped.items():
            if rows:mapping['in.'+key]=rows[0]['path']
        for raw in spec['command']:
            argv=[]
            for arg in raw:
                for key,value in mapping.items():arg=arg.replace('{'+key+'}',value)
                if '{secret.file}' in arg:
                    if not spec.get('secret_delivery'):raise ValueError('This contract has no scoped licence-file binding')
                    arg=arg.replace('{secret.file}','/run/ngs-secrets/license')
                if '{in.' in arg:raise ValueError('Unresolved custom tool input')
                argv.append(arg)
            for binding in spec.get('bindings',[]):
                key=binding['key'];value=mapped.get(key,[{}])[0].get('path') if binding.get('file') and mapped.get(key) else p.get(key)
                if binding.get('boolean'):
                    if value:argv.append(binding.get('prefix',''))
                    continue
                if value in [None,'']:
                    if binding.get('optional'):continue
                    raise ValueError('Required command binding is absent: '+key)
                prefix=binding.get('prefix','')
                argv.extend([prefix,str(value)] if prefix and binding.get('separate',True) else [prefix+str(value)])
            argv=[__import__('re').sub(r'\{param\.([A-Za-z0-9_-]+)\}',lambda m:str(p[m[1]]),arg) for arg in argv]
            step(argv+advanced,next(iter(spec['output_files'].values()))['file'] if spec.get('capture_stdout') and raw is spec['command'][-1] else None)
        for key,desc in spec['output_files'].items():output(key,desc['file'],desc['type'])
    elif tid in ['custom','sentieon','dragen','hgmd','annovar','exomiser','intervar','enformer','seurat','dorado','vg']:
        raise ValueError('This runtime requires its vendor/model-specific published container contract. Use the Universal Container publisher with explicit command and file bindings; no guessed execution profile is launched.')
    else:raise ValueError('Unknown published tool profile')
    return {'steps':steps,'outputs':outs,'bindings':list(dict.fromkeys(bindings))}

REQUIRES_PUBLISHED={'cosmic','bcl.convert','annovar','exomiser','intervar','enformer','seurat','dorado','vg','sentieon','dragen','hgmd'}
