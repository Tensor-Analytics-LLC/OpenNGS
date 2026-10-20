"""Execute eight real format contracts inside the bounded, isolated cloud image."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

from backend.format_contract import ASSERTIONS, FAMILY, ISOLATION, SCOPE, VERSION

VCF = '''##fileformat=VCFv4.3
##contig=<ID=1,length=1000,assembly=engineering,Description="First control contig">
##contig=<ID=2,length=1000,assembly=engineering,Description="Second control contig">
##FILTER=<ID=q10,Description="Quality below ten">
##INFO=<ID=AF,Number=A,Type=Float,Description="Allele frequency">
##INFO=<ID=DP,Number=1,Type=Integer,Description="Depth">
##INFO=<ID=END,Number=1,Type=Integer,Description="End position">
##INFO=<ID=CHR2,Number=1,Type=String,Description="Partner contig">
##ALT=<ID=NON_REF,Description="Any alternate allele">
##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">
##FORMAT=<ID=AD,Number=R,Type=Integer,Description="Allelic depths">
##FORMAT=<ID=DP,Number=1,Type=Integer,Description="Sample depth">
##FORMAT=<ID=PL,Number=G,Type=Integer,Description="Likelihoods">
#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE_A\tSAMPLE_B
1\t10\tmulti\tAC\tA,ACC\t60\tPASS\tAF=0.25,0.75;DP=40\tGT:AD:DP:PL\t1|2:5,15,20:40:100,90,80,70,0,60\t./1:10,4,0:14:0,10,20,30,40,50
1\t20\tblock\tG\t<NON_REF>\t.\tq10\tEND=25;DP=3\tGT:AD:DP:PL\t0/0:3,0:3:0,9,99\t./.:.,.:.:.,.,.
1\t30\tbnd1\tA\tA[2:40[\t50\tPASS\tCHR2=2\tGT\t0/1\t1|1
1\t31\tbnd2\tA\tA]2:41]\t50\tPASS\tCHR2=2\tGT\t0/1\t0/0
1\t32\tbnd3\tA\t[2:42[A\t50\tPASS\tCHR2=2\tGT\t0/1\t./1
1\t33\tbnd4\tA\t]2:43]A\t50\tPASS\tCHR2=2\tGT\t0/1\t1/1
'''
BED = '# exact comment\ntrack name="control"\nbrowser position 1:1-30\n1\t0\t1\tone\t0\t+\n2\t9\t12\ttwo\t9\t-\n1\t20\t30\tblocks\t8\t-\t20\t30\t0\t2\t3,3,\t0,7,\n'
GTF = '# interval controls\n1\tcontrol\texon\t1\t1\t.\t+\t.\tgene_id "a";\n1\tcontrol\texon\t10\t12\t.\t-\t.\tgene_id "b";\n2\tcontrol\texon\t21\t22\t.\t.\t.\tgene_id "c";\n1\tcontrol\tgene\t1\t50\t.\t+\t.\tgene_id "a";\n##FASTA\n>1\nACGT\n'
FASTA = '>1 first\naCgTaCgTaCgTaCgTaCgTaCgTaCgTaCgT\n>2 second\nTTTTCCCCAAAAGGGGttttcccc\n'
INTERVALS = '@HD\tVN:1.6\tSO:coordinate\n@SQ\tSN:1\tLN:1000\n@SQ\tSN:2\tLN:1000\n1\t1\t1\t+\tone\n1\t10\t12\t-\ttwo\n2\t21\t22\t+\tthree\n'
FIXTURES = {'records.vcf':VCF,'blocks.bed':BED,'features.gtf':GTF,'reference.fa':FASTA,
            'regions.interval_list':INTERVALS,'header-only.interval_list':INTERVALS[:INTERVALS.index('1\t1\t1')],
            'undeclared.vcf':VCF.replace('##INFO=<ID=AF,Number=A,Type=Float,Description="Allele frequency">\n',''),
            'collision.bed':'1\t0\t1\nchr1\t1\t2\n','zero.bed':'1\t1\t1\n',
            'invalid-blocks.bed':'1\t20\t30\tb\t0\t+\t20\t30\t0\t2\t3,3,\t0,8,\n',
            'invalid-columns.gtf':'1\tcontrol\texon\t1\t1\t.\t+\t.\n',
            'invalid-strand.gtf':'1\tcontrol\texon\t1\t1\t.\t?\t.\tgene_id "a";\n',
            'malformed-bnd.vcf':VCF.replace('A[2:40[','A[2:40[T')}


def fixture_files(directory):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    rows=[]
    for name,text in sorted(FIXTURES.items()):
        raw=text.encode();(directory/name).write_bytes(raw)
        rows.append({'name':name,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()})
    return rows


def vcf_snapshot(path):
    import pysam
    with pysam.VariantFile(str(path)) as src:
        # BCF stores dictionary offsets internally. Compare biological/header
        # declarations independently from those storage-only IDX offsets.
        declarations=[]
        for rec in src.header.records:
            values=[(key,value) for key,value in rec.items() if key!='IDX']
            declarations.append([rec.type,rec.key,rec.value,values])
        return {'samples':list(src.header.samples),'header':declarations,
                'records':[str(rec).rstrip('\r\n').split('\t') for rec in src]}


def verify_artifacts(paths):
    """Independently inspect the retained actual artifacts; no worker execution."""
    import tempfile
    if sys.flags.optimize:raise ValueError('Format semantic assertions must execute without Python optimization')
    with tempfile.TemporaryDirectory(prefix='openngs-format-controls-') as directory:
        source=Path(directory,'source.vcf');source.write_text(VCF);original=vcf_snapshot(source)
    for name in ['vcf.bcf/converted.bcf','bcf.vcf/converted.vcf']:
        assert vcf_snapshot(paths[name])==original,'VCF/BCF full records or header changed'
    renamed=vcf_snapshot(paths['contig.rename/renamed.vcf'])
    expected=[]
    for row in original['records']:
        row=list(row);row[0]='chr'+row[0]
        row[4]=re.sub(r'(?<=[\[\]])2:', 'chr2:',row[4])
        row[7]=row[7].replace('CHR2=2','CHR2=chr2');expected.append(row)
    assert renamed['records']==expected,'Contig rename changed a record beyond explicit contig fields'
    # Exact untouched declarations, including descriptions and assembly metadata.
    def noncontig(header):return [row for row in header if row[1]!='contig']
    assert noncontig(renamed['header'])==noncontig(original['header'])
    old_contigs=[row for row in original['header'] if row[1]=='contig']
    expected_contigs=[]
    for row in old_contigs:
        row=[*row[:3],[(key,'chr'+value if key=='ID' else value) for key,value in row[3]]];expected_contigs.append(row)
    assert [row for row in renamed['header'] if row[1]=='contig']==expected_contigs
    renamed_bed=Path(paths['contig.rename.bed/renamed.bed']).read_text()
    expected_bed=''.join(('chr'+line if line.startswith(('1\t','2\t')) else line) for line in BED.splitlines(keepends=True))
    assert renamed_bed==expected_bed,'BED rename failed exact preservation of other columns/directives'
    assert Path(paths['gtf.bed/features.bed']).read_text()=='1\t0\t1\texon\t0\t+\n1\t9\t12\texon\t0\t-\n2\t20\t22\texon\t0\t.\n'
    gtf=[line.split('\t') for line in Path(paths['bed.gtf/intervals.gtf']).read_text().splitlines()]
    assert [(r[0],int(r[3]),int(r[4]),r[6]) for r in gtf]==[('1',1,1,'+'),('2',10,12,'-'),('1',21,23,'-'),('1',28,30,'-')]
    assert gtf[2][8]==gtf[3][8] and len({r[8] for r in gtf})==3 and all(r[2]=='exon' for r in gtf)
    assert Path(paths['intervals.bed/regions.bed']).read_text()=='1\t0\t1\n1\t9\t12\n2\t20\t22\n'
    lines=Path(paths['reference.dictionary/reference.dict']).read_text().splitlines()
    rows=[dict(field.split(':',1) for field in line.split('\t')[1:]) for line in lines if line.startswith('@SQ\t')]
    assert len(lines)==3 and lines[0].startswith('@HD\t') and [r['SN'] for r in rows]==['1','2']
    assert [int(r['LN']) for r in rows]==[32,24]
    assert [r['M5'] for r in rows]==[hashlib.md5(seq.upper().encode()).hexdigest() for seq in ['aCgT'*8,'TTTTCCCCAAAAGGGGttttcccc']]
    return {'actual_output_files':len(paths),'full_vcf_records':6,'breakend_orientations':4,'bed12_exons':2,
            'dictionary_sequence_checksums_verified':True}


def isolation(work,fixtures):
    def denied(path):
        try:Path(path).write_text('unexpected write')
        except OSError:return True
        Path(path).unlink();return False
    status=dict(line.split(':',1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
    mounts={fields[4]:fields[5].split(',') for line in Path('/proc/self/mountinfo').read_text().splitlines()
            if len(fields:=line.split())>6}
    for directory in [work,Path('/tmp')]:
        probe=directory/'format-write-probe';probe.write_text('ok');probe.unlink()
    return {'uid':os.geteuid(),'root_write_denied':denied('/opt/studio/format-write-probe'),
        'input_write_denied':denied(fixtures/'format-write-probe'),
        'root_mount_read_only':'ro' in mounts.get('/',[]),'input_mount_read_only':'ro' in mounts.get(str(fixtures),[]),
        'effective_capabilities_zero':int(status['CapEff'].strip(),16)==0,
        'no_new_privileges':status['NoNewPrivs'].strip()=='1',
        'no_default_ipv4_route':not any(row.split()[1]=='00000000' for row in Path('/proc/net/route').read_text().splitlines()[1:]),
        'work_writable':True,'tmp_writable':True}


def qualify(work,fixtures,check_isolation=True):
    import pysam
    from backend.portable import artifact,execute
    if sys.flags.optimize:raise ValueError('Format qualification requires active semantic assertions')
    work=Path(work);work.mkdir(parents=True,exist_ok=True);fixtures=Path(fixtures)
    for name,text in FIXTURES.items():assert (fixtures/name).read_bytes()==text.encode(),'Fixture differs from reviewed bytes'
    proof={'schema':1,'family':FAMILY,'version':VERSION,'scope':SCOPE,'cases':[],
           'versions':{'python':sys.version.split()[0],'pysam':pysam.__version__,'samtools':pysam.__samtools_version__},
           'implementation_sha256':{name:hashlib.sha256((Path('/opt/studio')/name).read_bytes()).hexdigest()
               for name in ['backend/portable.py','backend/scientific.py','backend/format_operators.py','backend/format_contract.py']}
               if check_isolation else {},'isolation':isolation(work,fixtures) if check_isolation else None}
    (work/'qualification.json').write_text(json.dumps(proof,indent=2))
    if check_isolation:assert proof['isolation']==ISOLATION,'Actual worker isolation differs from the required profile'
    outputs={};counter=0
    def input_item(name,fmt):return artifact(fixtures/name,fmt,assembly='engineering')
    def invoke(tool,port,item,params=None,suffix=''):
        return execute({'tool':tool,'inputs':{port:[item]},'workdir':str(work/(tool+suffix)),'params':params or {},'threads':2})
    def reject(tool,port,item,params=None):
        nonlocal counter
        counter+=1
        try:invoke(tool,port,item,params,'-rejection-'+str(counter))
        except ValueError:return True
        raise AssertionError('Invalid control was accepted: '+tool)
    plans=[('vcf.bcf','variants',input_item('records.vcf','VCF'),{}),
           ('bcf.vcf','variants',None,{}),('contig.rename','data',input_item('records.vcf','VCF'),{'style':'chr'}),
           ('contig.rename.bed','data',input_item('blocks.bed','BED'),{'style':'chr'}),
           ('gtf.bed','intervals',input_item('features.gtf','GTF'),{'feature':'exon'}),
           ('bed.gtf','intervals',input_item('blocks.bed','BED'),{}),
           ('intervals.bed','intervals',input_item('regions.interval_list','INTERVALLIST'),{}),
           ('reference.dictionary','reference',input_item('reference.fa','FASTA'),{})]
    for tool,port,item,params in plans:
        began=time.monotonic();case={'tool':tool,'status':'failed'};proof['cases'].append(case)
        try:
            if tool=='bcf.vcf':item=outputs['vcf.bcf']['variants'][0]
            result=invoke(tool,port,item,params);outputs[tool]=result
            if tool=='vcf.bcf':reject(tool,port,input_item('undeclared.vcf','VCF'))
            if tool=='contig.rename':
                reject(tool,port,item,{'style':'mapping','mapping':'{"1":"same","2":"same"}'})
                reject(tool,port,item,{'style':'invalid'})
                reject(tool,port,input_item('malformed-bnd.vcf','VCF'))
                assert result['data'][0]['metadata']['liftover'] is False
            if tool=='contig.rename.bed':
                reject(tool,port,input_item('collision.bed','BED'));assert result['data'][0]['metadata']['liftover'] is False
            if tool=='gtf.bed':
                reject(tool,port,input_item('invalid-columns.gtf','GTF'));reject(tool,port,input_item('invalid-strand.gtf','GTF'))
            if tool=='bed.gtf':
                reject(tool,port,input_item('zero.bed','BED'));reject(tool,port,input_item('invalid-blocks.bed','BED'))
            if tool=='intervals.bed':
                reject(tool,port,input_item('header-only.interval_list','REFERENCE_DICTIONARY'))
                reject(tool,port,input_item('header-only.interval_list','INTERVALLIST'))
            if tool=='reference.dictionary':assert result['dictionary'][0]['format']=='REFERENCE_DICTIONARY'
            case.update(status='passed',assertions=ASSERTIONS[tool],outputs=[{
                'name':tool+'/'+Path(obj['path']).name,'format':obj['format'],'bytes':Path(obj['path']).stat().st_size,
                'sha256':hashlib.sha256(Path(obj['path']).read_bytes()).hexdigest()}
                for rows in result.values() for obj in rows])
        except Exception as error:
            case.update(status='failed',error=str(error)[-1500:]);raise
        finally:
            case['seconds']=round(time.monotonic()-began,3)
            (work/'qualification.json').write_text(json.dumps(proof,indent=2))
    paths={obj['name']:work/obj['name'] for case in proof['cases'] for obj in case['outputs']}
    proof['artifact_verification']=verify_artifacts(paths)
    (work/'qualification.json').write_text(json.dumps(proof,indent=2));return proof


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--work',default='/work')
    parser.add_argument('--fixtures',default='/inputs/fixtures');args=parser.parse_args()
    qualify(args.work,args.fixtures)


if __name__=='__main__':main()
