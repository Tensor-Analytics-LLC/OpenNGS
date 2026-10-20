import json
from pathlib import Path
import pysam,pytest
from backend.portable import artifact,execute
from backend import compiler,registry

def reference(tmp):
    p=tmp/'reference.fa';p.write_text('>chrM\n'+'ACGT'*100+'\n');pysam.faidx(str(p));return artifact(p,'FASTA',assembly='GRCh38',index=str(p)+'.fai')

def test_wes_capture_padding_and_true_target_coverage(tmp_path):
    ref=reference(tmp_path);bed=tmp_path/'kit.bed';bed.write_text('chrM\t10\t20\nchrM\t18\t30\n')
    capture=execute({'tool':'wes.capture','workdir':str(tmp_path/'capture'),'params':{'kit_name':'Known test capture','kit_version':'v1','padding_bp':5},'inputs':{'targets':[artifact(bed,'BED')],'reference':[ref]}})
    assert Path(capture['targets'][0]['path']).read_text()=='chrM\t10\t30\n'
    assert Path(capture['calling'][0]['path']).read_text()=='chrM\t5\t35\n'
    bam=tmp_path/'reads.bam'
    with pysam.AlignmentFile(str(bam),'wb',header={'HD':{'VN':'1.6','SO':'coordinate'},'SQ':[{'SN':'chrM','LN':400}]}) as out:
        for i in range(12):
            r=pysam.AlignedSegment();r.query_name='r'+str(i);r.query_sequence='GTACGTACGTACGTACGTAC';r.reference_id=0;r.reference_start=10;r.mapping_quality=60;r.flag=0;r.cigar=[(0,20)];r.query_qualities=pysam.qualitystring_to_array('I'*20);out.write(r)
    pysam.index(str(bam));item=artifact(bam,'INDEXED_BAM',index=str(bam)+'.bai')
    result=execute({'tool':'wes.coverage','workdir':str(tmp_path/'coverage'),'inputs':{'alignment':[item],'targets':capture['targets']},'params':{}})['report'][0]['metadata']['report']
    assert result['target_bases']==20 and result['mean_depth']==12
    assert result['fraction_at_depth']=={'1':1,'10':1,'20':0,'30':0}
    bed.write_text('other\t1\t2\n')
    with pytest.raises(ValueError):execute({'tool':'wes.capture','workdir':str(tmp_path/'invalid'),'params':{'kit_name':'Kit','kit_version':'1'},'inputs':{'targets':[artifact(bed,'BED')],'reference':[ref]}})

def test_mitochondrial_rotation_chain_and_dictionaries(tmp_path):
    ref=reference(tmp_path);result=execute({'tool':'mito.shift-reference','workdir':str(tmp_path/'rotate'),'params':{'contig':'chrM','shift_bases':81},'inputs':{'reference':[ref]}})
    with pysam.FastaFile(result['original'][0]['path']) as normal,pysam.FastaFile(result['shifted'][0]['path']) as shifted:
        a=normal.fetch('chrM');b=shifted.fetch('chrM');assert b==a[81:]+a[:81]
        for i in range(len(a)):assert b[i]==a[(i+81)%len(a)]
    assert all(Path(p).is_file() for row in [result['original'][0],result['shifted'][0]] for p in row['metadata']['index_paths'])
    chain=Path(result['chain'][0]['path']).read_text();assert '319\n\nchain' in chain and chain.endswith('81\n\n')
    assert result['original'][0]['metadata']['reference_digest']!=result['shifted'][0]['metadata']['reference_digest']

def test_wes_and_mito_callers_compile_distinct_scientific_modes(tmp_path):
    dummy=lambda name,fmt:{'path':str(tmp_path/name),'format':fmt,'metadata':{}}
    spec=registry.BASE['haplotypecaller'];node={'tool':spec['id'],'params':registry.defaults(spec),'resources':{'threads':2,'memory_mb':4096}}
    plan=compiler.compile_tool(spec,node,{'alignment':[dummy('a.bam','INDEXED_BAM')],'reference':[dummy('r.fa','FASTA')],'intervals':[dummy('kit.bed','BED')]},tmp_path)
    assert any('kit.bed' in x for x in plan['steps'][0]['argv'])
    spec=registry.BASE['gatk.mt.call'];node={'tool':spec['id'],'params':registry.defaults(spec),'resources':{'threads':2,'memory_mb':4096}}
    plan=compiler.compile_tool(spec,node,{'alignment':[dummy('a.bam','INDEXED_BAM')],'reference':[dummy('mt.fa','FASTA')]},tmp_path)
    command=plan['steps'][0]['argv'];assert '--mitochondria-mode' in command and 'HaplotypeCaller' not in command
    assert command[command.index('--max-reads-per-alignment-start')+1]=='0'
