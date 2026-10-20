"""Qualify three existing core-binary contracts on the frozen scientific image."""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import pysam
import qualify_extended_runtime as q

def main():
    q.FIXTURES=Path('/tmp/adjunct-fixtures')
    shutil.copytree('/qualification/fixtures',q.FIXTURES,dirs_exist_ok=True)
    f=q.FIXTURES
    q.command(['samtools','dict','-o',f/'reference.dict',f/'reference.fa'])
    base=q.inputs();base['reference'][0]['metadata']['dictionary']=str(f/'reference.dict')
    q.command(['samtools','addreplacerg','-r','ID:NORMAL','-r','SM:NORMAL','-r','PL:ILLUMINA',
               '-o',f/'normal.bam',f/'reads.bam'])
    q.command(['samtools','index',f/'normal.bam'])
    q.command(['gatk','--java-options','-Xmx1024m','Mutect2','-R',f/'reference.fa',
               '-I',f/'reads.bam','-I',f/'normal.bam','-normal','NORMAL','-O',f/'somatic.vcf'])
    q.execute('mutect-filter',{'variants':[q.artifact(f/'somatic.vcf','VCF')],
        'statistics':[q.artifact(f/'somatic.vcf.stats','REPORT')],'reference':base['reference']},
        assertion=q.variants)
    def lifted(files):
        with pysam.VariantFile(str(next(p for p in files if p.name=='lifted.vcf'))) as source:
            positions=[r.pos for r in source]
        with pysam.VariantFile(str(next(p for p in files if p.name=='rejected.vcf'))) as source:
            rejected=list(source)
        assert positions==[1000,1010] and not rejected
        return {'preserved_positions':positions,'rejected_variants':0,'scope':'Identity chain only; inter-assembly liftover needs separate validation'}
    q.execute('liftover.vcf',{'variants':[q.artifact(f/'heterozygous.vcf','VCF')],
        'chain':[q.artifact(f/'identity.chain','CHAIN')],'reference':base['reference']},
        {'source_assembly':'engineering','target_assembly':'engineering'},assertion=lifted)
    for label,bam in [('SAMPLE','reads.bam'),('NORMAL','normal.bam')]:
        q.command(['gatk','--java-options','-Xmx1024m','HaplotypeCaller','-R',f/'reference.fa',
                   '-I',f/bam,'-ERC','GVCF','-L','engineering_contig:900-1100','-O',f/(label+'.g.vcf')])
    def genotyped(files):
        with pysam.VariantFile(str(files[0])) as source:
            samples=list(source.header.samples);records=list(source)
        assert set(samples)=={'SAMPLE','NORMAL'} and len(records)>0
        assert any(all(r.samples[s]['GT']==(0,0) for s in samples) for r in records)
        return {'sample_names':samples,'joint_genotype_records':len(records),
                'observed_homozygous_reference_genotypes':True,
                'scope':'Two matched engineering samples; no pedigree or variant sensitivity claim'}
    q.execute('cohort.genotype',{'gvcfs':[q.artifact(f/(s+'.g.vcf'),'VCF',
        index=str(f/(s+'.g.vcf.idx'))) for s in ['SAMPLE','NORMAL']],'reference':base['reference']},
        advanced='--include-non-variant-sites',assertion=genotyped)
    if {c['tool'] for c in q.CASES}!={'mutect-filter','liftover.vcf','cohort.genotype'}:
        raise ValueError('Incomplete adjunct contract audit')
    report={'schema':1,'cases':q.CASES,
        'scope':'Real core-binary command contracts on explicit tiny engineering controls; no assay accuracy or cohort-scale validation',
        'environment_sha256':hashlib.sha256(Path('/opt/studio/environment-explicit.lock').read_bytes()).hexdigest()}
    Path(sys.argv[1]).write_text(json.dumps(report,indent=2))
    print(json.dumps({'passed':sum(c['status']=='passed' for c in q.CASES),'total':len(q.CASES)}),flush=True)

if __name__=='__main__':main()
