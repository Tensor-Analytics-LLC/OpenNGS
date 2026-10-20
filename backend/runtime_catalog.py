"""Reviewed CPU environments, with execution evidence kept separate from recipes."""
import json
import re
from . import db

EXTENDED_PACKAGES = {
    'multiqc': '1.25.1', 'trimmomatic': '0.39', 'bowtie2': '2.5.4',
    'hisat2': '2.2.1', 'star': '2.7.11b', 'salmon': '1.10.3',
    'sniffles': '2.6.1', 'varscan': '2.4.6', 'delly': '1.3.1',
    'whatshap': '2.4', 'macs3': '3.0.3', 'kraken2': '2.1.3',
    'crossmap': '0.7.3', 'deeptools': '3.5.6', 'cnvkit': '0.9.14',
    'bismark': '0.24.2', 'expansionhunter': '5.0.0',
}
EXTENDED_TOOLS = {
    'multiqc': 'multiqc', 'trimmomatic': 'trimmomatic', 'bowtie2': 'bowtie2',
    'hisat2': 'hisat2', 'star': 'star', 'salmon': 'salmon',
    'sniffles2': 'sniffles', 'varscan2': 'varscan', 'delly': 'delly',
    'whatshap': 'whatshap', 'macs3': 'macs3', 'kraken2': 'kraken2',
    'crossmap.bed': 'crossmap', 'bam.bigwig': 'deeptools', 'cnvkit': 'cnvkit',
    'bismark': 'bismark', 'expansionhunter': 'expansionhunter',
}
CORE_PACKAGES = {'bwa-mem2':'2.2.1','gatk4':'4.6.2.0','picard':'3.4.0','samtools':'1.21','bcftools':'1.21','fastp':'0.24.0','fastqc':'0.12.1','minimap2':'2.28','freebayes':'1.3.6'}
CORE_PACKAGE_TOOLS = {**{t:t for t in ['bwa-mem2','picard','samtools','bcftools','fastp','fastqc','minimap2','freebayes']},
    **{t:'gatk4' for t in ['gatk-bqsr','gatk-apply-bqsr','haplotypecaller','mutect2','gatk.mt.call','gatk.mt.filter','mito.liftover']},
    'mito.extract':'samtools','mito.realign':'bwa-mem2','bcftools.annotate':'bcftools'}
PORTABLE_TOOLS = {'sam.bam','bam.sort','bam.index','bam.stats','vcf.bgzip','vcf.index','vcf.filter','vcf.stats','qc.summary','fastq.merge','fastq.split','bed.intervals','intervals.bed'}
ADJUNCT_TOOLS = {
    'mutect-filter': {'name': 'gatk4', 'version': '4.6.2.0'},
    'liftover.vcf': {'name': 'picard', 'version': '3.4.0'},
    'cohort.genotype': {'name': 'gatk4', 'version': '4.6.2.0'},
}
BLOCKERS = {
    'deepvariant': 'The version-pinned DeepVariant model/runtime has not been wrapped into the private worker contract or executed on a representative input.',
    'strelka2': 'The legacy Strelka Python environment and workflow outputs require a separate private runtime and tumor/germline qualification.',
    'manta': 'The legacy Manta workflow environment requires a separate private runtime and structural-variant positive-control qualification.',
    'vep': 'A matching Ensembl executable, cache release, FASTA and complete directory-bundle contract have not been provisioned and qualified.',
    'snpeff': 'The exact SnpEff executable and assembly database bundle have not been provisioned and qualified.',
    'scanpy': 'The Scanpy/Leiden environment is not published; H5AD loading alone does not establish analysis execution.',
    'seurat': 'The R/Seurat environment, explicit count-matrix bundle and complete analysis contract are not published.',
    'benchmark.happy': 'The hap.py/RTG runtime and representation-aware truth-region comparison remain unpublished; exact-allele comparison is a separate implemented operation.',
    'vg': 'A graph reference/index bundle and explicit VG mapping/genotyping contract are required; linear FASTA alignment is not a graph-analysis substitute.',
    'exomiser': 'The Exomiser executable, versioned phenotype/variant databases and proband/pedigree analysis contract are not published.',
    'intervar': 'InterVar requires separately permitted annotation dependencies and a validated ACMG evidence contract; a catalogue entry does not authorize those assets.',
    'dorado': 'Requires reviewed ONT software/model permissions, a compatible published GPU worker and POD5/model qualification.',
    'enformer': 'Requires permitted model weights, sequence-window assembly and a published model inference runtime; precomputed score lookup is not Enformer inference.',
    'annovar': 'Requires the user’s ANNOVAR authorization and exact compatible annotation database releases.',
    'sentieon': 'Requires a permitted vendor runtime, license-server contract and tool-specific execution qualification.',
    'dragen': 'Requires the licensed DRAGEN distribution and supported hardware/runtime; the generic CPU lane is not an equivalent execution environment.',
    'hgmd': 'Requires an authorized HGMD release and a vendor-specific permitted query contract.',
    'bcl.convert': 'Requires a reviewed BCL Convert distribution, sample sheet/run-folder contract and applicable vendor permission.',
    'cosmic': 'Requires authorized COSMIC data and a validated version-specific query contract.',
    'mutect-filter': 'The binary exists in the core image; the somatic contamination/orientation filtering contract is awaiting end-to-end qualification.',
    'liftover.vcf': 'The Picard binary exists in the core image; positive/negative chain and REF-validation cases are awaiting qualification.',
    'cohort.genotype': 'The GATK binary exists in the core image; multi-sample GVCF and pedigree/cohort qualification is awaiting completion.',
}

def validate_release(value):
    """Only successfully executed, reviewed commands become hosted tool routes."""
    if value.get('family') != 'extended-cpu-v1':
        raise ValueError('Unknown reviewed runtime family')
    if not re.fullmatch(r'[0-9]{12}\.dkr\.ecr\.us-east-1\.amazonaws\.com/openngs-demo@sha256:[a-f0-9]{64}', value.get('image', '')):
        raise ValueError('A private us-east-1 immutable worker image is required')
    if value.get('packages') != EXTENDED_PACKAGES:
        raise ValueError('Runtime package versions differ from the reviewed recipe')
    qualification = value.get('qualification', {})
    cases = qualification.get('cases', [])
    if not cases or len({x.get('tool') for x in cases}) != len(cases):
        raise ValueError('Unique scientific execution cases are required')
    for case in cases:
        if case.get('tool') not in EXTENDED_TOOLS:
            raise ValueError('Qualification contains an unreviewed tool')
        if case.get('status') == 'passed' and (not case.get('outputs') or not case.get('assertions')):
            raise ValueError('Tool output and scientific assertions are required')
    tools = sorted(x['tool'] for x in cases if x.get('status') == 'passed')
    if value.get('tools') != tools:
        raise ValueError('Published tools must exactly match passing execution cases')
    if not re.fullmatch('[a-f0-9]{64}', value.get('source_sha256', '')):
        raise ValueError('Source provenance is required')
    if 'core_qualification' in value or value.get('refreshed_core_tools'):
        from .provisioning import CORE_TOOLS
        core=value.get('core_qualification',{})
        if core.get('image')!=value['image'] or not re.fullmatch('[a-f0-9]{64}',core.get('source_sha256','')):
            raise ValueError('Core regression evidence must bind this exact worker image and audit source')
        cases=core.get('cases',[])
        if {c.get('tool') for c in cases}!=CORE_TOOLS or len(cases)!=len(CORE_TOOLS):
            raise ValueError('Every inherited core route needs a unique regression outcome')
        if any(c.get('status')=='passed' and (not c.get('outputs') or not c.get('assertions')) for c in cases):
            raise ValueError('Core regression requires executed outputs and assertions')
        if value.get('refreshed_core_tools')!=sorted(c['tool'] for c in cases if c.get('status')=='passed'):
            raise ValueError('Core refresh routes must match passing regression cases')
    if 'adjunct_qualification' in value or value.get('adjunct_tools'):
        proof=value.get('adjunct_qualification',{})
        cases=proof.get('cases',[])
        if proof.get('image')!=value['image'] or not re.fullmatch('[a-f0-9]{64}',proof.get('source_sha256','')):
            raise ValueError('Additional native contracts require exact image-bound audit provenance')
        if {c.get('tool') for c in cases}!=set(ADJUNCT_TOOLS) or len(cases)!=len(ADJUNCT_TOOLS):
            raise ValueError('Additional native contract outcomes must cover the reviewed set exactly')
        if any(c.get('status')=='passed' and (not c.get('outputs') or not c.get('assertions')) for c in cases):
            raise ValueError('Additional native contracts require real outputs and assertions')
        if value.get('adjunct_tools')!=sorted(c['tool'] for c in cases if c.get('status')=='passed'):
            raise ValueError('Additional routes must exactly match passing command contracts')
    if 'portable_qualification' in value or value.get('portable_tools'):
        proof=value.get('portable_qualification',{});cases=proof.get('cases',[])
        if proof.get('image')!=value['image'] or not re.fullmatch('[a-f0-9]{64}',proof.get('source_sha256','')):
            raise ValueError('Portable regressions require exact image-bound audit provenance')
        if {c.get('tool') for c in cases}!=PORTABLE_TOOLS or len(cases)!=len(PORTABLE_TOOLS):
            raise ValueError('Portable regression outcomes must cover the reviewed set exactly')
        if any(c.get('status')=='passed' and (not c.get('outputs') or not c.get('assertions')) for c in cases):
            raise ValueError('Portable regressions require actual outputs and assertions')
        if value.get('portable_tools')!=sorted(c['tool'] for c in cases if c.get('status')=='passed'):
            raise ValueError('Portable routes must match passing round-trip regressions')
    return value

def published():
    row = db.query("SELECT value FROM controls WHERE key='runtime-release:extended-cpu-v1'", one=True)
    if not row:
        return None
    try:
        return validate_release(json.loads(row['value']))
    except (ValueError, TypeError, KeyError):
        # Invalid publication must never turn catalogue declarations into approval.
        return None
