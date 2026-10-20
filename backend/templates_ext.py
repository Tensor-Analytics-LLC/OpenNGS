"""Editable research workflows with explicit cloud inputs and scientific scope."""


def extend(make):
    result = []
    result.append(make(
        'deepvariant', 'DeepVariant · germline DNA',
        'Bind paired reads and a matching reference. The approved DeepVariant model/runtime must be available before execution. This blueprint is not an independently validated assay.',
        ['asset.file', 'reference.asset', 'bwa-mem2', 'bam.sort', 'bam.index', 'deepvariant', 'vcf.stats', 'utility.export'],
        [(0, 'data', 2, 'reads'), (1, 'reference', 2, 'reference'), (2, 'alignment', 3, 'alignment'),
         (3, 'alignment', 4, 'alignment'), (4, 'alignment', 5, 'alignment'), (1, 'reference', 5, 'reference'),
         (5, 'variants', 6, 'variants'), (5, 'variants', 7, 'data'), (6, 'report', 7, 'data')]))
    result.append(make(
        'somatic', 'Somatic · tumor / normal',
        'Bind distinct tumor and normal paired reads and the exact reference. QC, read-group-aware alignment, duplicate marking, Mutect2 and matched-statistics filtering are wired. Connect assay-specific contamination/orientation evidence before interpretation; this template does not estimate those quantities.',
        ['asset.file', 'asset.file', 'reference.asset', 'fastqc', 'fastqc', 'bwa-mem2', 'bwa-mem2',
         'bam.sort', 'bam.sort', 'picard', 'picard', 'bam.index', 'bam.index',
         'mutect2', 'mutect-filter', 'vcf.stats', 'multiqc', 'utility.export'],
        [(0, 'data', 3, 'reads'), (1, 'data', 4, 'reads'),
         (0, 'data', 5, 'reads'), (1, 'data', 6, 'reads'),
         (2, 'reference', 5, 'reference'), (2, 'reference', 6, 'reference'),
         (5, 'alignment', 7, 'alignment'), (6, 'alignment', 8, 'alignment'),
         (7, 'alignment', 9, 'alignment'), (8, 'alignment', 10, 'alignment'),
         (9, 'alignment', 11, 'alignment'), (10, 'alignment', 12, 'alignment'),
         (11, 'alignment', 13, 'tumor'), (12, 'alignment', 13, 'normal'),
         (2, 'reference', 13, 'reference'), (13, 'variants', 14, 'variants'),
         (13, 'statistics', 14, 'statistics'), (2, 'reference', 14, 'reference'),
         (14, 'variants', 15, 'variants'), (3, 'report', 16, 'reports'),
         (4, 'report', 16, 'reports'), (9, 'report', 16, 'reports'), (10, 'report', 16, 'reports'),
         (14, 'variants', 17, 'data'), (15, 'report', 17, 'data'), (16, 'report', 17, 'data')]))
    result[-1]['graph']['nodes'][5]['params']['sample_id'] = 'TUMOR'
    result[-1]['graph']['nodes'][6]['params']['sample_id'] = 'NORMAL'
    result.append(make(
        'long-read', 'Long-read · structural discovery',
        'Bind Nanopore/PacBio reads and a reference. Choose map-ont or map-hifi for the assay, then sort/index and call with Sniffles2. Support filters require dataset-specific validation.',
        ['asset.file', 'reference.asset', 'minimap2', 'bam.sort', 'bam.index', 'sniffles2', 'utility.export'],
        [(0, 'data', 2, 'reads'), (1, 'reference', 2, 'reference'), (2, 'alignment', 3, 'alignment'),
         (3, 'alignment', 4, 'alignment'), (4, 'alignment', 5, 'alignment'),
         (1, 'reference', 5, 'reference'), (5, 'variants', 6, 'data')]))
    result[-1]['graph']['nodes'][2]['params']['preset'] = 'map-ont'
    result.append(make(
        'bulk-rna', 'Bulk RNA-seq · transcript quantification',
        'Paired reads → complete read-quality summary and Salmon quantification. Bind the exact transcript FASTA or complete Salmon index bundle; add matching decoy names when using a gentrome. Differential expression requires a separate replicated cohort analysis.',
        ['asset.file', 'asset.file', 'qc.summary', 'salmon', 'utility.export'],
        [(0, 'data', 2, 'reads'), (0, 'data', 3, 'reads'), (1, 'data', 3, 'reference'),
         (3, 'quantification', 4, 'data'), (2, 'report', 4, 'data')]))
    result[-1]['graph']['nodes'][2]['params']['max_reads'] = 0
    result.append(make(
        'single-cell', 'Single cell · AnnData analysis',
        'Bind an H5AD raw-count matrix for normalization, PCA, neighbourhood graph, Leiden and UMAP. Confirm that the input is counts, not an already log-transformed matrix; batch correction, cell QC and interpretation require dataset-specific choices.',
        ['asset.file', 'scanpy', 'utility.export'], [(0, 'data', 1, 'counts'), (1, 'embedding', 2, 'data')]))
    result.append(make(
        'giab-validation', 'GIAB · haplotype-aware benchmarking',
        'Bind query VCF, matching NIST truth VCF, confident BED and the exact reference. hap.py/vcfeval reports precision/recall within those regions. An engineering qualification does not establish assay accuracy.',
        ['asset.file', 'asset.file', 'asset.file', 'reference.asset', 'benchmark.happy', 'utility.export'],
        [(0, 'data', 4, 'query'), (1, 'data', 4, 'truth'), (2, 'data', 4, 'regions'),
         (3, 'reference', 4, 'reference'), (4, 'summary', 5, 'data'), (4, 'extended', 5, 'data')]))
    result.append(make(
        'research-read-qc', 'Read QC · your paired dataset',
        'Bind a complete cloud paired-read dataset. FastQC diagnoses raw reads, fastp trims with explicit parameters, and MultiQC collects their native reports. The quality summary scans the complete trimmed input.',
        ['asset.file', 'fastqc', 'fastp', 'qc.summary', 'multiqc', 'utility.export'],
        [(0, 'data', 1, 'reads'), (0, 'data', 2, 'reads'), (2, 'reads', 3, 'reads'),
         (1, 'report', 4, 'reports'), (2, 'report', 4, 'reports'),
         (2, 'reads', 5, 'data'), (3, 'report', 5, 'data'), (4, 'report', 5, 'data')]))
    result[-1]['graph']['nodes'][3]['params']['max_reads'] = 0
    result.append(make(
        'research-variant-review', 'Variant filtering · your callset',
        'Bind a cloud VCF and choose explicit contig, QUAL and FILTER controls. Publish retained records and actual summary statistics for the workbench. Annotation fields must already be present; no clinical classifications are inferred.',
        ['asset.file', 'vcf.filter', 'vcf.stats', 'utility.export'],
        [(0, 'data', 1, 'variants'), (1, 'variants', 2, 'variants'),
         (1, 'variants', 3, 'data'), (2, 'report', 3, 'data')]))
    result.append(make(
        'research-cohort', 'Cohort · joint germline genotyping',
        'Bind two distinct sample gVCFs and the exact reference; add further gVCFs to the joint-genotyping port as needed. GATK combines and genotypes them. This does not infer pedigree relationships or perform population-scale GenomicsDB optimization.',
        ['asset.file', 'asset.file', 'reference.asset', 'cohort.genotype', 'vcf.stats', 'utility.export'],
        [(0, 'data', 3, 'gvcfs'), (1, 'data', 3, 'gvcfs'), (2, 'reference', 3, 'reference'),
         (3, 'variants', 4, 'variants'), (3, 'variants', 5, 'data'), (4, 'report', 5, 'data')]))
    result.append(make(
        'research-metagenomics', 'Metagenomics · taxonomic classification',
        'Bind reads and a versioned Kraken2 database bundle containing matching hash, options and taxonomy files. Classify complete reads using the explicit confidence setting. The report is not a clinical pathogen diagnosis.',
        ['asset.file', 'database.asset', 'kraken2', 'utility.export'],
        [(0, 'data', 2, 'reads'), (1, 'database', 2, 'database'), (2, 'report', 3, 'data')]))
    # These are starting allocations, not a promise that every input size fits.
    # Admission reserves the maxima; no stage silently truncates data to fit.
    for template in result:
        for node in template['graph']['nodes']:
            if node['tool'] not in ['asset.file', 'reference.asset', 'database.asset', 'utility.export']:
                node['resources'] = {'threads': 2, 'memory_mb': 8192, 'scratch_mb': 20480, 'timeout_seconds': 3600}
        template['validation_status'] = 'Small-data operator evidence is reported in the catalogue; the complete workflow and full-scale scientific performance require representative validation.'
    return result
