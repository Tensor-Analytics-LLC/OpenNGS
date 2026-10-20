"""Execute scientific commands on bounded, disclosed fixtures inside the image.

Passing cases establish small-data command contracts, not assay accuracy. A
failing case remains unavailable even if its binary installed successfully.
"""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

from backend import compiler, registry
from backend.runtime_catalog import EXTENDED_PACKAGES

ROOT = Path('/opt/studio/demo-data')
WORK = Path('/work')
INPUTS = Path('/inputs')
FIXTURES = Path('/qualification/fixtures')
CASES = []

def command(argv, stdout=None, seconds=120):
    target = open(stdout, 'wb') if stdout else subprocess.DEVNULL
    try:
        result = subprocess.run(list(map(str, argv)), stdout=target, stderr=subprocess.PIPE,
                                timeout=seconds, check=False, cwd=WORK)
        if result.returncode:
            raise RuntimeError(str(argv[0]) + ': ' + result.stderr.decode(errors='replace')[-1800:])
    finally:
        if stdout:
            target.close()

def artifact(path, fmt, **meta):
    return {'path': str(path), 'format': fmt, 'metadata': meta}

def inputs():
    return {
        'reads': [artifact(ROOT/'sample.fastq/fixture_R1.fastq.gz', 'FASTQ_PAIR',
                           mate=str(ROOT/'sample.fastq/fixture_R2.fastq.gz'))],
        'reference': [artifact(FIXTURES/'reference.fa', 'FASTA', index=str(FIXTURES/'reference.fa.fai'))],
        'alignment': [artifact(FIXTURES/'reads.bam', 'INDEXED_BAM', index=str(FIXTURES/'reads.bam.bai'))],
    }

def execute(tool, bound, params=None, advanced='', assertion=None):
    began = time.monotonic()
    case = {'tool': tool, 'scope': 'Synthetic engineering command integration; no clinical accuracy claim'}
    try:
        for root in [WORK, INPUTS]:
            for p in root.iterdir():
                if p.is_dir():
                    shutil.rmtree(p)
                else:
                    p.unlink()
        spec = registry.BASE[tool]
        node = {'tool': tool, 'params': params or {}, 'advanced': advanced,
                'resources': {'threads': 2, 'memory_mb': 4096}}
        plan = compiler.compile_tool(spec, node, bound, WORK)
        for source, target in plan['bindings']:
            p = Path(target)
            p.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, p)
        for step in plan['steps']:
            command(step['argv'], WORK/step['stdout'] if step.get('stdout') else None)
        files = sorted({p for descs in plan['outputs'].values() for desc in descs for p in WORK.glob(desc['file'])})
        for descs in plan['outputs'].values():
            for desc in descs:
                if not list(WORK.glob(desc['file'])):
                    raise AssertionError('Missing output ' + desc['file'])
        checks = assertion(files) if assertion else {'expected_output_files': len(files)}
        if not checks or not files:
            raise AssertionError('No scientific output assertions')
        case.update(status='passed', assertions=checks, outputs=[
            {'name': str(p.relative_to(WORK)), 'bytes': p.stat().st_size,
             'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in files])
    except Exception as error:
        case.update(status='failed', error=(type(error).__name__ + ': ' + str(error))[-2000:])
    case['seconds'] = round(time.monotonic()-began, 3)
    CASES.append(case)
    print(json.dumps({'tool': tool, 'status': case['status'], 'seconds': case['seconds'],
                      'error': case.get('error')}), flush=True)

def alignments(files):
    import pysam
    path = next(p for p in files if p.suffix in ['.sam', '.bam'])
    with pysam.AlignmentFile(str(path)) as handle:
        records = list(handle.fetch(until_eof=True))
    mapped = sum(not r.is_unmapped for r in records)
    assert mapped > 0 and len(records) == 2000, (len(records), mapped)
    return {'read_records': len(records), 'mapped_records': mapped, 'expected_input_read_records': 2000}

def variants(files):
    import pysam
    path = next(p for p in files if '.vcf' in p.name or p.suffix == '.bcf')
    with pysam.VariantFile(str(path)) as handle:
        records = list(handle)
        assert 'engineering_contig' in handle.header.contigs
    return {'valid_variant_records': len(records), 'reference_contig_preserved': True,
            'control': 'Synthetic command integration; negative output permitted, no sensitivity claim'}

def main():
    import pysam
    if FIXTURES.exists():
        shutil.rmtree(FIXTURES)
    FIXTURES.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(exist_ok=True)
    INPUTS.mkdir(exist_ok=True)
    shutil.copyfile(ROOT/'sample.fastq/engineering.fa', FIXTURES/'reference.fa')
    pysam.faidx(str(FIXTURES/'reference.fa'))
    command(['bwa-mem2', 'index', FIXTURES/'reference.fa'])
    command(['bwa-mem2', 'mem', '-R', '@RG\\tID:ENG\\tSM:SAMPLE\\tPL:ILLUMINA',
             FIXTURES/'reference.fa', ROOT/'sample.fastq/fixture_R1.fastq.gz',
             ROOT/'sample.fastq/fixture_R2.fastq.gz'], FIXTURES/'reads.sam')
    command(['samtools', 'sort', '-o', FIXTURES/'reads.bam', FIXTURES/'reads.sam'])
    command(['samtools', 'index', FIXTURES/'reads.bam'])
    with pysam.FastaFile(str(FIXTURES/'reference.fa')) as fasta:
        sequence = fasta.fetch('engineering_contig')
    header = '##fileformat=VCFv4.2\n##contig=<ID=engineering_contig,length='+str(len(sequence))+'>\n##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE\n'
    text = header + ''.join('engineering_contig\t%d\t.\t%s\t%s\t60\tPASS\t.\tGT\t0/1\n' % (p, sequence[p-1], next(b for b in 'ACGT' if b != sequence[p-1])) for p in [1000, 1010])
    (FIXTURES/'heterozygous.vcf').write_text(text)
    (FIXTURES/'targets.bed').write_text('engineering_contig\t0\t5000\tENGINEERING_A\nengineering_contig\t5000\t10000\tENGINEERING_B\nengineering_contig\t10000\t15000\tENGINEERING_C\nengineering_contig\t15000\t20000\tENGINEERING_D\n')
    (FIXTURES/'identity.chain').write_text('chain 100 engineering_contig 20000 + 0 20000 engineering_contig 20000 + 0 20000 1\n20000\n')
    (FIXTURES/'interval.bed').write_text('engineering_contig\t100\t200\n')
    base = inputs()
    for tool in ['bowtie2', 'hisat2', 'star']:
        execute(tool, {k: base[k] for k in ['reads', 'reference']}, assertion=alignments)

    def trimmed(files):
        import gzip
        count = len(gzip.open(next(p for p in files if 'R1' in p.name), 'rt').readlines())//4
        assert count == 1000
        return {'paired_reads_retained': count, 'input_pairs': 1000}
    execute('trimmomatic', {'reads': base['reads']}, assertion=trimmed)

    command(['fastqc', '--threads', '2', '--outdir', FIXTURES,
             ROOT/'sample.fastq/fixture_R1.fastq.gz', ROOT/'sample.fastq/fixture_R2.fastq.gz'])
    def multiqc(files):
        html = files[0].read_text()
        assert 'FastQC' in html and 'fixture_R1' in html and 'fixture_R2' in html
        return {'parsed_fastqc_samples': 2, 'fastqc_machine_reports': True}
    execute('multiqc', {'reports': [artifact(p, 'REPORT') for p in FIXTURES.glob('*_fastqc.zip')]}, assertion=multiqc)

    def quant(files):
        with files[0].open() as handle:
            rows = list(csv.DictReader(handle, delimiter='\t'))
        total = sum(float(x['NumReads']) for x in rows)
        assert total > 0 and total <= 1000 and rows[0]['Name']=='engineering_contig'
        return {'quantified_transcripts': len(rows), 'assigned_fragments': total, 'input_pairs': 1000,
                'reference': 'Single synthetic transcript; no biological abundance validation'}
    execute('salmon', {k: base[k] for k in ['reads', 'reference']}, assertion=quant)
    for tool in ['varscan2', 'delly', 'sniffles2']:
        execute(tool, {k: base[k] for k in ['alignment', 'reference']}, assertion=variants)
    execute('whatshap', {**{k: base[k] for k in ['alignment', 'reference']},
                        'variants': [artifact(FIXTURES/'heterozygous.vcf','VCF')]}, assertion=variants)

    def peak(files):
        rows=[line for line in files[0].read_text().splitlines() if line and not line.startswith('#')]
        assert rows and all(line.split('\t')[0]=='engineering_contig' for line in rows)
        return {'called_peaks': len(rows), 'synthetic_enrichment': True}
    execute('macs3', {'treatment': [artifact(ROOT/'sample.alignments/engineering.bam', 'BAM')]},
            {'genome_size': '20000', 'qvalue': .05, 'broad': False},
            '--keep-dup all --nomodel --extsize 100', peak)
    def lifted(files):
        assert files[0].read_text().strip() == 'engineering_contig\t100\t200'
        return {'identity_chain_round_trip': True, 'zero_based_half_open_interval': [100,200]}
    execute('crossmap.bed', {'chain': [artifact(FIXTURES/'identity.chain', 'CHAIN')],
                           'intervals': [artifact(FIXTURES/'interval.bed', 'BED')]}, assertion=lifted)
    def coverage(files):
        # deepTools' Python environment owns pyBigWig; ask it to validate the binary.
        command(['/opt/extended/bin/python', '-c',
                 "import pyBigWig; b=pyBigWig.open(__import__('sys').argv[1]); assert b.chroms()['engineering_contig']==20000; assert b.stats('engineering_contig',0,20000)[0]>0",
                 files[0]])
        return {'bigwig_reference_length': 20000, 'nonzero_coverage': True}
    execute('bam.bigwig', {'alignment': base['alignment']}, {'bin_size': 25, 'normalization': 'None'}, assertion=coverage)
    def segments(files):
        count=sum(max(0,len(p.read_text().splitlines())-1) for p in files)
        assert count>0
        return {'segment_rows':count,'baseline':'Flat synthetic baseline; no CNV truth sensitivity claim'}
    execute('cnvkit', {**{k: base[k] for k in ['alignment','reference']},
                      'targets': [artifact(FIXTURES/'targets.bed', 'BED')]},
            {'assay_method':'amplicon','segmentation':'haar'},
            assertion=segments)
    def bisulfite(files):
        with pysam.AlignmentFile(str(files[0])) as handle:
            records=sum(1 for _ in handle.fetch(until_eof=True))
        assert records>0
        return {'bam_records':records,'sample':'Unconverted synthetic reads; command-only, no methylation accuracy claim'}
    execute('bismark', {k: base[k] for k in ['reads','reference']},
            assertion=bisulfite)

    # Kraken's small test database is explicit and local; runtime egress is not needed.
    kraken=FIXTURES/'kraken';(kraken/'taxonomy').mkdir(parents=True,exist_ok=True)
    (kraken/'taxonomy/nodes.dmp').write_text('1\t|\t1\t|\tno rank\t|\t\t|\t0\t|\t0\t|\t1\t|\t0\t|\t0\t|\t0\t|\t0\t|\t0\t|\t\t|\n2\t|\t1\t|\tspecies\t|\t\t|\t0\t|\t0\t|\t1\t|\t0\t|\t0\t|\t0\t|\t0\t|\t0\t|\t\t|\n')
    (kraken/'taxonomy/names.dmp').write_text('1\t|\troot\t|\t\t|\tscientific name\t|\n2\t|\tEngineering species\t|\t\t|\tscientific name\t|\n')
    (FIXTURES/'taxonomy.fa').write_text('>engineering_contig|kraken:taxid|2\n'+sequence+'\n')
    try:
        command(['kraken2-build','--add-to-library',FIXTURES/'taxonomy.fa','--db',kraken])
        command(['kraken2-build','--build','--db',kraken,'--threads','2'])
        def classified(files):
            rows = [line.split('\t') for line in files[0].read_text().splitlines() if line.strip()]
            assert any(row[-1].strip()=='Engineering species' and float(row[0])>0 for row in rows)
            return {'known_synthetic_taxon_detected': True, 'taxonomic_truth': 'one artificial species'}
        execute('kraken2', {'reads':base['reads'],'database':[artifact(kraken/'hash.k2d','DATABASE',
            index_paths=[str(kraken/'opts.k2d'),str(kraken/'taxo.k2d')])]}, assertion=classified)
    except Exception as error:
        CASES.append({'tool':'kraken2','status':'failed','error':str(error)[-1500:]})
    # A positive repeat control checks the declared genotype, not merely a VCF.
    repeat=FIXTURES/'repeat';repeat.mkdir(exist_ok=True)
    repeat_sequence=sequence[:10000]+'CAG'*20+sequence[10060:]
    (repeat/'reference.fa').write_text('>engineering_contig\n'+repeat_sequence+'\n')
    pysam.faidx(str(repeat/'reference.fa'))
    complement=str.maketrans('ACGT','TGCA')
    with (repeat/'reads_R1.fastq').open('w') as left,(repeat/'reads_R2.fastq').open('w') as right:
        for i in range(600):
            start=9750+i%300;fragment=repeat_sequence[start:start+400]
            left.write('@repeat%d/1\n%s\n+\n%s\n'%(i,fragment[:150],'I'*150))
            right.write('@repeat%d/2\n%s\n+\n%s\n'%(i,fragment[-150:].translate(complement)[::-1],'I'*150))
    try:
        command(['bwa-mem2','index',repeat/'reference.fa'])
        command(['bwa-mem2','mem','-R','@RG\\tID:REPEAT\\tSM:SAMPLE\\tPL:ILLUMINA',repeat/'reference.fa',repeat/'reads_R1.fastq',repeat/'reads_R2.fastq'],repeat/'reads.sam')
        command(['samtools','sort','-o',repeat/'reads.bam',repeat/'reads.sam'])
        command(['samtools','index',repeat/'reads.bam'])
        (repeat/'catalogue.json').write_text(json.dumps([{'LocusId':'engineering_repeat','ReferenceRegion':'engineering_contig:10000-10060','LocusStructure':'(CAG)*','VariantType':'Repeat'}]))
        def repeated(files):
            with pysam.VariantFile(str(next(p for p in files if p.suffix=='.vcf'))) as handle:
                calls=list(handle)
            assert len(calls)==1 and len(calls[0].samples)==1
            sample=next(iter(calls[0].samples))
            observed=calls[0].samples[sample]['REPCN']
            assert observed=='20/20',f'Expected synthetic 20/20 repeat genotype, observed {observed}'
            return {'motif':'CAG','expected_repeat_copies':[20,20],'observed_repeat_copies':'20/20',
                    'sample_label':sample,'input_pairs':600,'qualification':'One synthetic reference-length positive control'}
        execute('expansionhunter',{'reference':[artifact(repeat/'reference.fa','FASTA',index=str(repeat/'reference.fa.fai'))],
                                  'alignment':[artifact(repeat/'reads.bam','INDEXED_BAM',index=str(repeat/'reads.bam.bai'))],
                                  'catalogue':[artifact(repeat/'catalogue.json','REPORT')]},assertion=repeated)
    except Exception as error:
        CASES.append({'tool':'expansionhunter','status':'failed','error':str(error)[-1500:]})
    from backend.remote_viewer_worker import execute as prepare_viewer
    from backend.viewer_protocol import decode,validate_metadata,validate_row
    viewer_dir=FIXTURES/'viewer';viewer_dir.mkdir(exist_ok=True)
    source=ROOT/'sample.clinvar/ClinVar_GRCh38_1000.vcf'
    result=prepare_viewer({'workdir':str(viewer_dir),'params':{'operation':'variant-index'},
        'inputs':{'data':[artifact(source,'VCF',assembly='GRCh38')]},'max_output_bytes':16*1024**2})
    lines=Path(result['report'][0]['path']).read_bytes().splitlines()
    validate_metadata(decode(lines[0]))
    for line in lines[1:]:validate_row(decode(line))
    assert len(lines)-1==999, len(lines)-1
    report={'schema':1,'packages':EXTENDED_PACKAGES,'cases':CASES,
            'internal_cases':[{'tool':'results.prepare','operation':'variant-index','status':'passed',
                'source':'Public repository ClinVar GRCh38 slice','allele_rows':len(lines)-1,
                'assertions':['Every row passes strict interchange validation','999 expected alternate allele rows'],
                'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest()}],
            'dataset':{'source':'Repository immutable engineering FASTQ/reference/alignment fixtures',
                       'paired_reads':1000,'reference_bases':len(sequence),
                       'reference_sha256':hashlib.sha256((FIXTURES/'reference.fa').read_bytes()).hexdigest()},
            'qualification':'Small synthetic command contracts, not clinical validation or representative throughput'}
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True)
    Path(parser.parse_args().output).write_text(json.dumps(report,indent=2))
    if not any(x['status']=='passed' for x in CASES):
        raise SystemExit('No executable scientific contracts passed')

if __name__=='__main__':
    main()
