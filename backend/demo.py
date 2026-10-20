"""Hosted public-data edition. Self-hosted installations retain the full registry."""
import os
from .config import DEMO

SOURCES={'sample.fastq','sample.rna','sample.giab','sample.somatic','sample.clinvar','sample.alignments','reference.tiny'}
TOOLS=SOURCES|{'qc.summary','vcf.stats','bam.stats','sam.bam','bam.sort','bam.index','vcf.bgzip','vcf.index','vcf.filter','fastq.split','fastq.merge','utility.gather','utility.scatter','utility.condition','utility.export','fastqc','fastp','minimap2','bowtie2','hisat2','samtools','bcftools','freebayes'}

def enabled(spec,uid=None):
    from .entitlements import restricted
    return not (restricted(uid) if uid else DEMO) or spec['id'] in TOOLS

def validate(g,uid=None):
    from .entitlements import restricted
    if not (restricted(uid) if uid else DEMO):return
    if len(g['nodes'])>24:raise ValueError('The hosted sample edition supports 24 nodes per pipeline. The self-hosted edition supports larger graphs.')
    for n in g['nodes']:
        if n['tool'] not in TOOLS:raise ValueError('Choose a hosted sample tool; this node is available in the self-hosted catalogue: '+n['tool'])
        r=n.get('resources',{})
        if any(type(r.get(k,default))!=int for k,default in [('threads',2),('memory_mb',4096),('timeout_seconds',120)]):raise ValueError('Hosted resource limits must be integers')
        if r.get('threads',2)>2 or r.get('memory_mb',4096)>4096 or r.get('timeout_seconds',120)>120:raise ValueError('Hosted sample resources are bounded to 2 vCPU, 4 GiB and 120 seconds per node')
        if n.get('advanced') or n.get('params',{}).get('extra'):raise ValueError('Custom CLI overrides are enabled in the self-hosted edition; hosted nodes expose their reviewed scientific parameters')
        from .registry import BASE
        allowed={p['key'] for p in BASE[n['tool']]['parameters']}
        if set(n.get('params',{}))-allowed:raise ValueError('Unrecognized sample parameter; dataset sources and versions are controlled by the platform')
    if not any(n['tool'] in SOURCES for n in g['nodes']):raise ValueError('Start with a provided sample dataset')

def public_info(uid=None):
    from .entitlements import restricted,limits
    limited=restricted(uid) if uid else DEMO
    return {'enabled':limited,'uploads_enabled':not limited,'tier':limits(uid) if uid else None,'dataset_policy':'Curated, bounded public and clearly labelled synthetic sample slices only' if limited else 'Workspace datasets',
        'max_nodes':24 if limited else 2000,'queue_policy':'Accepted work is preserved; allowance exhaustion is explained before new paid work starts',
        'self_host_url':os.getenv('NGS_SOURCE_URL','https://github.com/Tensor-Analytics-LLC/OpenNGS'),'maximum_output_bytes':64*1024*1024}
