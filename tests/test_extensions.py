import copy
import json
import time
from pathlib import Path
import pytest
from backend import cache,db,graph,onboarding,registry,workbench
from backend.config import DATA
from backend.portable import execute,artifact
from test_studio import wait_run

def vcf(path,rows,sample=True):
    path.write_text('##fileformat=VCFv4.2\n##contig=<ID=chr22,length=10000>\n##INFO=<ID=MQ,Number=1,Type=Float,Description="Mapping quality">\n##INFO=<ID=gnomAD_AF,Number=A,Type=Float,Description="Population frequency">\n##INFO=<ID=GENE,Number=1,Type=String,Description="Gene">\n##INFO=<ID=CLNSIG,Number=1,Type=String,Description="ClinVar source label">\n##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n##FORMAT=<ID=DP,Number=1,Type=Integer,Description="Depth">\n##FORMAT=<ID=GQ,Number=1,Type=Integer,Description="Genotype quality">\n##FORMAT=<ID=AD,Number=R,Type=Integer,Description="Allelic depths">\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO'+('\tFORMAT\tHG002' if sample else '')+'\n'+''.join('chr22\t'+str(pos)+'\t.\t'+ref+'\t'+alt+'\t60\tPASS\tMQ=60;gnomAD_AF='+str(af)+';GENE='+gene+';CLNSIG='+label+('\tGT:DP:GQ:AD\t0/1:30:50:15,15' if sample else '')+'\n' for pos,ref,alt,af,gene,label in rows))
    return artifact(path,'VCF',assembly='GRCh38',contig_style='chr')

def job(tmp,tool,inputs,params=None):return execute({'tool':tool,'inputs':inputs,'params':params or {},'workdir':str(tmp/tool.replace('.','_'))})

def bind(uid,item):
    aid=db.ident('artifact_')
    with db.tx() as c:c.execute('INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?,?,?,?)',(aid,uid,None,None,item['name'],item['path'],item['format'],item['bytes'],item.get('digest'),db.encode(item.get('metadata',{})),time.time()))
    return aid

def test_exact_benchmark_known_tp_fp_fn_and_intervals(tmp_path):
    truth=vcf(tmp_path/'truth.vcf',[(10,'A','G',.001,'GENE1','Pathogenic'),(20,'A','T',.02,'GENE2','Benign'),(30,'A','AC',0,'GENE3','Uncertain_significance')]);query=vcf(tmp_path/'query.vcf',[(10,'A','G',.001,'GENE1','Pathogenic'),(40,'C','T',.03,'GENE4','Benign')]);bed=tmp_path/'confident.bed';bed.write_text('chr22\t0\t100\n')
    out=job(tmp_path,'benchmark.exact',{'truth':[truth],'query':[query],'regions':[artifact(bed,'BED')]})['report'][0]['metadata']['report'];snv=out['metrics']['SNV']
    assert (snv['TP'],snv['FP'],snv['FN'])==(1,1,1)
    assert snv['precision']==snv['recall']==snv['f1']==.5
    assert snv['precision_wilson_95'][0]<.5<snv['precision_wilson_95'][1]
    assert out['metrics']['INDEL']['FN']==1 and out['independent_validation'] is False

def test_scatter_gather_consensus_and_vcf_bcf_roundtrip(tmp_path):
    item=vcf(tmp_path/'source.vcf',[(10,'A','G',.001,'GENE1','Pathogenic'),(20,'A','T',.02,'GENE2','Benign')]);bcf=job(tmp_path,'vcf.bcf',{'variants':[item]});back=job(tmp_path,'bcf.vcf',bcf)
    import pysam
    with pysam.VariantFile(back['variants'][0]['path']) as f:assert [r.pos for r in f]==[10,20]
    shards=job(tmp_path,'vcf.scatter',{'variants':[item]});gather=job(tmp_path,'vcf.gather',shards)
    with pysam.VariantFile(gather['variants'][0]['path']) as f:assert len(list(f))==2
    second=vcf(tmp_path/'second.vcf',[(10,'A','G',.001,'GENE1','Pathogenic')]);ensemble=job(tmp_path,'vcf.ensemble',{'callsets':[item,second]},{'minimum_callers':2})
    with pysam.VariantFile(ensemble['variants'][0]['path']) as f:
        rows=list(f);assert len(rows)==1 and rows[0].info['OPENNGS_SUPPORT']==2

def test_contig_rename_and_gtf_coordinates(tmp_path):
    item=vcf(tmp_path/'source.vcf',[(10,'A','G',.001,'GENE1','Pathogenic')]);renamed=job(tmp_path,'contig.rename',{'data':[item]},{'style':'numeric'})
    import pysam
    with pysam.VariantFile(renamed['data'][0]['path']) as f:assert next(f).contig=='22'
    gtf=tmp_path/'genes.gtf';gtf.write_text('chr22\tx\texon\t1\t10\t.\t+\t.\tgene_id "a";\n')
    bed=job(tmp_path,'gtf.bed',{'intervals':[artifact(gtf,'GTF')]})
    assert Path(bed['intervals'][0]['path']).read_text().startswith('chr22\t0\t10')

def test_qc_router_and_bh_reference_values(tmp_path):
    report=tmp_path/'qc.json';report.write_text('{"mean_coverage":20}');data=artifact(report,'REPORT');gate=job(tmp_path,'qc.gate',{'data':[data],'report':[data]},{'metric':'mean_coverage','operator':'>=','threshold':30})
    assert gate['passed']==[] and gate['rejected']==[data]
    with pytest.raises(ValueError):job(tmp_path,'qc.gate',{'data':[data],'report':[data]},{'metric':'missing','operator':'>=','threshold':30})
    table=tmp_path/'p.tsv';table.write_text('gene\tpvalue\na\t0.01\nb\t0.04\nc\t0.03\nd\t0.002\n')
    result=job(tmp_path,'rna.fdr',{'table':[artifact(table,'TSV')]},{'p_column':'pvalue','fdr':.035})
    content=Path(result['table'][0]['path']).read_text();assert 'a\t0.01\t0.02' in content and 'd\t0.002\t0.008' in content and 'b\t' not in content

def test_variant_guards_do_not_accept_missing_metrics(tmp_path):
    item=vcf(tmp_path/'source.vcf',[(10,'A','G',.001,'GENE1','Pathogenic')]);out=job(tmp_path,'variant.guard',{'variants':[item]},{'minimum_mq':40,'minimum_dp':10,'minimum_gq':20,'minimum_quality':30})
    assert out['report'][0]['metadata']['report']['counts']['retained']==1

def test_cwl_flags_defaults_and_safe_subset():
    content='''cwlVersion: v1.2
class: CommandLineTool
label: VCF statistics
baseCommand: [bcftools, stats]
requirements:
  DockerRequirement: {dockerPull: quay.io/biocontainers/bcftools:1.21--h3a4d415_1}
inputs:
  variants:
    type: File
    inputBinding: {position: 2}
  threads:
    type: int
    default: 2
    inputBinding: {position: 1, prefix: --threads}
outputs:
  report: {type: stdout}
stdout: stats.txt
'''
    result=onboarding.inspect({'content':content});draft=result['draft'];assert draft['inputs'][0]['type']=='VCF' and draft['parameters'][0]['default']==2
    assert draft['capture_stdout'] and draft['bindings'][0]['key']=='threads'
    with pytest.raises(ValueError):onboarding.inspect({'content':content.replace('baseCommand: [bcftools, stats]','baseCommand: [bcftools, stats]\narguments: ["$(evil())"]')})

def test_openapi_operations_and_private_network_denial():
    spec={'openapi':'3.1.0','servers':[{'url':'https://api.example.org'}],'paths':{'/variants/{id}':{'get':{'operationId':'getVariant','parameters':[{'name':'id','in':'path','required':True,'schema':{'type':'integer','minimum':1}}],'responses':{'200':{'description':'ok'}}}}}}
    result=onboarding.inspect({'kind':'openapi','content':json.dumps(spec)});assert result['draft']['api']['method']=='GET' and result['draft']['parameters'][0]['min']==1
    for url in ['https://127.0.0.1/','http://api.example.org/','https://169.254.169.254/','https://[::1]/']:
        with pytest.raises(ValueError):onboarding.request_public(url)

def test_results_index_filters_exports_and_tenant_scope(client,owner):
    folder=DATA/'uploads'/owner['id'];folder.mkdir(parents=True,exist_ok=True)
    item=vcf(folder/'annotated.vcf',[(10,'A','G',.001,'BRCA1','Pathogenic'),(20,'A','T',.2,'OTHER','Benign')]);aid=bind(owner['id'],item)
    assert client.post('/api/results/'+aid+'/index',json={}).status_code==200
    for _ in range(100):
        status=client.get('/api/results/'+aid+'/status').json()
        if status['state'] in ['ready','failed']:break
        time.sleep(.02)
    assert status['state']=='ready',status
    result=client.post('/api/results/'+aid+'/query',json={'filters':{'max_af':.01,'classification':'P'}}).json();assert result['matched']==1 and result['rows'][0]['gene']=='BRCA1'
    assert 'BRCA1' in client.post('/api/results/'+aid+'/export/tsv',json={'filters':{'gene':'BRCA1'}}).text
    assert 'chr22\t10' in client.post('/api/results/'+aid+'/export/vcf',json={'filters':{'gene':'BRCA1'}}).text
    pdf=client.post('/api/results/'+aid+'/export/pdf',json={'filters':{}});assert pdf.content.startswith(b'%PDF-')
    assert client.post('/api/results/'+aid+'/query',json={'filters':{'injection; DROP TABLE variants':'x'}}).status_code==422
    r=client.post('/api/auth/signup',json={'email':'outsider@example.test','password':'Other-pass-123456789'});client.headers['X-CSRF-Token']=r.json()['csrf']
    assert client.post('/api/results/'+aid+'/query',json={'filters':{}}).status_code==422

def test_cache_reuses_upstream_and_invalidates_only_downstream(client,owner):
    g=copy.deepcopy(registry.templates()[0]['graph'])
    def run(operation):
        r=client.post('/api/runs',json={'graph':g},headers={'Idempotency-Key':operation});assert r.status_code==200,r.text
        result=wait_run(client,r.json()['run']['id']);assert result['run']['state']=='succeeded',result
        return result
    run('resume-validation-first');second=run('resume-validation-second')
    assert all(a['metrics'].get('cached') for a in second['attempts'][:2])
    g['nodes'][1]['params']['max_reads']=10;third=run('resume-validation-third')
    assert third['attempts'][0]['metrics'].get('cached') is True
    assert not third['attempts'][1]['metrics'].get('cached')

def test_cache_integrity_rejects_modified_output(client,owner):
    folder=DATA/'uploads'/owner['id'];folder.mkdir(parents=True,exist_ok=True);p=folder/'result.txt';p.write_text('valid')
    out={'data':[artifact(p,'REPORT')]};cache.store(owner['id'],'integrity-test',out);assert cache.load(owner['id'],'integrity-test')
    p.write_text('corrupt');assert cache.load(owner['id'],'integrity-test') is None

def test_branch_local_reference_validation(client,owner):
    n=lambda i,t,p:{'id':i,'tool':t,'params':p,'x':0,'y':0,'resources':{'threads':1,'memory_mb':1024,'timeout_seconds':30}}
    nodes=[n('a','reference.asset',{'assembly':'GRCh37'}),n('b','reference.asset',{'assembly':'GRCh38'})]
    independent=graph.preflight({'nodes':nodes,'edges':[]},owner)
    assert not any(i['code']=='ASSEMBLY_MISMATCH' for i in independent['issues'])
    nodes.append(n('g','utility.gather',{}));joined=graph.preflight({'nodes':nodes,'edges':[{'id':'1','source':'a','sourcePort':'reference','target':'g','targetPort':'items'},{'id':'2','source':'b','sourcePort':'reference','target':'g','targetPort':'items'}]},owner)
    assert any(i['code']=='ASSEMBLY_MISMATCH' for i in joined['issues'])

def test_indexed_pileup_and_http_byte_ranges(client,owner):
    folder=DATA/'uploads'/owner['id']/'track-test';folder.mkdir(parents=True,exist_ok=True)
    result=execute({'tool':'sample.alignments','inputs':{},'workdir':str(folder)})
    alignment=bind(owner['id'],result['alignment'][0]);reference=bind(owner['id'],result['reference'][0])
    r=client.post('/api/tracks/pileup',json={'alignment_id':alignment,'reference_id':reference,'chrom':'engineering_contig','start':950,'end':1000})
    assert r.status_code==200,r.text
    data=r.json();assert len(data['reference'])==50 and len(data['reads'])==100 and max(data['coverage'])==100
    byte_range=client.get('/api/tracks/'+alignment+'/bytes',headers={'Range':'bytes=0-31'})
    assert byte_range.status_code==206 and len(byte_range.content)==32
    inspected=client.post('/api/assets/'+alignment+'/inspect',json={});assert inspected.status_code==200 and inspected.json()['metadata']['header_inspected']
    assert client.post('/api/tracks/pileup',json={'alignment_id':alignment,'reference_id':reference,'chrom':'engineering_contig','start':0,'end':3000}).status_code==422

def test_onboard_publish_parameters_compile_without_shell(client,owner,tmp_path):
    from backend.compiler import compile_tool
    draft={'name':'Reviewed scalar tool','image':'docker.io/example@sha256:'+'1'*64,'license':'FOSS','command':[['tool']], 'inputs':[{'key':'variants','type':'VCF'}],'outputs':[{'key':'report','type':'REPORT'}],'output_files':{'report':{'file':'result.json','type':'REPORT'}},'parameters':[registry.param('threads','Threads',2,'number',min=1,max=8)],'bindings':[{'key':'variants','file':True,'prefix':'--input','separate':True},{'key':'threads','file':False,'prefix':'--threads','separate':True}]}
    response=client.post('/api/onboarding/publish',json={'draft':draft,'reviewed':True});assert response.status_code==200,response.text
    spec=response.json();plan=compile_tool(spec,{'params':{'threads':4}}, {'variants':[{'path':'/data/test.vcf','metadata':{}}]},tmp_path)
    assert plan['steps'][0]['argv']==['tool','--input','/inputs/variants/0/test.vcf','--threads','4']
    before=len(registry.catalogue(owner['id']));draft['bindings'][0]['key']='nonexistent'
    assert client.post('/api/onboarding/publish',json={'draft':draft,'reviewed':True}).status_code==422
    assert len(registry.catalogue(owner['id']))==before

def test_existing_templates_have_valid_registry_port_contracts():
    assert len(registry.templates())>=12
    for template in registry.templates():
        graph.validate_shape(template['graph']);nodes={n['id']:n for n in template['graph']['nodes']}
        for edge in template['graph']['edges']:
            assert edge['sourcePort'] in {p['key'] for p in registry.BASE[nodes[edge['source']]['tool']]['outputs']}
            assert edge['targetPort'] in {p['key'] for p in registry.BASE[nodes[edge['target']]['tool']]['inputs']}

def test_exact_annotation_join_retains_source_scores_and_unmatched_alleles(tmp_path):
    from backend.portable import report
    item=vcf(tmp_path/'calls.vcf',[(10,'A','G',.001,'BRCA1','Uncertain_significance'),(20,'C','T',.002,'BRCA2','Benign')])
    source=report(tmp_path,'cadd-lookup.json',{'matches':[{'chrom':'chr22','pos':10,'ref':'A','alt':'G','selected_score':27.5}]})
    result=job(tmp_path,'annotation.merge',{'variants':[item],'reports':[source]})
    import pysam
    with pysam.VariantFile(result['variants'][0]['path']) as f:
        rows=list(f);assert rows[0].info['OPENNGS_CADD']==(27.5,) and 'OPENNGS_CADD' not in rows[1].info
        assert rows[0].info['CLNSIG']=='Uncertain_significance'
    assert result['report'][0]['metadata']['report']['clinical_validation'] is False

def test_multiallelic_grid_zygosity_and_info_cardinality(client,owner):
    folder=DATA/'uploads'/owner['id'];folder.mkdir(parents=True,exist_ok=True);path=folder/'multi.vcf'
    vcf(path,[(10,'A','G',.001,'BRCA1','Pathogenic')]);value=path.read_text().replace('\tA\tG\t60','\tA\tG,T\t60').replace('gnomAD_AF=0.001','gnomAD_AF=0.001,0.2').replace('0/1:30:50:15,15','0/2:30:50:15,0,15');path.write_text(value)
    aid=bind(owner['id'],artifact(path,'VCF'));workbench.build(owner['id'],aid,workbench.identity(owner['id'],workbench.owned(owner['id'],aid)))
    rows=workbench.query(owner['id'],aid,{})['rows'];assert rows[0]['zygosity']=='hom_ref' and rows[1]['zygosity']=='het'
    assert rows[0]['af']<.01 and rows[1]['af']>.1
