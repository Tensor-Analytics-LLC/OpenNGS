"""Full-input research blueprints. Bind exact assets and a funded execution profile."""
def extend(make):
    result=[]
    def germline(wes):
        tools=['asset.file','reference.asset','asset.file','asset.file','fastqc','bwa-mem2','bam.sort','picard','bam.index','gatk-bqsr','gatk-apply-bqsr','bam.index','haplotypecaller','bcftools.annotate','vcf.stats','utility.export']
        links=[(0,'data',4,'reads'),(0,'data',5,'reads'),(1,'reference',5,'reference'),(5,'alignment',6,'alignment'),(6,'alignment',7,'alignment'),(7,'alignment',8,'alignment'),(8,'alignment',9,'alignment'),(1,'reference',9,'reference'),(2,'data',9,'known_sites'),(8,'alignment',10,'alignment'),(1,'reference',10,'reference'),(9,'report',10,'recalibration'),(10,'alignment',11,'alignment'),(11,'alignment',12,'alignment'),(1,'reference',12,'reference'),(12,'variants',13,'variants'),(1,'reference',13,'reference'),(3,'data',13,'database'),(13,'variants',14,'variants'),(13,'variants',15,'data'),(14,'report',15,'data'),(4,'report',15,'data'),(7,'report',15,'data')]
        if wes:
            tools+=['asset.file','wes.capture','wes.coverage'];links +=[(16,'data',17,'targets'),(1,'reference',17,'reference'),(17,'calling',12,'intervals'),(11,'alignment',18,'alignment'),(17,'targets',18,'targets'),(18,'report',15,'data'),(17,'report',15,'data')]
        template=make('research-wes' if wes else 'research-wgs','WES · capture-aware research' if wes else 'WGS · full-input germline research','Real QC, alignment, duplicate marking, BQSR, calling, allele annotation and research exports. Bind exact reads/reference/known-sites/annotation assets; select correct ploidy/intervals for the assay. '+('Capture kit/version and target BED are mandatory; coverage is measured on unpadded targets.' if wes else 'Mitochondrial analysis uses the separate NUMT/circular-reference blueprint. Sex-chromosome/ploidy choices and SV/CNV branches require assay-specific configuration.'),tools,links)
        for node in template['graph']['nodes']:
            if node['tool'].startswith(('asset.','reference.')):node['resources']={'threads':1,'memory_mb':1024,'scratch_mb':2048,'timeout_seconds':60}
            elif node['tool']=='bwa-mem2':node['resources']={'threads':16,'memory_mb':65536,'scratch_mb':1024000,'timeout_seconds':43200}
            elif node['tool'] in ['bam.sort','picard','gatk-bqsr','gatk-apply-bqsr','haplotypecaller']:node['resources']={'threads':8,'memory_mb':32768,'scratch_mb':1024000,'timeout_seconds':43200}
            else:node['resources']={'threads':2,'memory_mb':8192,'scratch_mb':204800,'timeout_seconds':14400}
        template['validation_status']='Not full-scale validated; dependencies and funded worker profiles required'
        return template
    result.extend([germline(False),germline(True)])
    tools=['asset.file','reference.asset','asset.file','mito.extract','mito.shift-reference','mito.realign','mito.realign','gatk.mt.call','gatk.mt.call','gatk.mt.filter','gatk.mt.filter','mito.liftover','mito.reconcile','mt.qc','utility.export','mito.regions']
    links=[(0,'data',3,'alignment'),(2,'data',15,'regions'),(1,'reference',15,'reference'),(15,'regions',3,'regions'),(1,'reference',4,'reference'),(3,'alignment',5,'alignment'),(4,'original',5,'reference'),(3,'alignment',6,'alignment'),(4,'shifted',6,'reference'),(5,'alignment',7,'alignment'),(4,'original',7,'reference'),(6,'alignment',8,'alignment'),(4,'shifted',8,'reference'),(7,'variants',9,'variants'),(7,'statistics',9,'statistics'),(4,'original',9,'reference'),(8,'variants',10,'variants'),(8,'statistics',10,'statistics'),(4,'shifted',10,'reference'),(10,'variants',11,'variants'),(4,'chain',11,'chain'),(4,'original',11,'reference'),(9,'variants',12,'original'),(11,'variants',12,'shifted_back'),(4,'original',12,'reference'),(0,'data',13,'alignment'),(1,'reference',13,'reference'),(12,'variants',14,'data'),(12,'report',14,'data'),(11,'rejected',14,'data'),(13,'report',14,'data'),(4,'report',14,'data')]
    template=make('research-mtdna','mtDNA · circular/NUMT-aware research','Bind whole-genome indexed alignments, exact whole-genome reference, and explicit mitochondrial+nuclear candidate intervals. Realign on original/rotated mtDNA references, call/filter in mitochondrial mode, lift boundary calls back and reconcile. Heteroplasmy sensitivity, contamination and NUMT discrimination require representative validation; no clinical claim.',tools,links)
    for node in template['graph']['nodes']:node['resources']={'threads':4,'memory_mb':16384,'scratch_mb':102400,'timeout_seconds':14400}
    template['validation_status']='Coordinate operators unit-tested; native end-to-end and heteroplasmy/NUMT performance unverified'
    result.append(template)
    return result
