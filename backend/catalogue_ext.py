"""Explicit interoperability contracts. Transformations never imply biological equivalence."""
def extend(add, P, param):
    add('sample.clinvar','ClinVar · annotated GRCh38 subset','Samples',[],[P('variants','VCF')],'First 1,000 records from the existing public ClinVar GRCh38 SSD snapshot. Retains source clinical labels and allele-frequency annotations where supplied; no clinical interpretation is generated.',builtin=True,docs='https://www.ncbi.nlm.nih.gov/clinvar/')
    add('sample.alignments','Alignment viewer · engineering fixture','Samples',[],[P('alignment','INDEXED_BAM'),P('reference','FASTA')],'Deterministic aligned reads with known strand and base mismatches for testing pileup rendering. Synthetic, never an accuracy benchmark.',builtin=True)
    def native(id,name,ins,outs,description,parameters,exe,image,docs,group='Adapters'):
        return add(id,name,group,ins,outs,description,parameters,executable=exe,image=image,spdx='See upstream',docs=docs)
    def portable(id,name,ins,outs,description,parameters=None,group='Adapters'):
        return add(id,name,group,ins,outs,description,parameters,builtin=True)
    for id,name,family,build,kind,docs in [
        ('grch37','GRCh37 / hg19 reference','reference','GRCh37','FASTA','https://www.ncbi.nlm.nih.gov/assembly/GCF_000001405.25/'),
        ('grch38','GRCh38 / hg38 reference','reference','GRCh38','FASTA','https://www.ncbi.nlm.nih.gov/assembly/GCF_000001405.40/'),
        ('chm13','T2T-CHM13 reference','reference','T2T-CHM13','FASTA','https://github.com/marbl/CHM13'),
        ('gnomad','gnomAD population frequencies','gnomad','GRCh38','DATABASE','https://gnomad.broadinstitute.org/data'),
        ('clinvar','ClinVar clinical assertions','clinvar','GRCh38','DATABASE','https://www.ncbi.nlm.nih.gov/clinvar/'),
        ('dbsnp','dbSNP release','dbsnp','GRCh38','DATABASE','https://www.ncbi.nlm.nih.gov/snp/'),
        ('1000g','1000 Genomes','1000genomes','GRCh38','DATABASE','https://registry.opendata.aws/1000-genomes/'),
        ('giab','GIAB / NIST truth variants','giab','GRCh38','VCF','https://registry.opendata.aws/giab/'),
        ('omim','OMIM licensed snapshot','omim','GRCh38','DATABASE','https://www.omim.org/'),
        ('cosmic','COSMIC licensed snapshot','cosmic','GRCh38','DATABASE','https://cancer.sanger.ac.uk/cosmic')]:
        add('resource.'+id,name,'References' if kind=='FASTA' else 'Databases',[],[P('reference' if kind=='FASTA' else 'variants' if kind=='VCF' else 'database',kind)],'Bind an exact permitted release from the indexed SSD inventory. The resolver ranks in-region public mirrors first for cloud execution; assembly and licensing remain explicit.',[param('asset_id','Versioned asset','',kind='asset'),param('assembly','Assembly',build)],builtin=True,asset_family=family,docs=docs,license='Academic' if id in ['omim','cosmic'] else 'FOSS',rights_note='Dataset rights are independent of the platform. Keep the release, citation and permitted use with every asset.')
    for id,name,ins,outs,description,parameters,exe,image,docs,group in [
        ('varscan2','VarScan2',[P('alignment','INDEXED_BAM'),P('reference','FASTA')],[P('variants','VCF')],'SAMtools pileup followed by VarScan mpileup2cns; review assay-specific significance and allele-frequency settings.',[param('min_coverage','Minimum coverage',10,'number',min=1,max=100000),param('min_var_freq','Minimum variant fraction',.05,'number',min=.0001,max=1)],'varscan','quay.io/biocontainers/varscan:2.4.6--hdfd78af_0','https://varscan.sourceforge.net/','Variants'),
        ('delly','Delly',[P('alignment','INDEXED_BAM'),P('reference','FASTA')],[P('variants','BCF')],'Structural variant discovery with explicit reference and evidence BAM.',[],'delly','quay.io/biocontainers/delly:1.3.3--h7b50bb2_0','https://github.com/dellytools/delly','Variants'),
        ('expansionhunter','ExpansionHunter',[P('alignment','INDEXED_BAM'),P('reference','FASTA'),P('catalogue','REPORT')],[P('variants','VCF'),P('report','REPORT')],'Genotype repeats defined by a supplied locus catalogue; no clinical repeat interpretation is inferred.',[],'ExpansionHunter',None,'https://github.com/Illumina/ExpansionHunter','Variants'),
        ('whatshap','WhatsHap',[P('alignment','INDEXED_BAM'),P('variants','VCF|BGZIP_VCF|INDEXED_VCF'),P('reference','FASTA')],[P('variants','VCF')],'Read-backed phasing with the exact source variants and reference.',[],'whatshap','quay.io/biocontainers/whatshap:2.5--py312h6c305fc_0','https://whatshap.readthedocs.io/','Variants'),
        ('bismark','Bismark',[P('reads','FASTQ_PAIR'),P('reference','FASTA')],[P('alignment','BAM')],'Prepare a writable bisulfite Bowtie2 index in job scratch, then align paired reads. Reference-sized scratch and time must be provisioned explicitly.',[param('non_directional','Non-directional library',False,'boolean')],'bismark','quay.io/biocontainers/bismark:0.24.2--hdfd78af_0','https://felixkrueger.github.io/Bismark/','Epigenomics'),
        ('gatk-apply-bqsr','GATK ApplyBQSR',[P('alignment','INDEXED_BAM'),P('reference','FASTA'),P('recalibration','RECAL')],[P('alignment','SORTED_BAM')],'Apply a previously fitted recalibration table using the matching reference.',[],'gatk','broadinstitute/gatk:4.6.1.0','https://gatk.broadinstitute.org/','Post-processing'),
        ('mutect-filter','GATK FilterMutectCalls',[P('variants','VCF|BGZIP_VCF|INDEXED_VCF'),P('reference','FASTA'),P('statistics','REPORT'),P('contamination','REPORT',False),P('orientation','DATABASE',False)],[P('variants','VCF')],'Filter Mutect2 results using the matching caller stats and optional contamination/orientation estimates. Assay validation remains required.',[],'gatk','broadinstitute/gatk:4.6.1.0','https://gatk.broadinstitute.org/','Variants')]:
        native(id,name,ins,outs,description,parameters,exe,image,docs,group)
    add('bcl.convert','Illumina BCL Convert','Basecalling',[P('run','ARCHIVE'),P('sample_sheet','CSV')],[P('reads','COLLECTION')],'Vendor BCL conversion requires the licensed executable, BCL run-folder contract and sample-sheet chemistry/version. Publish the reviewed vendor profile; a platform trial grants no Illumina software rights.',[param('published_tool','Approved vendor runtime contract','')],license='Commercial BYOL',credential='dragen',executable='bcl-convert',docs='https://support.illumina.com/sequencing/sequencing_software/bcl-convert.html')
    add('cosmic','COSMIC licensed dataset','Commercial',[P('variants','VCF|BGZIP_VCF|INDEXED_VCF'),P('database','DATABASE')],[P('result','REPORT')],'Bind the permitted release and a reviewed schema-specific lookup profile. COSMIC download credentials do not grant an unrestricted clinical annotation API.',[param('published_tool','Approved lookup contract','')],license='Commercial BYOL',credential='cosmic',docs='https://cancer.sanger.ac.uk/cosmic')
    variant='VCF|BGZIP_VCF|INDEXED_VCF|BCF'
    portable('annotation.merge','Join allele annotation reports',[P('variants',variant),P('reports','REPORT',many=True)],[P('variants','VCF'),P('report','REPORT')],
        'Join actual OpenNGS score-table / OncoKB report values back to identical CHROM/POS/REF/ALT alleles. Retains source report hashes; never infers ACMG classification.',[],group='Annotation')
    portable('contig.rename','Rename contigs',[P('data',variant)],[P('data','VCF')],
        'Explicit chr-prefix transformation or JSON mapping. Rejects many-to-one collisions; changes names only, never assembly coordinates.',
        [param('style','Target naming','chr','select',options=['chr','numeric','mapping']),param('mapping','Explicit JSON name mapping','{}')])
    portable('contig.rename.bed','Rename BED contigs',[P('data','BED')],[P('data','BED')],'Explicit chr-prefix or JSON mapping for BED coordinates; never changes assembly.',[param('style','Target naming','chr','select',options=['chr','numeric','mapping']),param('mapping','Explicit JSON name mapping','{}')])
    portable('reference.dictionary','Build reference dictionary',[P('reference','FASTA')],[P('dictionary','INTERVALLIST')],
        'Read FASTA sequence lengths and MD5 identities into a SAM/Picard dictionary. Does not rewrite another reference.',[])
    for id,name,src,dst in [('bam.sam','BAM → SAM','BAM|SORTED_BAM|INDEXED_BAM','SAM'),('bam.cram','BAM → CRAM','BAM|SORTED_BAM|INDEXED_BAM','CRAM'),('cram.bam','CRAM → BAM','CRAM|INDEXED_CRAM','BAM')]:
        portable(id,name,[P('alignment',src)]+([P('reference','FASTA')] if 'cram' in id else []),[P('alignment',dst)],'HTSlib conversion. CRAM requires the exact reference sequence; output indexing is a separate operation.')
    portable('cram.index','CRAM index',[P('alignment','CRAM')],[P('alignment','INDEXED_CRAM')],'Build CRAI for coordinate-sorted CRAM; unsorted files fail.')
    for id,name,src,dst in [('vcf.bcf','VCF → BCF',variant,'BCF'),('bcf.vcf','BCF → VCF','BCF','VCF')]:
        portable(id,name,[P('variants',src)],[P('variants',dst)],'HTSlib record-preserving encoding conversion; does not normalize variants.')
    portable('gtf.bed','GTF / GFF3 → BED',[P('intervals','GTF|GFF3')],[P('intervals','BED')],'Convert one-based closed features to zero-based half-open BED, retaining feature type and strand.',[param('feature','Feature type (empty = all)','exon')])
    portable('bed.gtf','BED → GTF',[P('intervals','BED')],[P('intervals','GTF')],'Emit explicit feature records with generated interval gene/transcript IDs; does not infer transcript biology.')
    native('liftover.vcf','Picard LiftoverVcf',[P('variants',variant),P('chain','CHAIN'),P('reference','FASTA')],[P('variants','VCF'),P('rejected','VCF')],
        'Requires a direction-specific chain and target reference. Rejects unmappable/reference-mismatching alleles into a separate output.',
        [param('source_assembly','Source assembly','GRCh37'),param('target_assembly','Target assembly','GRCh38')],
        'picard','quay.io/biocontainers/picard:3.3.0--hdfd78af_0','https://broadinstitute.github.io/picard/command-line-overview.html#LiftoverVcf')
    native('crossmap.bed','CrossMap BED liftover',[P('intervals','BED'),P('chain','CHAIN')],[P('intervals','BED')],
        'Explicit source→target chain conversion. Review split/unmapped features before downstream use.',
        [param('source_assembly','Source assembly','GRCh37'),param('target_assembly','Target assembly','GRCh38')],
        'CrossMap','quay.io/biocontainers/crossmap:0.7.3--pyhdfd78af_0','https://crossmap.readthedocs.io/')
    native('bam.bigwig','BAM → coverage BigWig',[P('alignment','INDEXED_BAM')],[P('coverage','BIGWIG')],
        'deepTools bamCoverage with explicit normalization and bin size.',[param('bin_size','Bin size',50,'number',min=1,max=100000),param('normalization','Normalization','None','select',options=['None','RPKM','CPM','BPM','RPGC']),param('effective_genome_size','Effective genome size',2913022398,'number',min=1,max=10000000000)],
        'bamCoverage','quay.io/biocontainers/deeptools:3.5.6--pyhdfd78af_0','https://deeptools.readthedocs.io/')
    portable('vcf.scatter','Scatter VCF by contig',[P('variants',variant)],[P('shards','COLLECTION')],
        'Write independent contig VCF artifacts. The collection preserves contig/build provenance; downstream ensemble operations accept collections.',[],group='Flow control')
    portable('vcf.gather','Gather contig VCFs',[P('shards','VCF|COLLECTION',many=True)],[P('variants','VCF')],
        'Merge disjoint-contig VCF shards with identical samples and header schema. Rejects overlapping shards or incompatible references.',[],group='Flow control')
    portable('qc.gate','Measured QC gate / router',[P('data','ANY'),P('report','REPORT')],[P('passed','ANY'),P('rejected','ANY'),P('decision','REPORT')],
        'Evaluate a numeric JSON metric path; missing/ambiguous values fail closed. Use explicit thresholds appropriate to your assay.',
        [param('metric','Metric path','mates.0.q30_percent'),param('operator','Comparison','>=','select',options=['>=','>','<=','<']),param('threshold','Threshold',80,'number',min=-1000000,max=1000000),param('halt','Fail run when guard fails',False,'boolean')],group='Flow control')
    portable('cohort.bind','Pedigree / sample-role manifest',[P('samples','ANY',many=True)],[P('cohort','COLLECTION'),P('manifest','REPORT')],
        'Validate distinct sample IDs and pedigree or tumor/normal roles from a supplied JSON manifest. Does not infer kinship or genotype jointly.',
        [param('members','Members JSON','[]','text','Array of {sample_id,role,artifact_index,father,mother}; one row per input.')],group='Cohorts')
    native('cohort.genotype','GATK joint genotyping',[P('gvcfs','VCF|BGZIP_VCF|INDEXED_VCF',many=True),P('reference','FASTA')],[P('variants','VCF')],
        'Combine compatible single-sample gVCFs and jointly genotype. Inputs must contain gVCF reference blocks; ordinary VCFs are rejected.',[],
        'gatk','broadinstitute/gatk:4.6.1.0','https://gatk.broadinstitute.org/',group='Cohorts')
    portable('vcf.ensemble','Exact-allele caller consensus',[P('callsets',variant+'|COLLECTION',many=True)],[P('variants','VCF'),P('report','REPORT')],
        'Count distinct caller files supporting identical CHROM/POS/REF/ALT alleles. Requires normalized biallelic records and matching samples. Does not equate haplotypes or reconcile conflicting genotypes.',
        [param('minimum_callers','Minimum independent callsets',2,'number',min=1,max=100)],group='Variants')
    portable('variant.guard','Variant quality guard',[P('variants',variant)],[P('variants','VCF'),P('report','REPORT')],
        'Apply explicit QUAL/MQ/DP/GQ/VAF thresholds to one selected sample. Missing required metrics are rejected and counted; thresholds are assay presets, not clinical guarantees.',
        [param('minimum_quality','Minimum QUAL',30,'number',min=0,max=10000),param('minimum_mq','Minimum INFO/MQ',40,'number',min=0,max=255),param('minimum_dp','Minimum sample DP',10,'number',min=0,max=1000000),param('minimum_gq','Minimum sample GQ',20,'number',min=0,max=999),param('minimum_vaf','Minimum VAF (FORMAT/AF or AD)',0,'number',min=0,max=1,step=.001),param('sample','Sample (empty = first)','')],group='Validation')
    portable('benchmark.exact','Exact-allele benchmark',[P('query',variant),P('truth',variant),P('regions','BED')],[P('report','REPORT')],
        'Small-variant exact normalized allele comparison inside explicit confident regions. Reports SNV/indel precision, recall, F1, Ti/Tv and Wilson intervals. Not a haplotype-aware or clinical validation substitute.',
        [param('sample','Sample (empty = first)',''),param('maximum_alleles','Safety ceiling',2000000,'number',min=1,max=10000000)],group='Validation')
    native('benchmark.happy','GIAB hap.py / vcfeval',[P('query','VCF|BGZIP_VCF|INDEXED_VCF'),P('truth','VCF|BGZIP_VCF|INDEXED_VCF'),P('regions','BED'),P('reference','FASTA'),P('stratifications','TSV',False)],
        [P('summary','REPORT'),P('extended','REPORT')],
        'Real haplotype-aware comparison with explicit truth, confident BED and reference; optional GA4GH stratification TSV must refer to available mounted BEDs.',
        [param('engine','Comparison engine','vcfeval','select',options=['vcfeval','xcmp'])],
        'hap.py',None,'https://github.com/Illumina/hap.py',group='Validation')
    portable('rna.fdr','RNA differential-expression FDR guard',[P('table','TSV|CSV')],[P('table','TSV'),P('report','REPORT')],
        'Benjamini–Hochberg correction of supplied raw p-values and FDR filtering. Requires valid p-values from a separately justified statistical model; does not perform differential expression.',
        [param('p_column','Raw p-value column','pvalue'),param('fdr','Maximum BH FDR',.05,'number',min=.000001,max=1)],group='Validation')
