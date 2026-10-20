"""Exercise inherited core commands against an already immutable worker image.

The application and scientific binaries are imported from the image. This
external audit script cannot replace their code, and runs without networking.
"""
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import sys
import pysam
import qualify_extended_runtime as q
from backend.provisioning import CORE_TOOLS

def main():
    q.FIXTURES=Path('/tmp/core-fixtures')
    shutil.copytree('/qualification/fixtures',q.FIXTURES,dirs_exist_ok=True)
    q.CASES.clear()
    f=q.FIXTURES
    q.command(['samtools','dict','-o',f/'reference.dict',f/'reference.fa'])
    base=q.inputs()
    base['reference'][0]['metadata']['dictionary']=str(f/'reference.dict')
    pysam.tabix_compress(str(f/'heterozygous.vcf'),str(f/'known.vcf.gz'),force=True)
    pysam.tabix_index(str(f/'known.vcf.gz'),preset='vcf',force=True)
    known=q.artifact(f/'known.vcf.gz','INDEXED_VCF',index=str(f/'known.vcf.gz.tbi'))
    paired={'reads':[q.artifact(q.ROOT/'sample.rna/sample_R1_1.fq.gz','FASTQ_PAIR',
                      mate=str(q.ROOT/'sample.rna/sample_R1_2.fq.gz'))]}
    original_count=len(gzip.open(q.ROOT/'sample.rna/sample_R1_1.fq.gz','rt').readlines())//4
    def qc(files):
        assert len([p for p in files if p.suffix=='.zip'])==2
        assert len([p for p in files if p.suffix=='.html'])==2
        return {'public_rna_read_pairs':original_count,'machine_and_human_reports':4}
    q.execute('fastqc',paired,assertion=qc)
    def trimmed(files):
        report=json.loads(next(p for p in files if p.name=='fastp.json').read_text())
        before=report['summary']['before_filtering']['total_reads']
        assert before==original_count*2
        return {'public_rna_input_reads':before,'retained_reads':report['summary']['after_filtering']['total_reads']}
    q.execute('fastp',paired,assertion=trimmed)
    for tool in ['bwa-mem2','minimap2']:
        q.execute(tool,{k:base[k] for k in ['reads','reference']},
                  {'preset':'sr'} if tool=='minimap2' else None,assertion=q.alignments)
    q.execute('samtools',{'alignment':base['alignment']},assertion=lambda files:
        {'statistics_bytes':sum(p.stat().st_size for p in files)} if all(p.stat().st_size for p in files) else None)
    for tool in ['bcftools','freebayes']:
        q.execute(tool,{k:base[k] for k in ['alignment','reference']},assertion=q.variants)
    q.execute('picard',{'alignment':base['alignment']},assertion=q.alignments)
    q.execute('gatk-bqsr',{**{k:base[k] for k in ['alignment','reference']},'known_sites':[known]},
              assertion=lambda files:{'recalibration_tables':sum('RecalTable' in p.read_text() for p in files)} if
              any('RecalTable' in p.read_text() for p in files) else None)
    if q.CASES[-1]['status']=='passed':
        shutil.copyfile('/work/recal.table',f/'recal.table')
        q.execute('gatk-apply-bqsr',{**{k:base[k] for k in ['alignment','reference']},
                  'recalibration':[q.artifact(f/'recal.table','RECAL')]},assertion=q.alignments)
    else:
        q.CASES.append({'tool':'gatk-apply-bqsr','status':'failed','error':'Upstream recalibration qualification failed'})
    q.execute('haplotypecaller',{k:base[k] for k in ['alignment','reference']},assertion=q.variants)
    q.command(['samtools','addreplacerg','-r','ID:NORMAL','-r','SM:NORMAL','-r','PL:ILLUMINA',
               '-o',f/'normal.bam',f/'reads.bam'])
    q.command(['samtools','index',f/'normal.bam'])
    q.execute('mutect2',{'tumor':base['alignment'],'normal':[q.artifact(f/'normal.bam','INDEXED_BAM',
              index=str(f/'normal.bam.bai'))],'reference':base['reference']},assertion=q.variants)
    q.execute('gatk.mt.call',{k:base[k] for k in ['alignment','reference']},assertion=q.variants)
    if q.CASES[-1]['status']=='passed':
        for filename in ['mitochondrial.vcf','mitochondrial.vcf.stats']:
            shutil.copyfile('/work/'+filename,f/filename)
        q.execute('gatk.mt.filter',{'variants':[q.artifact(f/'mitochondrial.vcf','VCF')],
                 'statistics':[q.artifact(f/'mitochondrial.vcf.stats','REPORT')],
                 'reference':base['reference']},assertion=q.variants)
    else:
        q.CASES.append({'tool':'gatk.mt.filter','status':'failed','error':'Upstream mitochondrial caller qualification failed'})
    (f/'regions.bed').write_text('engineering_contig\t0\t20000\n')
    q.execute('mito.extract',{'alignment':base['alignment'],
             'regions':[q.artifact(f/'regions.bed','BED')]},assertion=q.alignments)
    q.execute('mito.realign',{k:base[k] for k in ['alignment','reference']},assertion=q.alignments)
    def lifted(files):
        path=next(p for p in files if p.name=='shifted-back.vcf')
        with pysam.VariantFile(str(path)) as source:
            positions=[r.pos for r in source]
        assert positions==[1000,1010]
        return {'identity_liftover_preserved_positions':positions,'not_an_assembly_conversion_benchmark':True}
    q.execute('mito.liftover',{'variants':[q.artifact(f/'heterozygous.vcf','VCF')],
             'chain':[q.artifact(f/'identity.chain','CHAIN')],'reference':base['reference']},assertion=lifted)
    original=(f/'heterozygous.vcf').read_text()
    database=original.replace('#CHROM','##INFO=<ID=TESTANN,Number=1,Type=String,Description="Engineering annotation">\n#CHROM')
    database=database.replace('\tPASS\t.\tGT','\tPASS\tTESTANN=KNOWN\tGT')
    (f/'annotation.vcf').write_text(database)
    pysam.tabix_compress(str(f/'annotation.vcf'),str(f/'annotation.vcf.gz'),force=True)
    pysam.tabix_index(str(f/'annotation.vcf.gz'),preset='vcf',force=True)
    def annotated(files):
        with pysam.VariantFile(str(next(p for p in files if p.name=='annotated.vcf.gz'))) as source:
            records=list(source)
        assert len(records)==2 and all(r.info['TESTANN']=='KNOWN' for r in records)
        return {'exact_allele_annotations':2}
    q.execute('bcftools.annotate',{'variants':[q.artifact(f/'heterozygous.vcf','VCF')],
             'database':[q.artifact(f/'annotation.vcf.gz','INDEXED_VCF',index=str(f/'annotation.vcf.gz.tbi'))],
             'reference':base['reference']},{'columns':'INFO/TESTANN'},assertion=annotated)
    if {c['tool'] for c in q.CASES}!=CORE_TOOLS:raise ValueError('Core qualification does not cover the declared core routes')
    report={'schema':1,'cases':q.CASES,
            'scope':'Frozen inherited core command regression on engineering controls and public bacterial RNA reads; not full-scale or clinical validation.',
            'environment_sha256':hashlib.sha256(Path('/opt/studio/environment-explicit.lock').read_bytes()).hexdigest(),
            'public_rna_input_pairs':original_count}
    Path(sys.argv[1]).write_text(json.dumps(report,indent=2))
    print(json.dumps({'passed':sum(c['status']=='passed' for c in q.CASES),'total':len(q.CASES)}),flush=True)

if __name__=='__main__':main()
