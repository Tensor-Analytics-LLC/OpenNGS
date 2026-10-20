"""Sequence dictionaries describe contigs; they never supply calling intervals."""
import hashlib
import json
import time
from pathlib import Path

import pytest

from backend import cloud_data, data, db, graph, registry, semantics
from backend.portable import artifact, execute


def make_dictionary(tmp_path):
    reference=tmp_path/'reference.fa'
    reference.write_text('>chr1\nacGT\n>chrM\nTtAa\n')
    source=artifact(reference,'FASTA',assembly='GRCh38',contig_style='chr',reference_digest='sha256:'+hashlib.sha256(reference.read_bytes()).hexdigest())
    return execute({'tool':'reference.dictionary','inputs':{'reference':[source]},'workdir':str(tmp_path/'dictionary')})['dictionary'][0]


def test_dictionary_emits_explicit_sequence_type_and_original_reference_context(tmp_path):
    output=make_dictionary(tmp_path)
    assert output['format']=='REFERENCE_DICTIONARY'
    assert registry.BASE['reference.dictionary']['outputs'][0]['type']==output['format']
    assert output['metadata']['assembly']=='GRCh38' and output['metadata']['contig_style']=='chr'
    assert output['metadata']['reference_digest'].startswith('sha256:')
    lines=Path(output['path']).read_text().splitlines()
    assert lines==['@HD\tVN:1.6\tSO:unsorted',
                   '@SQ\tSN:chr1\tLN:4\tM5:'+hashlib.md5(b'ACGT').hexdigest(),
                   '@SQ\tSN:chrM\tLN:4\tM5:'+hashlib.md5(b'TTAA').hexdigest()]


def test_dictionary_is_not_an_interval_conversion_or_calling_input():
    assert not registry.compatible('REFERENCE_DICTIONARY','INTERVALLIST')
    assert not registry.compatible('REFERENCE_DICTIONARY','BED|INTERVALLIST')
    assert graph.adapter_path('REFERENCE_DICTIONARY','BED') is None
    assert graph.adapter_path('REFERENCE_DICTIONARY','INTERVALLIST') is None
    with pytest.raises(ValueError,match='REFERENCE_DICTIONARY.*INTERVALLIST'):
        semantics.runtime(registry.BASE['intervals.bed'],{'intervals':[{'format':'REFERENCE_DICTIONARY'}]})


def test_graph_preflight_blocks_dictionary_to_interval_converter(client,owner):
    pipeline={'nodes':[{'id':'ref','tool':'reference.tiny'},{'id':'dict','tool':'reference.dictionary'},
                       {'id':'bed','tool':'intervals.bed'}],
              'edges':[{'id':'reference','source':'ref','sourcePort':'reference','target':'dict','targetPort':'reference'},
                       {'id':'invalid','source':'dict','sourcePort':'dictionary','target':'bed','targetPort':'intervals'}]}
    result=graph.preflight(pipeline,owner)
    mismatch=next(issue for issue in result['issues'] if issue['code']=='FORMAT_MISMATCH')
    assert mismatch['node_id']=='bed' and 'REFERENCE_DICTIONARY' in mismatch['message']
    assert mismatch['fix'] is None


@pytest.mark.parametrize('stored_format',['REFERENCE_DICTIONARY','INTERVALLIST'])
def test_dictionary_cannot_silently_become_an_empty_bed_even_with_legacy_label(tmp_path,stored_format):
    original=make_dictionary(tmp_path)
    supplied={**original,'format':stored_format}
    before=json.loads(json.dumps(supplied))
    directory=tmp_path/'conversion'
    with pytest.raises(ValueError,match='dictionary|no interval rows'):
        execute({'tool':'intervals.bed','inputs':{'intervals':[supplied]},'workdir':str(directory)})
    assert supplied==before
    assert Path(original['path']).is_file()
    assert original['digest']=='sha256:'+hashlib.sha256(Path(original['path']).read_bytes()).hexdigest()
    assert not (directory/'regions.bed').exists()


def test_real_picard_interval_rows_still_convert(tmp_path):
    source=tmp_path/'targets.interval_list'
    source.write_text('@HD\tVN:1.6\tSO:coordinate\n@SQ\tSN:chr1\tLN:20\nchr1\t1\t5\t+\ttarget\nchr1\t11\t20\t-\tother\n')
    result=execute({'tool':'intervals.bed','inputs':{'intervals':[artifact(source,'INTERVALLIST')]},'workdir':str(tmp_path/'conversion')})
    assert result['intervals'][0]['format']=='BED'
    assert Path(result['intervals'][0]['path']).read_text()=='chr1\t0\t5\nchr1\t10\t20\n'


def test_new_dictionary_uploads_have_distinct_type_without_changing_other_companions():
    assert data.file_type(Path('reference.dict'))=='REFERENCE_DICTIONARY'
    assert data.file_type(Path('REFERENCE.DICT'))=='REFERENCE_DICTIONARY'
    assert data.file_type(Path('reference.fa.fai'))=='INDEX'
    assert data.file_type(Path('targets.interval_list'))=='INTERVALLIST'


def add_dataset(uid,ident,name,fmt):
    now=time.time();manifest={'format':fmt,'retained_provenance':'original'};key='tenants/'+uid+'/datasets/'+ident+'/'+name
    with db.tx() as c:
        c.execute('INSERT INTO datasets VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(ident,uid,None,name,4,'a'*64,'Ready',key,'version-'+ident,db.encode(manifest),now,now))
        c.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?,?)',(ident,uid,'s3://test/'+key,name,fmt,4,now,'reference','GRCh38',db.encode(manifest)))


@pytest.mark.parametrize('dictionary_format',['INDEX','REFERENCE_DICTIONARY'])
def test_typed_and_original_index_classified_dictionaries_remain_valid_reference_companions(owner,dictionary_format):
    uid=owner['id']
    add_dataset(uid,'reference','reference.fa','FASTA')
    add_dataset(uid,'dictionary','reference.dict',dictionary_format)
    original=db.query('SELECT * FROM datasets WHERE id=?',('dictionary',),one=True)
    result=cloud_data.bind(uid,'reference',{'dictionary_id':'dictionary','index_ids':['dictionary']})
    assert result['format']=='FASTA'
    assert result['metadata']['dictionary'].endswith('/reference.dict')
    assert result['metadata']['dictionary_version_id']=='version-dictionary'
    assert result['metadata']['dictionary_sha256']=='a'*64
    assert result['metadata']['index_paths']==[result['metadata']['dictionary']]
    assert db.query('SELECT * FROM datasets WHERE id=?',('dictionary',),one=True)==original


def test_typed_dictionary_cannot_substitute_for_an_alignment_index(owner):
    uid=owner['id']
    add_dataset(uid,'alignment','reads.bam','BAM')
    add_dataset(uid,'dictionary','reference.dict','REFERENCE_DICTIONARY')
    with pytest.raises(ValueError,match='verified index file'):
        cloud_data.bind(uid,'alignment',{'index_id':'dictionary'})

