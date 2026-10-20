import io
import json
import os
import subprocess
import shutil
import sys
import time
from pathlib import Path
import pytest
from backend import db, workbench, remote_viewer, retention
from backend.portable import execute, artifact
from backend.viewer_protocol import COLUMNS, MAX_LINE, decode, validate_row
from backend.config import ROOT, DATA
from test_extensions import vcf, bind

def test_isolated_allele_rows_roundtrip_and_filtered_export(client, owner, tmp_path, monkeypatch):
    item=vcf(tmp_path/'private.vcf',[(10,'A','G',.001,'BRCA1','Pathogenic'),
                                  (20,'A','T',.2,"';DROP TABLE variants;--",'Benign')])
    output=execute({'tool':'results.prepare','inputs':{'data':[item]},
                    'params':{'operation':'variant-index'},'workdir':str(tmp_path/'prepared')})['report'][0]
    aid=bind(owner['id'],item)
    prepared=bind(owner['id'],output)
    body=Path(output['path']).read_bytes()
    monkeypatch.setattr(remote_viewer,'stream',lambda uid,a,maximum:io.BytesIO(body))
    request={'asset_id':aid,'identity':'known-identity','run_id':'isolated-proof'}
    result=remote_viewer.ingest_index(owner['id'],request,workbench.owned(owner['id'],prepared))
    assert result['rows']==2
    filtered=workbench.query(owner['id'],aid,{'max_af':.01})
    assert filtered['matched']==1 and filtered['rows'][0]['gene']=='BRCA1'
    assert workbench.query(owner['id'],aid,{})['total']==2
    with pytest.raises(ValueError,match='workspace'):
        workbench.query('u_foreign',aid,{})
    assert 'chr22\t10' in ''.join(workbench.export(owner['id'],aid,{'max_af':.01},'vcf'))

def test_interchange_rejects_malformed_overlong_and_nonfinite_rows():
    with pytest.raises(ValueError):decode(b'x'*(MAX_LINE+1))
    with pytest.raises(ValueError):decode(b'[NaN]')
    with pytest.raises(ValueError):validate_row(['unexpected'])
    row=['']*len(COLUMNS)
    for key in ['af','cadd','revel','alphamissense','spliceai','dp','gq','vaf','qual','cosmic']:
        row[COLUMNS.index(key)]=None
    row[COLUMNS.index('pos')]=-1
    with pytest.raises(ValueError,match='position'):validate_row(row)

def test_private_worker_does_not_publish_truncated_index(tmp_path):
    item=vcf(tmp_path/'large.vcf',[(10,'A','G',.001,'GENE','Pathogenic')])
    with pytest.raises(ValueError,match='no partial'):
        execute({'tool':'results.prepare','inputs':{'data':[item]},'params':{'operation':'variant-index'},
                 'workdir':str(tmp_path/'bounded'),'max_output_bytes':20})
    assert not (tmp_path/'bounded/result.json').exists()

def test_ephemeral_worker_pileup_on_known_alignment(tmp_path):
    work=tmp_path/'worker';work.mkdir()
    source=work/'inputs'
    shutil.copytree(ROOT/'demo-data/sample.alignments',source)
    alignment=artifact(source/'engineering.bam','INDEXED_BAM',assembly='engineering',index=str(source/'engineering.bam.bai'))
    reference=artifact(source/'engineering.fa','FASTA',assembly='engineering',index=str(source/'engineering.fa.fai'))
    job={'tool':'results.prepare','params':{'operation':'pileup','chrom':'engineering_contig','start':900,'end':1100},
         'inputs':{'data':[alignment],'reference':[reference]},'workdir':str(work)}
    path=work/'job.json';path.write_text(json.dumps(job))
    env={**os.environ,'NGS_STATE_DIR':str(work/'state'),'NGS_DATA_DIR':str(work/'data'),
         'NGS_DEPLOYMENT':'desktop','NGS_DATABASE_URL':'','NGS_DEMO':'0','PYTHONPATH':str(ROOT)}
    result=subprocess.run([sys.executable,'-m','backend.portable',str(path)],cwd=ROOT,env=env,capture_output=True,timeout=30)
    assert result.returncode==0,result.stderr.decode()
    response=json.loads((work/'viewer-result.json').read_text())
    assert len(response['result']['reads'])==100
    assert max(response['result']['coverage'])==100

def test_retention_routes_reject_foreign_pin(client,owner):
    with pytest.raises(ValueError,match='workspace|found'):
        retention.pin(owner['id'],'run_foreign',True)
    assert client.get('/api/operations/retention').status_code==200
    assert client.patch('/api/operations/admin/retention',json={'enabled':'true','reason':'test'}).status_code==422
