"""Versioned scientific node definitions; catalogue presence is not runtime approval."""
import copy
import json
from . import db

def param(key, label, default, kind='text', tip='', **limits):
    return {'key':key,'label':label,'default':default,'kind':kind,'tip':tip,**limits}

def port(key, kind, required=True, many=False):
    return {'key':key,'type':kind,'required':required,'many':many}

def node(id, name, group, inputs, outputs, description, params=None, license='FOSS', spdx=None, executable=None, image=None, docs=None, builtin=False, resource='cpu', **extra):
    return {'id':id,'name':name,'group':group,'version':'1.0.0','inputs':inputs,'outputs':outputs,'description':description,'parameters':params or [],'license':license,'spdx':spdx,'executable':executable,'image':image,'docs':docs,'builtin':builtin,'resource':resource,'resources':{'threads':2,'memory_mb':4096,'timeout_seconds':120},**extra}

P=port
NODES=[]
def add(*args,**kw):
    n=node(*args,**kw);NODES.append(n);return n

add('sample.fastq','Paired reads · engineering fixture','Samples',[],[P('reads','FASTQ_PAIR')],'Deterministic synthetic paired reads with a small reference. Tests wiring and execution; never presented as a human benchmark.',builtin=True,provenance='Generated locally with seed 7341; synthetic, no patient data.')
add('sample.rna','RNA-seq · public bacterial reads','Samples',[],[P('reads','FASTQ_PAIR')],'Small public paired prokaryotic RNA-seq reads from a pinned nf-core test-data revision. Download is bounded and provenance is retained.',builtin=True,docs='https://github.com/nf-core/test-datasets/tree/rnaseq')
add('sample.giab','GIAB HG002 · chr22 variants','Samples',[],[P('variants','VCF')],'Real NIST v4.2.1 GRCh38 truth variants, limited to 1,000 chr22 records. Uses the indexed SSD copy or an authenticated public-S3 range reader.',builtin=True,docs='https://registry.opendata.aws/giab/')
add('sample.somatic','Tumor / normal · engineering fixture','Samples',[],[P('tumor','FASTQ_PAIR'),P('normal','FASTQ_PAIR')],'Seeded tumor/normal sequence fixture for command and role wiring. Not a validated somatic benchmark.',builtin=True)
add('asset.file','Local / cloud asset','Assets',[],[P('data','ANY')],'Bind an indexed SSD asset, completed upload, result or approved public S3 object. Output type is resolved from the asset.',[param('asset_id','Asset','',kind='asset')],builtin=True)
add('reference.tiny','Engineering reference','References',[],[P('reference','FASTA')],'Small deterministic reference matching the engineering read fixtures.',builtin=True)
add('reference.asset','Reference genome','References',[],[P('reference','FASTA')],'Bind a specific reference build from SSD or the public registry; downstream build identity must agree.',[param('asset_id','Reference asset','',kind='asset'),param('assembly','Genome assembly','GRCh38',kind='select',options=['GRCh37','GRCh38','T2T-CHM13','rCRS','custom','engineering'])],builtin=True)
add('database.asset','Database / model asset','Databases',[],[P('database','DATABASE')],'Versioned database/model binding. Rights and assembly are separate from tool-code licensing.',[param('asset_id','Database asset','',kind='asset'),param('assembly','Assembly','GRCh38')],builtin=True)

# Small portable operations execute in a separate bounded process. Native tools
# below are compiled into argv and executed only by an admitted backend.
add('qc.summary','FASTQ quality summary','QC', [P('reads','FASTQ_PAIR|FASTQ')],[P('report','REPORT')],'Compute actual read count, base qualities, GC, N content and length distribution. This is a Studio QC report, not FastQC.',[param('max_reads','Maximum records per mate',100000,'number','Bounded preview; 0 scans the complete input.',min=0,max=10000000)],builtin=True)
add('vcf.stats','Variant statistics','Variants',[P('variants','VCF|BGZIP_VCF|INDEXED_VCF')],[P('report','REPORT')],'Actual variant counts, SNV/indel classes, chromosomes, sample names and filter distribution.',builtin=True)
add('bam.stats','Alignment statistics','Post-processing',[P('alignment','BAM|SORTED_BAM|INDEXED_BAM|SAM')],[P('report','REPORT')],'Actual alignment, mapping and duplicate flag counts using HTSlib.',builtin=True)
add('sam.bam','SAM → BAM','Adapters',[P('alignment','SAM')],[P('alignment','BAM')],'Convert SAM records to BAM without pretending that sorting or indexing has occurred.',builtin=True)
add('bam.sort','Coordinate sort','Adapters',[P('alignment','SAM|BAM')],[P('alignment','SORTED_BAM')],'Coordinate-sort alignments and retain reference identity.',builtin=True)
add('bam.index','BAM index','Adapters',[P('alignment','SORTED_BAM')],[P('alignment','INDEXED_BAM')],'Build a matching BAI or CSI and keep data/index bound as one artifact.',[param('csi','Use CSI index',False,'boolean')],builtin=True)
add('vcf.bgzip','VCF → BGZF','Adapters',[P('variants','VCF')],[P('variants','BGZIP_VCF')],'BGZF compression; ordinary gzip is not substituted for indexed BGZF.',builtin=True)
add('vcf.index','VCF index','Adapters',[P('variants','BGZIP_VCF')],[P('variants','INDEXED_VCF')],'Create a tabix VCF index; rejects unsorted records rather than silently reordering variants.',builtin=True)
add('vcf.filter','Filter variants','Variants',[P('variants','VCF|BGZIP_VCF|INDEXED_VCF')],[P('variants','VCF')],'Filter by chromosome, minimum QUAL and PASS state while preserving the source header.',[param('chromosome','Chromosome','','text','Exact contig identifier, for example chr22.'),param('minimum_quality','Minimum QUAL',0,'number',min=0,max=10000),param('pass_only','PASS / unfiltered only',True,'boolean')],builtin=True)
add('bed.intervals','BED → IntervalList','Adapters',[P('intervals','BED'),P('reference','FASTA')],[P('intervals','INTERVALLIST')],'Convert zero-based half-open BED coordinates to one-based closed Picard intervals using the bound reference dictionary.',builtin=True)
add('intervals.bed','IntervalList → BED','Adapters',[P('intervals','INTERVALLIST')],[P('intervals','BED')],'Explicit coordinate conversion with validation.',builtin=True)
add('fastq.split','Deinterleave paired FASTQ','Adapters',[P('reads','INTERLEAVED_FASTQ')],[P('reads','FASTQ_PAIR')],'Split alternating mates and verify read identifiers and pairing.',builtin=True)
add('fastq.merge','Interleave paired FASTQ','Adapters',[P('reads','FASTQ_PAIR')],[P('reads','INTERLEAVED_FASTQ')],'Interleave matched reads; never concatenate mates and call them paired.',builtin=True)
add('utility.gather','Gather artifact collection','Utilities',[P('items','ANY',many=True)],[P('collection','COLLECTION')],'Collect input artifacts without altering biological content.',builtin=True)
add('utility.scatter','Scatter intervals','Utilities',[P('intervals','BED')],[P('shards','COLLECTION')],'Split BED rows into a bounded number of explicit interval artifacts.',[param('shards','Number of shards',4,'number',min=1,max=32)],builtin=True)
add('utility.condition','Conditional route','Utilities',[P('data','ANY')],[P('accepted','ANY')],'Route an artifact only when its measured byte count passes the selected threshold. A rejected branch is marked skipped.',[param('minimum_bytes','Minimum bytes',1,'number',min=0),param('maximum_bytes','Maximum bytes',1000000000,'number',min=1)],builtin=True)
add('utility.export','Publish outputs','Utilities',[P('data','ANY',many=True)],[P('published','COLLECTION')],'Publish immutable artifacts to this run’s result list with SHA-256 provenance.',builtin=True)

THREADS=param('threads','Threads',2,'number',min=1,max=64)
def tool(id,name,group,ins,outs,desc,params,exe,image,spdx,docs,**extra):
    return add(id,name,group,ins,outs,desc,params,executable=exe,image=image,spdx=spdx,docs=docs,**extra)

tool('fastqc','FastQC','QC',[P('reads','FASTQ_PAIR|FASTQ')],[P('report','REPORT')],'Per-base quality, composition, duplication and adapter diagnostics.',[param('nogroup','Disable base grouping',False,'boolean')],'fastqc','quay.io/biocontainers/fastqc:0.12.1--hdfd78af_0','GPL-3.0','https://www.bioinformatics.babraham.ac.uk/projects/fastqc/')
tool('multiqc','MultiQC','QC',[P('reports','REPORT|COLLECTION',many=True)],[P('report','REPORT')],'Aggregate supported tool reports into an HTML report.',[param('title','Report title','NGS Studio')],'multiqc','quay.io/biocontainers/multiqc:1.25.1--pyhdfd78af_0','GPL-3.0','https://docs.seqera.io/multiqc')
tool('fastp','fastp','QC',[P('reads','FASTQ_PAIR')],[P('reads','FASTQ_PAIR'),P('report','REPORT')],'Adapter trimming, length and quality filtering for paired reads.',[param('qualified_quality_phred','Qualified Phred',15,'number',min=0,max=40),param('length_required','Minimum length',15,'number',min=1,max=1000),param('detect_adapter_for_pe','Detect paired adapters',True,'boolean')],'fastp','quay.io/biocontainers/fastp:0.23.4--hadf994f_2','MIT','https://github.com/OpenGene/fastp')
tool('trimmomatic','Trimmomatic','QC',[P('reads','FASTQ_PAIR')],[P('reads','FASTQ_PAIR')],'Paired-end quality trimming with explicit step controls.',[param('slidingwindow','Window:quality','4:15'),param('minlen','Minimum length',36,'number',min=1,max=1000)],'trimmomatic','quay.io/biocontainers/trimmomatic:0.39--hdfd78af_2','GPL-3.0','https://github.com/usadellab/Trimmomatic')
tool('dorado','Dorado','Basecalling',[P('signal','POD5')],[P('alignment','BAM')],'Nanopore basecalling; requires a reviewed model and compatible GPU profile.',[param('model','Model','hac'),param('device','Device','cuda:all')],'dorado',None,None,'https://github.com/nanoporetech/dorado',license='Academic',resource='gpu',rights_note='ONT software/model terms must be reviewed; the platform tier grants no vendor rights.')
align=[('bwa-mem2','BWA-MEM2','bwa-mem2','MIT','https://github.com/bwa-mem2/bwa-mem2'),('bowtie2','Bowtie2','bowtie2','GPL-3.0','https://bowtie-bio.sourceforge.net/bowtie2/manual.shtml'),('minimap2','Minimap2','minimap2','MIT','https://github.com/lh3/minimap2'),('star','STAR','STAR','BSD-3-Clause','https://github.com/alexdobin/STAR'),('hisat2','HISAT2','hisat2','GPL-3.0','https://daehwankimlab.github.io/hisat2/'),('vg','VG','vg','MIT','https://github.com/vgteam/vg')]
images={'bwa-mem2':'quay.io/biocontainers/bwa-mem2:2.2.1--hd03093a_5','bowtie2':'quay.io/biocontainers/bowtie2:2.5.4--he96a11b_5','minimap2':'quay.io/biocontainers/minimap2:2.28--he4a0461_3','star':'quay.io/biocontainers/star:2.7.11b--h43eeafb_2','hisat2':'quay.io/biocontainers/hisat2:2.2.1--h503566f_8'}
for id,name,exe,spdx,docs in align:
    options=[param('preset','Preset','sr',kind='select',options=['sr','map-ont','map-hifi','splice'])] if id=='minimap2' else [param('maximum_multimaps','Maximum multimaps',10,'number',min=1,max=100)] if id=='star' else [param('extra','Algorithm options','','text','Advanced options are parsed as argv, never a host shell.')]
    tool(id,name,'Alignment',[P('reads','FASTQ_PAIR|FASTQ'),P('reference','FASTA|INDEX')],[P('alignment','SORTED_BAM' if id=='star' else 'SAM')],'Align reads against an explicitly bound reference/index; no build inference from filenames alone.',options,exe,images.get(id),spdx,docs)
tool('salmon','Salmon','RNA-seq',[P('reads','FASTQ_PAIR'),P('reference','INDEX')],[P('quantification','QUANT')],'Transcript quantification using an explicitly prepared Salmon index.',[param('libtype','Library type','A'),param('validate_mappings','Validate mappings',True,'boolean')],'salmon','quay.io/biocontainers/salmon:1.10.3--h6dccd9a_2','GPL-3.0','https://salmon.readthedocs.io/')
tool('samtools','SAMtools flagstat','Post-processing',[P('alignment','BAM|SORTED_BAM|INDEXED_BAM')],[P('report','REPORT')],'Native SAMtools flag statistics.',[], 'samtools','quay.io/biocontainers/samtools:1.21--h50ea8bc_0','MIT','https://www.htslib.org/doc/samtools.html')
tool('picard','Picard MarkDuplicates','Post-processing',[P('alignment','SORTED_BAM')],[P('alignment','SORTED_BAM'),P('report','REPORT')],'Mark duplicate reads and retain duplication metrics.',[param('remove_duplicates','Remove duplicates',False,'boolean')],'picard','quay.io/biocontainers/picard:3.3.0--hdfd78af_0','MIT','https://broadinstitute.github.io/picard/')
tool('gatk-bqsr','GATK4 BaseRecalibrator','Post-processing',[P('alignment','INDEXED_BAM'),P('reference','FASTA'),P('known_sites','INDEXED_VCF')],[P('report','RECAL')],'Generate a BQSR table from known sites; ApplyBQSR is a separate operation.',[], 'gatk','broadinstitute/gatk:4.6.1.0','Apache-2.0','https://gatk.broadinstitute.org/')
callers=[('haplotypecaller','GATK HaplotypeCaller','gatk','Apache-2.0'),('deepvariant','DeepVariant','/opt/deepvariant/bin/run_deepvariant','BSD-3-Clause'),('mutect2','GATK Mutect2','gatk','Apache-2.0'),('strelka2','Strelka2','configureStrelkaGermlineWorkflow.py','GPL-3.0'),('freebayes','FreeBayes','freebayes','MIT'),('bcftools','Bcftools mpileup + call','bcftools','MIT'),('manta','Manta','configManta.py','GPL-3.0'),('sniffles2','Sniffles2','sniffles','MIT'),('cnvkit','CNVkit','cnvkit.py','Apache-2.0')]
for id,name,exe,spdx in callers:
    ins=[P('alignment','INDEXED_BAM'),P('reference','FASTA')]
    if id=='mutect2':ins=[P('tumor','INDEXED_BAM'),P('normal','INDEXED_BAM'),P('reference','FASTA')]
    if id=='cnvkit':ins.append(P('targets','BED'))
    params=[param('interval','Interval / region','','text','Leave empty for the complete bound input.')]
    if id=='haplotypecaller':params+=[param('ploidy','Sample ploidy',2,'number',min=1,max=20),param('gvcf','Emit GVCF',False,'boolean')]
    if id=='deepvariant':params+=[param('model_type','Model type','WGS','select',options=['WGS','WES','PACBIO','ONT_R104'])]
    if id=='mutect2':params+=[param('normal_sample','Normal sample ID','NORMAL')]
    if id=='sniffles2':params+=[param('min_support','Minimum support',3,'number',min=1,max=100)]
    image='broadinstitute/gatk:4.6.1.0' if id in ['haplotypecaller','mutect2'] else 'google/deepvariant:1.8.0' if id=='deepvariant' else 'quay.io/biocontainers/bcftools:1.21--h3a4d415_1' if id=='bcftools' else None
    tool(id,name,'Variants',ins,[P('variants','VCF')], 'A real native caller profile; reference/index, sample-role and tool-specific data requirements are checked before execution.',params,exe,image,spdx,{'freebayes':'https://github.com/freebayes/freebayes','sniffles2':'https://github.com/fritzsedlazeck/Sniffles','strelka2':'https://github.com/Illumina/strelka','manta':'https://github.com/Illumina/manta','cnvkit':'https://cnvkit.readthedocs.io/'}.get(id,'https://gatk.broadinstitute.org/' if exe=='gatk' else 'https://github.com/google/deepvariant'))
annotations=[('vep','Ensembl VEP','vep','Apache-2.0','https://www.ensembl.org/info/docs/tools/vep/'),('annovar','ANNOVAR','table_annovar.pl',None,'https://annovar.openbioinformatics.org/'),('snpeff','SnpEff','snpEff','LGPL-3.0','https://pcingola.github.io/SnpEff/'),('exomiser','Exomiser','exomiser', 'AGPL-3.0','https://github.com/exomiser/Exomiser'),('intervar','InterVar','InterVar.py',None,'https://github.com/WGLab/InterVar')]
for id,name,exe,spdx,docs in annotations:
    tool(id,name,'Annotation',[P('variants','VCF|BGZIP_VCF|INDEXED_VCF'),P('database','DATABASE')],[P('variants','VCF')],'Annotation/prioritization requires the exact database version, assembly and permitted data rights.',[param('assembly','Assembly','GRCh38'),param('species','Species','homo_sapiens')],exe,None,spdx,docs,license='Commercial BYOL' if id=='annovar' else 'FOSS',credential='annovar' if id=='annovar' else None,rights_note='Code and database rights are separate; confirm the published data licence.')
for id,name,docs in [('alphamissense','AlphaMissense','https://github.com/google-deepmind/alphamissense'),('spliceai','SpliceAI','https://github.com/Illumina/SpliceAI'),('cadd','CADD','https://cadd.gs.washington.edu/'),('revel','REVEL','https://sites.google.com/site/revelgenomics/'),('enformer','Enformer','https://github.com/google-deepmind/deepmind-research/tree/master/enformer')]:
    add(id,name,'AI models',[P('variants','VCF|BGZIP_VCF|INDEXED_VCF'),P('database','DATABASE')],[P('annotation','REPORT')],'Lookup/scoring with a separately bound published model or score dataset. Code, weights and score-table rights require independent review.',[param('assembly','Assembly','GRCh38'),param('threshold','Display threshold',0.5,'number',min=0,max=1,step=0.01)],license='Academic',docs=docs,builtin=id!='enformer',rights_note='No academic/commercial rights are inferred from a platform trial. An administrator must record the applicable data/model permission.')
tool('scanpy','Scanpy','Single cell',[P('counts','H5AD')],[P('embedding','H5AD')],'Normalize, log-transform, select variable genes, compute PCA, neighbours and UMAP.',[param('n_top_genes','Variable genes',2000,'number',min=100,max=10000),param('n_neighbors','Neighbours',15,'number',min=2,max=200),param('resolution','Leiden resolution',1.0,'number',min=0.01,max=10)],'python',None,'BSD-3-Clause','https://scanpy.readthedocs.io/')
tool('seurat','Seurat','Single cell',[P('counts','MTX_DIR')],[P('embedding','RDS')],'Normalize, select features, PCA, graph clustering and UMAP.',[param('nfeatures','Variable features',2000,'number',min=100,max=10000),param('resolution','Resolution',0.8,'number',min=0.01,max=10)],'Rscript',None,'MIT','https://satijalab.org/seurat/')
tool('macs3','MACS3','Epigenomics',[P('treatment','BAM|SORTED_BAM|INDEXED_BAM'),P('control','BAM|SORTED_BAM|INDEXED_BAM',False)],[P('peaks','BED')],'Peak calling with optional control and explicit genome size.',[param('genome_size','Effective genome size','hs'),param('qvalue','Q-value',0.05,'number',min=0.000001,max=1),param('broad','Broad peaks',False,'boolean')],'macs3',None,'BSD-3-Clause','https://macs3-project.github.io/MACS/')
tool('kraken2','Kraken2','Metagenomics',[P('reads','FASTQ_PAIR|FASTQ'),P('database','DATABASE')],[P('report','REPORT')],'Taxonomic classification with an explicitly bound Kraken database.',[param('confidence','Confidence',0.1,'number',min=0,max=1)],'kraken2',None,'MIT','https://github.com/DerrickWood/kraken2')
for id,name,docs in [('franklin','Genoox Franklin','https://franklin.genoox.com/'),('oncokb','OncoKB','https://api.oncokb.org/')]:
    add(id,name,'Clinical APIs',[P('variants','VCF|BGZIP_VCF|INDEXED_VCF')],[P('annotation','REPORT')],'Use your contracted API account; credentials stay in the server-side vault and requests pass through a scoped provider adapter.',[param('assembly','Reference genome','GRCh38','select',options=['GRCh37','GRCh38'])],license='Paid API BYOK',credential=id,docs=docs)
for id,name in [('sentieon','Sentieon'),('dragen','DRAGEN'),('hgmd','HGMD lookup')]:
    add(id,name,'Commercial',[P('data','ANY')],[P('result','ANY')],'Vendor-specific installation, licence and hardware profile are required. Register the exact runtime/argv using the custom tool publisher.',[param('published_tool','Approved custom tool ID','')],license='Commercial BYOL',credential=id,docs={'sentieon':'https://support.sentieon.com/','dragen':'https://help.dragen.illumina.com/','hgmd':'https://www.hgmd.cf.ac.uk/'}[id])
add('custom','Universal container','Utilities',[P('data','ANY',many=True)],[P('result','ANY')],'Run an approved digest-pinned OCI/BioContainer command with named input/output contracts. Scientific containers receive no AWS or vault credentials.',[param('published_tool','Published tool ID','')],license='Review required')

add('mt.qc','mtDNA allele-fraction QC','Variants',[P('alignment','INDEXED_BAM'),P('reference','FASTA')],[P('report','REPORT')],'Measured mitochondrial base counts and allele fractions. This is a QC report, not a validated heteroplasmy caller; NUMT, strand-bias and rotated-reference validation remain separate.',[param('contig','Mitochondrial contig','chrM'),param('minimum_depth','Minimum depth',50,'number',min=1,max=100000),param('minimum_base_quality','Minimum base quality',20,'number',min=0,max=60),param('minimum_mapping_quality','Minimum mapping quality',30,'number',min=0,max=60),param('minimum_fraction','Minimum alternate fraction',0.01,'number',min=0.0001,max=1,step=0.001)],builtin=True)
from .catalogue_ext import extend
extend(add, P, param)
from .assay_catalogue import extend as extend_assays
extend_assays(add,P,param)
BASE={n['id']:n for n in NODES}
BASE['haplotypecaller']['inputs'].append(P('intervals','BED|INTERVALLIST',False))
BASE['oncokb']['parameters'] += [param('origin','Variant origin','somatic','select',options=['somatic','germline']),param('tumor_type','OncoTree tumor type',''),param('max_variants','Maximum provider calls',25,'number',min=1,max=100),param('allow_external_processing','Allow sending variants to OncoKB',False,'boolean')]
BASE['enformer']['inputs']=[P('sequence','FASTA'),P('model','DATABASE')]
BASE['mutect2']['outputs'].append(P('statistics','REPORT'))
BASE['cnvkit']['outputs']=[P('report','CNV')]
for _id in ['annovar','exomiser','intervar','enformer','seurat','dorado','vg']:
    BASE[_id]['parameters'].append(param('published_tool','Approved runtime contract ID','','text','Publish a vendor/model-specific container using the Universal Container publisher. Input/output names must match this node.'))
BASE['exomiser']['outputs']=[P('prioritization','REPORT')]
BASE['intervar']['outputs']=[P('classification','REPORT')]
for _p in BASE['cadd']['parameters']:
    if _p['key']=='threshold':_p.update(label='Minimum CADD PHRED',default=20,min=0,max=60)
BASE['bwa-mem2']['parameters'].append(param('sample_id','Read-group sample ID','SAMPLE'))
ACCEPTS={'BAM':{'BAM','SORTED_BAM','INDEXED_BAM'},'SORTED_BAM':{'SORTED_BAM','INDEXED_BAM'},'VCF':{'VCF','BGZIP_VCF','INDEXED_VCF'},'BGZIP_VCF':{'BGZIP_VCF','INDEXED_VCF'},'FASTQ':{'FASTQ'},'ANY':{'ANY'}}

def compatible(source, target):
    return target=='ANY' or source=='ANY' or any(source in ACCEPTS.get(t,{t}) for t in target.split('|'))

def catalogue(user_id=None):
    result=copy.deepcopy(NODES)
    if user_id:
        for r in db.query('SELECT * FROM custom_tools WHERE user_id=?',(user_id,)):
            n=json.loads(r['body']);n['approved']=bool(r['approved']);result.append(n)
    quarantined={r['tool_id']:r['reason'] for r in db.query('SELECT * FROM quarantine')}
    for n in result:
        n['quarantined']=quarantined.get(n['id'])
        if user_id and n.get('asset_family'):
            rows=db.query('SELECT id,name,format,assembly,bytes FROM assets WHERE user_id=? AND family=? AND (assembly=? OR assembly IS NULL) ORDER BY name LIMIT 30',(user_id,n['asset_family'],next((p['default'] for p in n['parameters'] if p['key']=='assembly'),None)))
            if n['outputs'][0]['type']=='FASTA':rows=[r for r in rows if r['format']=='FASTA']
            elif n['outputs'][0]['type']=='VCF':rows=[r for r in rows if r['format'] in ['VCF','INDEXED_VCF','BGZIP_VCF']]
            else:rows=[r for r in rows if r['format']!='INDEX']
            n['asset_candidates']=rows
            if len(rows)==1:next(p for p in n['parameters'] if p['key']=='asset_id')['default']=rows[0]['id']
    return result

def definitions(user_id=None):
    return {n['id']:n for n in catalogue(user_id)}

def defaults(spec):
    return {p['key']:p['default'] for p in spec['parameters']}

def templates():
    def make(id,name,description,tools,links=None):
        ns=[{'id':f'n{i}','tool':t,'x':90+i*260,'y':180+(i%2)*25,'params':defaults(BASE[t]),'resources':{'threads':1,'memory_mb':1024,'timeout_seconds':30}} for i,t in enumerate(tools)]
        if links is None:links=[(i,BASE[tools[i]]['outputs'][0]['key'],i+1,BASE[tools[i+1]]['inputs'][0]['key']) for i in range(len(ns)-1)]
        es=[{'id':f'e{i}','source':f'n{a}','sourcePort':ap,'target':f'n{b}','targetPort':bp} for i,(a,ap,b,bp) in enumerate(links)]
        return {'id':id,'name':name,'description':description,'graph':{'name':name,'nodes':ns,'edges':es}}
    base=[make('quickstart','Read quality · instant','Actual QC on deterministic paired reads. No Docker or cloud compute required.',['sample.fastq','qc.summary','utility.export']),
            make('giab','GIAB chr22 · real data','NIST truth variants → explicit filtering → statistics and published output.',['sample.giab','vcf.filter','vcf.stats','utility.export']),
            make('rna-qc','RNA-seq · real reads','Public bacterial RNA reads → quality summary.',['sample.rna','qc.summary','utility.export']),
            make('germline','Germline DNA','Reference + paired reads → alignment → sorting/indexing → HaplotypeCaller. Requires approved native images.',['sample.fastq','reference.tiny','bwa-mem2','bam.sort','bam.index','haplotypecaller','vcf.stats'],[(0,'reads',2,'reads'),(1,'reference',2,'reference'),(2,'alignment',3,'alignment'),(3,'alignment',4,'alignment'),(4,'alignment',5,'alignment'),(1,'reference',5,'reference'),(5,'variants',6,'variants')])]

    from .templates_ext import extend as extend_templates
    from .clinical_templates import extend as clinical_templates
    sample_native=[make('native-qc','FastQC + fastp · sample reads','Execute the actual FastQC and fastp binaries on bounded synthetic paired reads.',['sample.fastq','fastqc','fastp','qc.summary','utility.export'],[(0,'reads',1,'reads'),(0,'reads',2,'reads'),(2,'reads',3,'reads'),(1,'report',4,'data'),(2,'report',4,'data'),(3,'report',4,'data')]),
        make('dna-engineering','DNA alignment + calls · engineering','Real Minimap2, HTSlib, SAMtools and FreeBayes execution on a labelled synthetic reference. This is a software integration test, not human WGS validation.',['sample.fastq','reference.tiny','minimap2','sam.bam','bam.sort','bam.index','samtools','freebayes','vcf.stats','utility.export'],[(0,'reads',2,'reads'),(1,'reference',2,'reference'),(2,'alignment',3,'alignment'),(3,'alignment',4,'alignment'),(4,'alignment',5,'alignment'),(5,'alignment',6,'alignment'),(5,'alignment',7,'alignment'),(1,'reference',7,'reference'),(7,'variants',8,'variants'),(6,'report',9,'data'),(8,'report',9,'data'),(7,'variants',9,'data')])]
    sample_native[1]['graph']['nodes'][2]['params']['preset']='sr'
    return base+extend_templates(make)+clinical_templates(make)+sample_native+[make('clinvar','ClinVar · tertiary review','Public annotated GRCh38 variants and published output for the results workbench.',['sample.clinvar','utility.export']),make('pileup','Genome tracks · engineering','Known synthetic indexed alignments and matching reference for pileup inspection.',['sample.alignments','utility.export'],[(0,'alignment',1,'data'),(0,'reference',1,'data')])]
