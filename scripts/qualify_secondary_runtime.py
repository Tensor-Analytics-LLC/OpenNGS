"""Execute five optional scientific contracts; unavailable environments stay blocked."""
import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import pysam
import qualify_extended_runtime as q


def main():
    q.FIXTURES=Path('/tmp/secondary-fixtures');shutil.rmtree(q.FIXTURES,ignore_errors=True);shutil.copytree('/qualification/fixtures',q.FIXTURES,dirs_exist_ok=True)
    f=q.FIXTURES;base=q.inputs();installed=json.loads(Path('/opt/studio/secondary-install.json').read_text())
    recipes=json.loads(Path('/opt/studio/secondary-environments.json').read_text())
    def available(family):
        if installed[family]['status']=='installed':return True
        for tool in recipes[family]['tools']:q.CASES.append({'tool':tool,'status':'failed','error':'Environment unavailable: '+installed[family].get('error','')})
        return False
    if available('analysis'):
        try:
            q.command(['/opt/analysis/bin/python','-c',"import anndata as a,numpy as n; r=n.random.default_rng(7341); x=r.poisson(2,(64,128)).astype('float32'); x[:32,:32]+=r.poisson(8,(32,32)); x[32:,32:64]+=r.poisson(8,(32,32)); a.AnnData(x).write_h5ad(__import__('sys').argv[1])",f/'counts.h5ad'])
            def clustered(files):
                q.command(['/opt/analysis/bin/python','-c',"import anndata as a,numpy as n; x=a.read_h5ad(__import__('sys').argv[1]); assert x.shape==(64,128); assert x.obsm['X_umap'].shape==(64,2); assert n.isfinite(x.obsm['X_umap']).all(); assert len(x.obs.leiden.unique())>=2; assert x.layers['counts'].shape==x.shape; assert x.uns['openngs_analysis']['seed']==7341",files[0]])
                return {'cells':64,'genes':128,'finite_umap_coordinates':128,'at_least_two_clusters':True,'raw_counts_preserved':True,'scope':'Two synthetic expression populations; no cell-type or batch correction validation'}
            q.execute('scanpy',{'counts':[q.artifact(f/'counts.h5ad','H5AD')]},{'n_top_genes':100,'n_neighbors':10,'resolution':.8},assertion=clustered)
        except Exception as error:q.CASES.append({'tool':'scanpy','status':'failed','error':str(error)[-1800:]})
    if available('annotation'):
        try:
            root=f/'snpeff';genome=root/'engineering';genome.mkdir(parents=True)
            sequence='A'*100+'ATG'+'GCT'*98+'TAA'+'A'*600
            (genome/'sequences.fa').write_text('>engineering_contig\n'+sequence+'\n')
            (genome/'genes.gff').write_text('##gff-version 3\n'+'\n'.join('engineering_contig\tfixture\t'+row for row in [
                'gene\t101\t400\t.\t+\t.\tID=GENE1;Name=GENE1',
                'mRNA\t101\t400\t.\t+\t.\tID=TX1;Parent=GENE1',
                'exon\t101\t400\t.\t+\t.\tID=EXON1;Parent=TX1',
                'CDS\t101\t400\t.\t+\t0\tParent=TX1'])+'\n')
            config=f/'snpeff.config';config.write_text('data.dir = '+str(root)+'\nengineering.genome : Engineering\n')
            q.command(['env','JAVA_HOME=/opt/annotation','PATH=/opt/annotation/bin:'+os.environ.get('PATH',''),'/opt/annotation/bin/snpEff','build','-gff3','-noCheckCds','-noCheckProtein','-c',config,'-dataDir',root,'engineering'])
            vcf=f/'coding.vcf';vcf.write_text('##fileformat=VCFv4.2\n##contig=<ID=engineering_contig,length=1000>\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\nengineering_contig\t105\t.\tC\tT\t60\tPASS\t.\n')
            def annotated(files):
                with pysam.VariantFile(str(files[0])) as source: rows=list(source)
                assert len(rows)==1 and any('missense_variant' in a and 'GENE1' in a for a in rows[0].info['ANN'])
                return {'known_coding_snv':105,'expected_missense_consequence':True,'gene':'GENE1','scope':'Locally built artificial annotation; no human assembly database availability claim'}
            q.execute('snpeff',{'variants':[q.artifact(vcf,'VCF')],'database':[q.artifact(genome/'snpEffectPredictor.bin','DATABASE')]},{'assembly':'engineering'},assertion=annotated)
        except Exception as error:q.CASES.append({'tool':'snpeff','status':'failed','error':str(error)[-1800:]})
    if available('happy'):
        try:
            truth=f/'heterozygous.vcf';text=truth.read_text();header='\n'.join(x for x in text.splitlines() if x.startswith('#'))+'\n';row=next(x for x in text.splitlines() if not x.startswith('#'))
            with pysam.FastaFile(str(f/'reference.fa')) as ref:base_at=ref.fetch('engineering_contig',1999,2000)
            alt=next(x for x in 'ACGT' if x!=base_at)
            query=f/'query.vcf';query.write_text(header+row+'\nengineering_contig\t2000\t.\t'+base_at+'\t'+alt+'\t60\tPASS\t.\tGT\t0/1\n')
            regions=f/'confidence.bed';regions.write_text('engineering_contig\t0\t20000\n')
            def benchmarked(files):
                rows=list(csv.DictReader(next(p for p in files if p.name.endswith('summary.csv')).open()))
                selected=next(r for r in rows if r['Type']=='SNP' and r['Filter']=='ALL')
                assert abs(float(selected['METRIC.Precision'])-.5)<.0001 and abs(float(selected['METRIC.Recall'])-.5)<.0001,selected
                return {'truth_snps':2,'query_snps':2,'true_positive':1,'false_positive':1,'false_negative':1,'precision':.5,'recall':.5,'engine':'vcfeval','scope':'Controlled SNP accounting; not GIAB sample or indel representation accuracy validation'}
            q.execute('benchmark.happy',{'truth':[q.artifact(truth,'VCF')],'query':[q.artifact(query,'VCF')],'reference':base['reference'],'regions':[q.artifact(regions,'BED')]},{'engine':'vcfeval'},assertion=benchmarked)
        except Exception as error:q.CASES.append({'tool':'benchmark.happy','status':'failed','error':str(error)[-1800:]})
    if available('legacy'):
        for tool in ['strelka2','manta']:
            q.execute(tool,{k:base[k] for k in ['alignment','reference']},{'exome':True},assertion=q.variants)
    expected={'scanpy','snpeff','benchmark.happy','strelka2','manta'}
    if {c['tool'] for c in q.CASES}!=expected or len(q.CASES)!=len(expected):raise ValueError('Incomplete or duplicated scientific outcomes')
    report={'schema':1,'cases':q.CASES,'environments':installed,'scope':'Actual tiny engineering executions; representative clinical and full-size validation not established',
        'locks':{name:hashlib.sha256(Path('/opt/studio/'+name+'-explicit.lock').read_bytes()).hexdigest() for name in recipes if installed[name]['status']=='installed'}}
    Path(sys.argv[1]).write_text(json.dumps(report,indent=2));print(json.dumps({'passed':sum(c['status']=='passed' for c in q.CASES),'total':5}),flush=True)

if __name__=='__main__':main()
