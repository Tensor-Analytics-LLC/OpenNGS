"""Real round-trip regressions of portable operators in the frozen worker image."""
import gzip
import hashlib
import json
from pathlib import Path
import sys
import time
import pysam
from backend.portable import execute,artifact

F=Path('/qualification/fixtures');D=Path('/opt/studio/demo-data');cases=[]

def run(tool,inputs,check,params=None):
    began=time.monotonic();work=Path('/work')/tool;work.mkdir()
    record={'tool':tool,'scope':'Known-count and lossless round-trip engineering checks'}
    try:
        out=execute({'tool':tool,'inputs':inputs,'workdir':str(work),'params':params or {},'threads':2})
        checks=check(out);files=[Path(x['path']) for rows in out.values() for x in rows]
        assert checks and files
        record.update(status='passed',assertions=checks,outputs=[{'name':p.name,'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in files])
    except Exception as error:record.update(status='failed',error=str(error)[-1800:]);out=None
    record['seconds']=round(time.monotonic()-began,3);cases.append(record);print(json.dumps({'tool':tool,'status':record['status'],'error':record.get('error')}),flush=True)
    if out is None:raise RuntimeError('Dependent portable regression failed: '+tool)
    return out

def bam(out):
    with pysam.AlignmentFile(out['alignment'][0]['path']) as handle:reads=list(handle.fetch(until_eof=True))
    assert len(reads)==2000 and sum(not r.is_unmapped for r in reads)==2000
    return {'read_records':2000,'mapped_records':2000}

def vcf(out):
    with pysam.VariantFile(out['variants'][0]['path']) as handle:rows=list(handle)
    assert [r.pos for r in rows]==[1000,1010]
    return {'preserved_variant_positions':[1000,1010]}

def report(out,key,value):
    doc=json.loads(Path(out['report'][0]['path']).read_text());assert doc[key]==value,doc
    return {key:value}

def main():
    source={'alignment':[artifact(F/'reads.sam','SAM')]}
    converted=run('sam.bam',source,bam)
    ordered=run('bam.sort',converted,bam)
    def indexed(out):
        check=bam(out)
        with pysam.AlignmentFile(out['alignment'][0]['path']) as handle:assert handle.has_index()
        return {**check,'index_available':True}
    idx=run('bam.index',ordered,indexed)
    run('bam.stats',idx,lambda x:report(x,'records',2000))
    variants={'variants':[artifact(F/'heterozygous.vcf','VCF')]}
    compressed=run('vcf.bgzip',variants,vcf)
    def indexed_vcf(out):
        check=vcf(out)
        with pysam.VariantFile(out['variants'][0]['path']) as handle:assert len(list(handle.fetch('engineering_contig',999,1011)))==2
        return {**check,'indexed_locus_query_records':2}
    run('vcf.index',compressed,indexed_vcf)
    run('vcf.filter',variants,vcf,{'minimum_quality':0,'pass_only':False})
    run('vcf.stats',variants,lambda x:report(x,'records',2))
    reads={'reads':[artifact(D/'sample.fastq/fixture_R1.fastq.gz','FASTQ_PAIR',mate=str(D/'sample.fastq/fixture_R2.fastq.gz'))]}
    run('qc.summary',reads,lambda x:report(x,'total_reads',2000),{'max_reads':0})
    def merged_check(out):
        with gzip.open(out['reads'][0]['path'],'rt') as handle:assert sum(1 for _ in handle)==8000
        return {'interleaved_records':2000}
    merged=run('fastq.merge',reads,merged_check)
    def split_check(out):
        for a,b in [(out['reads'][0]['path'],reads['reads'][0]['path']),(out['reads'][0]['metadata']['mate'],reads['reads'][0]['metadata']['mate'])]:
            with gzip.open(a,'rb') as left,gzip.open(b,'rb') as right:assert left.read()==right.read()
        return {'both_mates_preserved_byte_for_byte_uncompressed':True,'pairs':1000}
    run('fastq.split',merged,split_check)
    bed={'intervals':[artifact(F/'interval.bed','BED')],'reference':[artifact(F/'reference.fa','FASTA')]}
    def interval_check(out):
        text=Path(out['intervals'][0]['path']).read_text();assert 'engineering_contig\t101\t200\t' in text
        return {'bed_zero_based_start':100,'interval_one_based_start':101,'end':200}
    intervals=run('bed.intervals',bed,interval_check)
    def bed_check(out):
        assert Path(out['intervals'][0]['path']).read_text().strip()=='engineering_contig\t100\t200'
        return {'zero_based_half_open_round_trip':[100,200]}
    run('intervals.bed',intervals,bed_check)
    Path(sys.argv[1]).write_text(json.dumps({'schema':1,'scope':'Thirteen portable command contracts; no scientific scale or accuracy claim','cases':cases},indent=2))

if __name__=='__main__':main()
