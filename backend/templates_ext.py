"""Editable scientific workflow blueprints; native-runtime readiness is checked separately."""
def extend(make):
    result=[]
    result.append(make('deepvariant','DeepVariant · germline DNA','Bind paired reads and a matching reference, then execute an approved DeepVariant model/runtime. This is a configurable research blueprint, not an independently validated assay.',
        ['asset.file','reference.asset','bwa-mem2','bam.sort','bam.index','deepvariant','vcf.stats','utility.export'],
        [(0,'data',2,'reads'),(1,'reference',2,'reference'),(2,'alignment',3,'alignment'),(3,'alignment',4,'alignment'),(4,'alignment',5,'alignment'),(1,'reference',5,'reference'),(5,'variants',6,'variants'),(5,'variants',7,'data')]))
    result.append(make('somatic','Somatic · tumor / normal','Explicit tumor/normal read groups and a matched reference. Mutect2 emits unfiltered calls; connect its statistics to FilterMutectCalls and bind assay-specific contamination/orientation data before interpretation.',
        ['asset.file','asset.file','reference.asset','bwa-mem2','bwa-mem2','bam.sort','bam.sort','bam.index','bam.index','mutect2','utility.export'],
        [(0,'data',3,'reads'),(1,'data',4,'reads'),(2,'reference',3,'reference'),(2,'reference',4,'reference'),(3,'alignment',5,'alignment'),(4,'alignment',6,'alignment'),(5,'alignment',7,'alignment'),(6,'alignment',8,'alignment'),(7,'alignment',9,'tumor'),(8,'alignment',9,'normal'),(2,'reference',9,'reference'),(9,'variants',10,'data')]))
    result[-1]['graph']['nodes'][3]['params']['sample_id']='TUMOR';result[-1]['graph']['nodes'][4]['params']['sample_id']='NORMAL'
    result.append(make('long-read','Long-read · structural discovery','Bind Nanopore/PacBio reads and a reference. Select map-ont or map-hifi to match the assay; Sniffles support filters require dataset validation.',
        ['asset.file','reference.asset','minimap2','bam.sort','bam.index','sniffles2','utility.export'],
        [(0,'data',2,'reads'),(1,'reference',2,'reference'),(2,'alignment',3,'alignment'),(3,'alignment',4,'alignment'),(4,'alignment',5,'alignment'),(1,'reference',5,'reference'),(5,'variants',6,'data')]))
    result[-1]['graph']['nodes'][2]['params']['preset']='map-ont'
    result.append(make('bulk-rna','Bulk RNA-seq · quantification','Paired reads → quality summary and Salmon quantification. Bind a separately versioned Salmon transcript index. Differential-expression modelling is a distinct cohort analysis.',
        ['asset.file','asset.file','qc.summary','salmon','utility.export'],
        [(0,'data',2,'reads'),(0,'data',3,'reads'),(1,'data',3,'reference'),(3,'quantification',4,'data'),(2,'report',4,'data')]))
    result.append(make('single-cell','Single cell · AnnData analysis','Bind an H5AD count matrix for normalization, PCA, neighbourhood graph, Leiden and UMAP. Biology, batch correction and filtering require dataset-specific choices.',
        ['asset.file','scanpy','utility.export'],[(0,'data',1,'counts'),(1,'embedding',2,'data')]))
    result.append(make('giab-validation','GIAB · haplotype-aware validation','Bind query VCF, matching NIST truth VCF, confident BED and exact reference. Run the approved hap.py/vcfeval image. This template does not certify clinical accuracy.',
        ['asset.file','asset.file','asset.file','reference.asset','benchmark.happy','utility.export'],
        [(0,'data',4,'query'),(1,'data',4,'truth'),(2,'data',4,'regions'),(3,'reference',4,'reference'),(4,'summary',5,'data'),(4,'extended',5,'data')]))
    return result
