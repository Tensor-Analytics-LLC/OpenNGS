import concurrent.futures
import copy
import json
import time
from pathlib import Path
import pytest
from backend import data,db,graph,registry,runner,security
from backend.config import DATA
from backend.portable import execute

def wait_run(client,rid):
    for _ in range(150):
        r=client.get('/api/runs/'+rid).json()
        if r['run']['state'] in ['succeeded','failed','cancelled','interrupted']:return r
        time.sleep(.1)
    pytest.fail('Run did not reach a terminal state')

def test_authentication_origin_and_csrf(client,owner):
    assert client.post('/api/graph/preflight',json={'graph':registry.templates()[0]['graph']},headers={'Origin':'https://evil.test'}).status_code==403
    assert client.post('/api/graph/preflight',json={'graph':registry.templates()[0]['graph']},headers={'X-CSRF-Token':'wrong'}).status_code==403
    assert client.get('/api/session').json()['user']['role']=='admin'
    assert 'password' not in client.get('/api/session').text
    assert client.cookies.get('ngs_session')

def test_cross_tenant_graph_secret_and_admin_denial(client,owner):
    g=registry.templates()[0]['graph']
    assert client.put('/api/graphs/owner_graph',json={'graph':g,'revision':0}).status_code==200
    saved=client.post('/api/vault',json={'provider':'oncokb','label':'owner-key','value':'test-not-real-key-123','bindings':['oncokb']});assert saved.status_code==200
    r=client.post('/api/auth/signup',json={'email':'other@example.test','password':'Another-test-password-7341!'});client.headers['X-CSRF-Token']=r.json()['csrf']
    assert r.json()['user']['role']=='user'
    assert client.get('/api/graphs/owner_graph').status_code==404
    assert client.get('/api/vault').json()==[]
    assert client.get('/api/admin').status_code==403
    assert client.post('/api/vault/'+saved.json()['id']+'/check',json={}).status_code==422
    assert client.patch('/api/settings',json={'ssd_roots':['/Users/k1zzle/.aws']}).status_code==422

def test_graph_cycle_and_explicit_adapters(client,owner):
    g=copy.deepcopy(registry.templates()[0]['graph'])
    g['edges'].append({'id':'bad','source':'n2','sourcePort':'published','target':'n1','targetPort':'reads'})
    assert any(i['code']=='CYCLE' for i in graph.preflight(g,owner)['issues'])
    assert graph.adapter_path('SAM','INDEXED_BAM')==['bam.sort','bam.index']
    assert graph.adapter_path('VCF','INDEXED_VCF')==['vcf.bgzip','vcf.index']
    assert graph.adapter_path('FASTQ_PAIR','INDEXED_VCF') is None
    fixed=graph.apply_fixes({'nodes':[{'id':'a','tool':'asset.file','x':0,'y':0},{'id':'b','tool':'bam.stats','x':500,'y':0}],'edges':[{'id':'e','source':'a','sourcePort':'data','target':'b','targetPort':'alignment'}]},[{'action':'insert_adapters','edge_id':'e','tools':['bam.sort','bam.index']}])
    assert len(fixed['nodes'])==4 and len(fixed['edges'])==3

def test_revision_conflict_and_no_unknown_input(client,owner):
    g=registry.templates()[0]['graph']
    assert client.put('/api/graphs/concurrent',json={'graph':g,'revision':0}).status_code==200
    assert client.put('/api/graphs/concurrent',json={'graph':g,'revision':0}).status_code==409
    g=copy.deepcopy(g);g['nodes'][0]['tool']='not-published'
    r=client.post('/api/graph/preflight',json={'graph':g}).json()
    assert not r['ok'] and any(i['code']=='UNKNOWN_TOOL' for i in r['issues'])

def test_trial_does_not_grant_third_party_rights(client,owner):
    n={'id':'a','tool':'spliceai','x':0,'y':0,'params':{},'resources':{'threads':1,'memory_mb':1024,'timeout_seconds':30}}
    r=graph.preflight({'nodes':[n],'edges':[]},owner)
    assert any(i['code']=='RIGHTS_REQUIRED' for i in r['issues'])

def test_real_quality_pipeline_and_idempotency(client,owner):
    g=registry.templates()[0]['graph'];headers={'Idempotency-Key':'quality-test-operation-001'}
    r=client.post('/api/runs',json={'graph':g},headers=headers);assert r.status_code==200,r.text
    rid=r.json()['run']['id'];result=wait_run(client,rid)
    assert result['run']['state']=='succeeded',result
    reports=[a for a in result['artifacts'] if a['name']=='read-quality.json'];assert reports
    report=client.get('/api/artifacts/'+reports[0]['id']+'?metadata=1').json()['metadata']['report']
    assert report['total_reads']==2000 and report['mates'][0]['q30_percent']==100
    assert client.get('/api/artifacts/'+reports[0]['id']).status_code==200
    duplicate=client.post('/api/runs',json={'graph':g},headers=headers);assert duplicate.status_code==200,duplicate.text
    assert duplicate.json()['run']['id']==rid and not duplicate.json()['created']
    s=client.get('/api/session').json()['user'];assert s['reserved_seconds']==0 and s['used_seconds']>0

def test_concurrent_reservations_cannot_double_spend(client,owner,monkeypatch):
    with db.tx() as c:c.execute('UPDATE users SET quota_seconds=120 WHERE id=?',(owner['id'],))
    owner=db.query('SELECT * FROM users WHERE id=?',(owner['id'],),one=True)
    monkeypatch.setattr(runner.POOL,'submit',lambda *a,**k:None)
    monkeypatch.setattr(runner,'capabilities',lambda:{'portable':True,'docker':False})
    g=registry.templates()[0]['graph']
    def submit(i):
        try:runner.admit(owner,g,'parallel-operation-'+str(i));return True
        except ValueError:return False
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:results=list(pool.map(submit,range(30)))
    assert sum(results)==1
    live=db.query('SELECT * FROM users WHERE id=?',(owner['id'],),one=True)
    assert live['reserved_seconds']==90 and live['reserved_seconds']<=live['quota_seconds']

def test_vault_encryption_revocation_and_audit(client,owner):
    plaintext='test-only-provider-secret-7341'
    r=client.post('/api/vault',json={'provider':'oncokb','label':'research','value':plaintext,'bindings':['oncokb']});assert r.status_code==200
    sid=r.json()['id'];stored=db.query('SELECT * FROM secrets WHERE id=?',(sid,),one=True)
    assert plaintext not in stored['encrypted'] and plaintext not in client.get('/api/vault').text
    assert security.unwrap(owner['id'],sid,'oncokb')==plaintext
    with pytest.raises(ValueError):security.unwrap(owner['id'],sid,'franklin')
    assert client.post('/api/vault/'+sid+'/revoke',json={}).status_code==200
    with pytest.raises(ValueError):security.unwrap(owner['id'],sid,'oncokb')
    assert security.verify_audit()['valid']
    with db.tx() as c:c.execute("UPDATE audit SET body='{}' WHERE seq=(SELECT MAX(seq) FROM audit)")
    assert not security.verify_audit()['valid']

def test_freeze_quarantine_and_cancel_remain_available(client,owner):
    assert client.post('/api/admin/freeze',json={'frozen':True,'reason':'Test incident containment'}).status_code==200
    r=client.post('/api/runs',json={'graph':registry.templates()[0]['graph']},headers={'Idempotency-Key':'frozen-operation-001'})
    assert r.status_code==422 and 'GLOBAL_FREEZE' in r.text
    assert client.post('/api/admin/freeze',json={'frozen':False,'reason':'Test incident resolved'}).status_code==200
    assert client.post('/api/admin/quarantine',json={'tool_id':'qc.summary','reason':'Test compromised release'}).status_code==200
    assert any(i['code']=='QUARANTINED' for i in client.post('/api/graph/preflight',json={'graph':registry.templates()[0]['graph']}).json()['issues'])

def test_native_bam_sort_index_and_exact_record_counts(tmp_path):
    import pysam
    src=tmp_path/'unsorted.sam'
    src.write_text('@HD\tVN:1.6\tSO:unsorted\n@SQ\tSN:chr22\tLN:1000\nb\t0\tchr22\t50\t60\t4M\t*\t0\t0\tACGT\tIIII\na\t0\tchr22\t10\t60\t4M\t*\t0\t0\tACGT\tIIII\n')
    item={'path':str(src),'format':'SAM','metadata':{}};work=tmp_path/'sort'
    out=execute({'tool':'bam.sort','inputs':{'alignment':[item]},'workdir':str(work)})
    indexed=execute({'tool':'bam.index','inputs':out,'workdir':str(tmp_path/'index')})
    p=indexed['alignment'][0]
    with pysam.AlignmentFile(p['path'],'rb') as bam:assert [r.reference_start for r in bam.fetch('chr22')]==[9,49]
    assert Path(p['metadata']['index']).exists()

def test_coordinate_adapters_preserve_intervals(tmp_path):
    bed=tmp_path/'regions.bed';bed.write_text('chr22\t0\t10\tfirst\nchr22\t20\t25\tsecond\n');fa=tmp_path/'reference.fa';fa.write_text('>chr22\n'+'A'*100+'\n')
    import pysam;pysam.faidx(str(fa))
    out=execute({'tool':'bed.intervals','inputs':{'intervals':[{'path':str(bed)}],'reference':[{'path':str(fa)}]},'workdir':str(tmp_path/'intervals')})
    back=execute({'tool':'intervals.bed','inputs':out,'workdir':str(tmp_path/'bed')})
    assert Path(back['intervals'][0]['path']).read_text()=='chr22\t0\t10\nchr22\t20\t25\n'

def test_scratch_paths_and_format_validation(client,owner):
    assert client.post('/api/tools/publish',json={'name':'Unsafe','image':'docker.io/test@sha256:'+'0'*64,'command':[['tool']], 'inputs':[{'key':'data','type':'ANY'}],'outputs':[{'key':'result','type':'REPORT'}],'output_files':{'result':{'file':'../../outside','type':'REPORT'}}}).status_code==422
    assert client.post('/api/graph/preflight',json={'graph':{'nodes':[{'id':'n','tool':'sample.fastq','x':0,'y':0,'params':{},'resources':{'threads':1000,'memory_mb':1000000,'timeout_seconds':1000000}}],'edges':[]}}).json()['ok'] is False

def test_cancellation_confirms_terminal_state_and_releases_reservation(client,owner):
    r=client.post('/api/runs',json={'graph':registry.templates()[0]['graph']},headers={'Idempotency-Key':'cancel-operation-7341'});assert r.status_code==200
    rid=r.json()['run']['id'];assert client.post('/api/runs/'+rid+'/cancel',json={}).status_code==200
    result=wait_run(client,rid);assert result['run']['state']=='cancelled',result
    assert client.get('/api/session').json()['user']['reserved_seconds']==0

def test_mitochondrial_qc_measures_known_mixture(tmp_path):
    import pysam
    ref=tmp_path/'mt.fa';ref.write_text('>chrM\n'+'A'*100+'\n');pysam.faidx(str(ref))
    source=tmp_path/'mt.bam'
    header={'HD':{'VN':'1.6','SO':'coordinate'},'SQ':[{'SN':'chrM','LN':100}]}
    with pysam.AlignmentFile(str(source),'wb',header=header) as out:
        for i in range(100):
            r=pysam.AlignedSegment();r.query_name='mt'+str(i);r.query_sequence=('C' if i<10 else 'A')+'A'*19;r.flag=0;r.reference_id=0;r.reference_start=10;r.mapping_quality=60;r.cigar=[(0,20)];r.query_qualities=pysam.qualitystring_to_array('I'*20);out.write(r)
    pysam.index(str(source))
    result=execute({'tool':'mt.qc','params':{'contig':'chrM','minimum_depth':50,'minimum_fraction':.01},'inputs':{'alignment':[{'path':str(source)}],'reference':[{'path':str(ref)}]},'workdir':str(tmp_path/'mt-out')})
    data=result['report'][0]['metadata']['report'];assert data['sites'][0]['position']==11
    assert data['sites'][0]['alternate_fractions']['C']['fraction']==.1
    assert data['clinical_validation'] is False

def test_provider_hgvs_conversion_keeps_indel_coordinates_explicit(tmp_path):
    vcf=tmp_path/'input.vcf';vcf.write_text('##fileformat=VCFv4.2\n##contig=<ID=chr7,length=2000>\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\nchr7\t100\t.\tA\tC\t50\tPASS\t.\nchr7\t200\t.\tAT\tA\t50\tPASS\t.\nchr7\t300\t.\tA\tAT\t50\tPASS\t.\n')
    work=tmp_path/'provider';execute({'tool':'provider.variants','params':{'limit':10},'inputs':{'variants':[{'path':str(vcf)}]},'workdir':str(work)})
    assert [x['hgvsg'] for x in json.loads((work/'variants-for-provider.json').read_text())]==['7:g.100A>C','7:g.201del','7:g.300_301insT']
