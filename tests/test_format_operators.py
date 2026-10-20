"""Engineering checks for record-aware format adapters; no cloud qualification claims."""
import gzip
import hashlib
import json
from pathlib import Path

import pysam
import pytest

from backend.format_operators import execute as execute_format
from backend.portable import artifact, execute


VCF_HEADER='''##fileformat=VCFv4.3
##source=OpenNGS-format-engineering-control
##reference=engineering-reference-only
##contig=<ID=1,length=1000,assembly=engineering>
##contig=<ID=2,length=2000,assembly=engineering>
##contig=<ID=M,length=100>
##FILTER=<ID=q10,Description="Low quality">
##INFO=<ID=AF,Number=A,Type=Float,Description="Allele frequencies">
##INFO=<ID=AN,Number=1,Type=Integer,Description="Allele count">
##INFO=<ID=FLAG,Number=0,Type=Flag,Description="Flag retained">
##INFO=<ID=NOTE,Number=1,Type=String,Description="Unstructured annotation">
##INFO=<ID=END,Number=1,Type=Integer,Description="End position">
##INFO=<ID=CHR2,Number=.,Type=String,Description="Partner contig">
##INFO=<ID=CHROM2,Number=.,Type=String,Description="Other partner contigs">
##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">
##FORMAT=<ID=AD,Number=R,Type=Integer,Description="Allelic depths">
##FORMAT=<ID=DP,Number=1,Type=Integer,Description="Depth">
##FORMAT=<ID=PL,Number=G,Type=Integer,Description="Genotype likelihoods">
##FORMAT=<ID=PS,Number=1,Type=Integer,Description="Phase set">
#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tcase\tcontrol
'''
VCF_ROWS=[
    '1\t1\trs-control\tA\tC,G\t47.5\tq10\tAF=0.125,0.25;AN=4;FLAG;NOTE=1:20\tGT:AD:DP:PL:PS\t1|2:3,7,11:21:80,70,60,50,40,0:1\t./.:.,.,.:.:.,.,.,.,.,.:.',
    '1\t9\tdel-control\tA\t<DEL>\t60\tPASS\tEND=15;NOTE=2:40\tGT:DP\t0/1:22\t1/1:18',
    'M\t3\t.\tN\t.\t.\t.\t.\tGT:DP\t.:8\t0:9',
]


def vcf_source(tmp_path,rows=None,header=VCF_HEADER,name='input.vcf'):
    source=tmp_path/name
    source.write_text(header+'\n'.join(VCF_ROWS if rows is None else rows)+'\n')
    return artifact(source,'VCF',assembly='engineering',contig_style='numeric',public=True,
                    reference_digest='sha256:'+'a'*64,private_key='do-not-propagate')


def run(tmp_path,tool,item,port,params=None,tag=''):
    return execute({'tool':tool,'inputs':{port:[item]},'params':params or {},
                    'workdir':str(tmp_path/(tool.replace('.','_')+tag))})


def vcf_snapshot(path):
    with pysam.VariantFile(path) as source:
        header=str(source.header)
        samples=tuple(source.header.samples)
        rows=[]
        for record in source:
            rows.append({'contig':record.contig,'pos':record.pos,'stop':record.stop,
                         'id':record.id,'ref':record.ref,'alts':record.alts,'qual':record.qual,
                         'filter':tuple(record.filter),'info':dict(record.info),
                         'format':tuple(record.format),
                         'samples':{name:(dict(record.samples[name]),record.samples[name].phased)
                                    for name in samples}})
        return header,samples,rows


def test_vcf_bcf_roundtrip_retains_complete_typed_records_and_headers(tmp_path):
    source=vcf_source(tmp_path)
    expected=vcf_snapshot(source['path'])
    encoded=run(tmp_path,'vcf.bcf',source,'variants')['variants'][0]
    assert encoded['format']=='BCF'
    assert Path(encoded['path']).read_bytes()[:2]==b'\x1f\x8b'
    assert vcf_snapshot(encoded['path'])==expected
    restored=run(tmp_path,'bcf.vcf',encoded,'variants')['variants'][0]
    assert restored['format']=='VCF' and vcf_snapshot(restored['path'])==expected
    assert restored['metadata']['assembly']=='engineering'
    assert restored['metadata']['reference_digest']==source['metadata']['reference_digest']
    assert 'private_key' not in restored['metadata']
    rows=expected[2]
    assert rows[0]['alts']==('C','G') and rows[0]['samples']['case'][0]['GT']==(1,2)
    assert rows[0]['samples']['case'][1] is True
    assert rows[0]['samples']['control'][0]['GT']==(None,None)
    assert rows[1]['stop']==15 and rows[2]['samples']['control'][0]['GT']==(0,)


@pytest.mark.parametrize('alter',[
    lambda text:text.replace('##contig=<ID=1,length=1000,assembly=engineering>\n',''),
    lambda text:text.replace('##INFO=<ID=FLAG,Number=0,Type=Flag,Description="Flag retained">\n',''),
    lambda text:text.replace('##FORMAT=<ID=DP,Number=1,Type=Integer,Description="Depth">\n',''),
    lambda text:text.replace('##FILTER=<ID=q10,Description="Low quality">\n',''),
])
@pytest.mark.parametrize('tool,port',[('vcf.bcf','variants'),('contig.rename','data')])
def test_encoding_and_renaming_reject_undeclared_record_dictionaries(tmp_path,alter,tool,port):
    source=vcf_source(tmp_path)
    Path(source['path']).write_text(alter(Path(source['path']).read_text()))
    with pytest.raises(ValueError,match='explicit.*declarations'):
        run(tmp_path,tool,source,port)


def test_rename_handles_four_breakend_orientations_and_declared_partner_fields(tmp_path):
    alternatives=['A[2:41[','A]2:42]',']2:43]A','[2:44[A']
    rows=[f'1\t{10+i}\tbnd{i}\tA\t{alt},G\t50\tPASS\tCHR2=2;CHROM2=2,M;NOTE=2:99\tGT:AD:DP\t1|2:2,3,4:9\t0/1:6,2,0:8'
          for i,alt in enumerate(alternatives)]
    source=vcf_source(tmp_path,rows)
    before=vcf_snapshot(source['path'])
    output=run(tmp_path,'contig.rename',source,'data',{'style':'chr'})['data'][0]
    header,samples,renamed=vcf_snapshot(output['path'])
    assert samples==before[1] and 'ID=chr1,length=1000,assembly=engineering' in header
    assert 'ID=chr2,length=2000,assembly=engineering' in header
    assert 'ID=chrM,length=100' in header
    for i,(old,new) in enumerate(zip(before[2],renamed)):
        assert new['contig']=='chr1' and new['alts']==(alternatives[i].replace('2:','chr2:'),'G')
        assert new['info']['CHR2']==('chr2',) and new['info']['CHROM2']==('chr2','chrM')
        assert new['info']['NOTE']=='2:99'
        for field in ('pos','stop','id','ref','qual','filter','format','samples'):
            assert new[field]==old[field]
    assert output['metadata']['liftover'] is False
    assert output['metadata']['assembly']=='engineering'
    assert output['metadata']['contig_style']=='chr'


def test_rename_from_bcf_and_explicit_mapping_preserves_non_name_fields(tmp_path):
    source=vcf_source(tmp_path)
    encoded=run(tmp_path,'vcf.bcf',source,'variants')['variants'][0]
    output=run(tmp_path,'contig.rename',encoded,'data',{'style':'mapping','mapping':json.dumps({'1':'scaffold:one','M':'mitochondrion'})})['data'][0]
    original=vcf_snapshot(source['path'])[2]
    renamed=vcf_snapshot(output['path'])[2]
    assert output['format']=='VCF'
    assert [row['contig'] for row in renamed]==['scaffold:one','scaffold:one','mitochondrion']
    for old,new in zip(original,renamed):
        assert {key:value for key,value in new.items() if key!='contig'}=={key:value for key,value in old.items() if key!='contig'}


@pytest.mark.parametrize('description',['has ID=other','contains,ID=other','escaped \\" quote,ID=other'])
def test_rename_contig_header_id_attribute_can_follow_quoted_description(tmp_path,description):
    declaration=f'##contig=<Description="{description}",length=100,ID=1>'
    header='##fileformat=VCFv4.3\n'+declaration+'\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n'
    source=vcf_source(tmp_path,['1\t1\t.\tA\tG\t.\tPASS\t.'],header)
    output=run(tmp_path,'contig.rename',source,'data')['data'][0]
    assert declaration.replace(',ID=1>',',ID=chr1>') in Path(output['path']).read_text()
    assert vcf_snapshot(output['path'])[2][0]['contig']=='chr1'


@pytest.mark.parametrize('alt',['A]2:10]T',']2:10]','A]2:10[','A]2:10]]','A[undeclared:10['])
def test_rename_rejects_malformed_or_undeclared_breakend_mates(tmp_path,alt):
    source=vcf_source(tmp_path,[f'1\t10\t.\tA\t{alt}\t50\tPASS\t.\tGT\t0/1\t0/0'])
    with pytest.raises(ValueError,match='breakend|Breakend'):
        run(tmp_path,'contig.rename',source,'data')


def test_rename_rejects_undeclared_partner_info_contig(tmp_path):
    source=vcf_source(tmp_path,['1\t10\t.\tA\tG\t50\tPASS\tCHR2=unknown\tGT\t0/1\t0/0'])
    with pytest.raises(ValueError,match='Partner contig'):
        run(tmp_path,'contig.rename',source,'data')


@pytest.mark.parametrize('params',[
    {'style':'not-a-style'}, {'style':'mapping','mapping':'[]'},
    {'style':'mapping','mapping':'{"1":""}'},
    {'style':'mapping','mapping':'{"1":"with space"}'},
    {'style':'mapping','mapping':'{"1":"bad,header"}'},
    {'style':'mapping','mapping':'{"1":42}'},
    {'style':'mapping','mapping':'{"1":"2"}'},
])
def test_rename_rejects_invalid_style_mapping_and_declared_contig_collision(tmp_path,params):
    with pytest.raises(ValueError):
        run(tmp_path,'contig.rename',vcf_source(tmp_path),'data',params)


@pytest.mark.parametrize('style,names',[('chr',['1','chr1']),('numeric',['M','MT'])])
def test_rename_rejects_collision_even_when_second_header_contig_has_no_records(tmp_path,style,names):
    header='##fileformat=VCFv4.3\n'+''.join(f'##contig=<ID={name},length=100>\n' for name in names)+'#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n'
    source=vcf_source(tmp_path,[f'{names[0]}\t1\t.\tA\tG\t.\tPASS\t.'],header)
    with pytest.raises(ValueError,match='many-to-one'):
        run(tmp_path,'contig.rename',source,'data',{'style':style})


def test_bed_rename_preserves_all_columns_comments_and_directive_like_contigs(tmp_path):
    source=tmp_path/'regions.bed.gz'
    text='# preserved comment\ntrack name="annotation"\nbrowser position 1:1-100\n\n1\t100\t200\tgene name\t917\t-\t110\t190\t255,0,0\t2\t20,40,\t0,60,\tcustom\ntrack1\t0\t1\nbrowser_contig\t2\t3\nbrowser1\t4\t5\ntrack\t6\t7\nbrowser\t8\t9\n'
    with gzip.open(source,'wt') as handle:handle.write(text)
    result=run(tmp_path,'contig.rename.bed',artifact(source,'BED',assembly='engineering'),'data',{'style':'chr'})['data'][0]
    expected=text.replace('\n1\t','\nchr1\t').replace('\ntrack1\t','\nchrtrack1\t').replace('\nbrowser_contig\t','\nchrbrowser_contig\t').replace('\nbrowser1\t','\nchrbrowser1\t').replace('\ntrack\t','\nchrtrack\t').replace('\nbrowser\t','\nchrbrowser\t')
    assert Path(result['path']).read_text()==expected
    assert result['format']=='BED' and result['metadata']['liftover'] is False


def test_bed_rename_detects_collisions_in_observed_names(tmp_path):
    source=tmp_path/'regions.bed';source.write_text('1\t0\t1\nchr1\t4\t5\n')
    with pytest.raises(ValueError,match='many-to-one'):
        run(tmp_path,'contig.rename.bed',artifact(source,'BED'),'data')


@pytest.mark.parametrize('tool,port',[('vcf.bcf','variants'),('bcf.vcf','variants'),('contig.rename','data'),('contig.rename.bed','data'),('gtf.bed','intervals'),('bed.gtf','intervals')])
def test_format_operators_reject_missing_or_multiple_inputs(tmp_path,tool,port):
    for rows in ([],[{},{}]):
        with pytest.raises(ValueError,match='exactly one'):
            execute({'tool':tool,'inputs':{port:rows},'workdir':str(tmp_path/tool)})


def test_gtf_bed_boundary_coordinates_strands_filter_and_gff_fasta_stop(tmp_path):
    source=tmp_path/'features.gff3'
    source.write_text('##gff-version 3\n1\tx\texon\t1\t1\t.\t+\t.\tID=first\n1\tx\texon\t10\t20\t3\t-\t0\tID=second\ntrack1\tx\texon\t99\t100\t.\t.\t2\tID=third\n1\tx\tCDS\t5\t8\t.\t+\t1\tID=cds\n##FASTA\n>1\nACGT\n')
    item=artifact(source,'GFF3',assembly='engineering')
    output=run(tmp_path,'gtf.bed',item,'intervals')['intervals'][0]
    assert Path(output['path']).read_text()=='1\t0\t1\texon\t0\t+\n1\t9\t20\texon\t0\t-\ntrack1\t98\t100\texon\t0\t.\n'
    all_features=run(tmp_path,'gtf.bed',item,'intervals',{'feature':''},tag='_all')['intervals'][0]
    assert Path(all_features['path']).read_text().endswith('1\t4\t8\tCDS\t0\t+\n')
    assert output['format']=='BED' and output['metadata']['assembly']=='engineering'
    assert output['metadata']['inferred_transcript_biology'] is False


@pytest.mark.parametrize('row',[
    '1\tx\texon\t0\t1\t.\t+\t.\tID=x',
    '1\tx\texon\t2\t1\t.\t+\t.\tID=x',
    '1\tx\texon\tbad\t1\t.\t+\t.\tID=x',
    '1\tx\texon\t1\t2\t.\t?\t.\tID=x',
    '1\tx\texon\t1\t2\t.\t+\t3\tID=x',
    '1\tx\texon\t1\t2\t.\t+\t.',
    '1\tx\texon\t1\t2\t.\t+\t.\tID=x\textra',
])
def test_gtf_bed_rejects_invalid_columns_boundaries_strand_and_phase(tmp_path,row):
    source=tmp_path/'features.gtf';source.write_text(row+'\n')
    with pytest.raises(ValueError):run(tmp_path,'gtf.bed',artifact(source,'GTF'),'intervals')


def test_bed12_to_gtf_uses_blocks_and_generated_ids_without_inventing_transcript_biology(tmp_path):
    source=tmp_path/'regions.bed'
    source.write_text('browser position 1:1-200\n1\t100\t200\toriginal name\t900\t-\t110\t190\t0\t2\t20,40,\t0,60,\ntrack1\t0\t1\n')
    output=run(tmp_path,'bed.gtf',artifact(source,'BED'),'intervals')['intervals'][0]
    rows=[line.split('\t') for line in Path(output['path']).read_text().splitlines()]
    assert [(row[0],row[3],row[4],row[6]) for row in rows]==[('1','101','120','-'),('1','161','200','-'),('track1','1','1','.')]
    assert rows[0][8]==rows[1][8]=='gene_id "interval_1"; transcript_id "interval_1";'
    assert rows[2][8]=='gene_id "interval_2"; transcript_id "interval_2";'
    assert all(row[1:3]==['OpenNGS','exon'] and row[5]==row[7]=='.' for row in rows)
    assert 'original name' not in Path(output['path']).read_text()
    assert output['format']=='GTF' and output['metadata']['inferred_transcript_biology'] is False
    assert 'BED12 blocks become separate' in output['metadata']['conversion_scope']


@pytest.mark.parametrize('line',[
    '1\t1', '1\t-1\t10', '1\t10\t9', '1\t0\t0', '1\t0\t10\tname\t0\t?',
    '1\t0\t10\tname\t0\t+\t0\t10\t0\t1',
    '1\t0\t10\tname\t0\t+\t0\t10\t0\t2\t10\t0',
    '1\t0\t10\tname\t0\t+\t0\t10\t0\t1\t0\t0',
    '1\t0\t10\tname\t0\t+\t0\t10\t0\t1\t11\t0',
    '1\t0\t10\tname\t0\t+\t0\t10\t0\t2\t6,5\t0,5',
    '1\t0\t10\tname\t0\t+\t0\t10\t0\t1\t9\t1',
    '1\t0\t10\tname\t0\t+\t0\t10\t0\t1\t9\t0',
])
def test_bed_gtf_rejects_invalid_intervals_and_blocks(tmp_path,line):
    source=tmp_path/'regions.bed';source.write_text(line+'\n')
    with pytest.raises(ValueError):run(tmp_path,'bed.gtf',artifact(source,'BED'),'intervals')


def test_zero_length_bed_site_can_be_renamed_without_becoming_a_gtf_feature(tmp_path):
    source=tmp_path/'site.bed';source.write_text('1\t0\t0\n')
    output=run(tmp_path,'contig.rename.bed',artifact(source,'BED'),'data')['data'][0]
    assert Path(output['path']).read_text()=='chr1\t0\t0\n'


def test_reviewed_module_creates_its_work_directory(tmp_path):
    source=tmp_path/'single.gtf';source.write_text('1\tx\texon\t1\t1\t.\t+\t.\tgene_id "a";\n')
    output=execute_format({'tool':'gtf.bed','inputs':{'intervals':[artifact(source,'GTF')]},'workdir':str(tmp_path/'new'/'work')})
    assert Path(output['intervals'][0]['path']).read_text()=='1\t0\t1\texon\t0\t+\n'


def test_reference_dictionary_output_is_contig_metadata_with_uppercase_sequence_md5(tmp_path):
    source=tmp_path/'reference.fa';source.write_text('>first description\nacGt\nNn\n>second\nttaa\n')
    output=run(tmp_path,'reference.dictionary',artifact(source,'FASTA',assembly='engineering'),'reference')['dictionary'][0]
    assert output['format']=='REFERENCE_DICTIONARY'
    assert output['metadata']['assembly']=='engineering'
    assert Path(output['path']).read_text().splitlines()==[
        '@HD\tVN:1.6\tSO:unsorted',
        '@SQ\tSN:first\tLN:6\tM5:'+hashlib.md5(b'ACGTNN').hexdigest(),
        '@SQ\tSN:second\tLN:4\tM5:'+hashlib.md5(b'TTAA').hexdigest(),
    ]


@pytest.mark.parametrize('fmt',['REFERENCE_DICTIONARY','INTERVALLIST'])
def test_interval_conversion_rejects_header_only_dictionary_even_with_historical_label(tmp_path,fmt):
    source=tmp_path/'reference.dict';source.write_text('@HD\tVN:1.6\tSO:unsorted\n@SQ\tSN:1\tLN:100\n')
    with pytest.raises(ValueError,match='dictionary|no interval rows'):
        run(tmp_path,'intervals.bed',artifact(source,fmt),'intervals')
    assert not (tmp_path/'intervals_bed'/'regions.bed').exists()


def test_real_interval_list_boundaries_still_emit_bed_format(tmp_path):
    source=tmp_path/'targets.interval_list'
    source.write_text('@HD\tVN:1.6\tSO:coordinate\n@SQ\tSN:1\tLN:100\n1\t1\t1\t+\tfirst\n1\t20\t30\t-\tsecond\n')
    output=run(tmp_path,'intervals.bed',artifact(source,'INTERVALLIST'),'intervals')['intervals'][0]
    assert output['format']=='BED' and Path(output['path']).read_text()=='1\t0\t1\n1\t19\t30\n'
