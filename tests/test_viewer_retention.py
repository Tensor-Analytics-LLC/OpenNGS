import json
import time
import concurrent.futures
import threading
import pytest
from backend import db, remote_viewer, retention, viewer_retention, workbench


@pytest.fixture
def viewer(owner):
    uid=owner['id'];aid=db.ident('source_');rid=db.ident('run_');request=db.ident('viewer_')
    with db.tx() as c:
        for table in ['viewer_requests','viewer_cache_holds','retention_tombstones','retention_plans','retention_leases']:
            c.execute('DELETE FROM '+table)
        c.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?,?)',(aid,uid,'/original/source.vcf','original.vcf','VCF',20,time.time(),'variants','engineering','{}'))
        c.execute("INSERT INTO runs(id,user_id,operation_id,state,graph,created,finished,reserved) VALUES(?,?,?,'succeeded',?,?,?,1)",
                  (rid,uid,request,db.encode({'nodes':[{'tool':'asset.file','params':{'asset_id':aid}}]}),time.time()-100,time.time()-10))
        c.execute("INSERT INTO viewer_requests VALUES(?,?,?,?,?,?,'ready',?,NULL,?,?)",
                  (request,uid,aid,'variant-index','identity',rid,db.encode({'rows':1}),time.time()-5,time.time()))
    row=db.query('SELECT * FROM viewer_requests WHERE id=?',(request,),one=True)
    path=workbench.index_path(uid,aid)
    with workbench.connection(path) as c:
        c.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)')
        for key,value in {'identity':'identity','isolated_run':rid}.items():c.execute('INSERT INTO meta VALUES(?,?)',(key,db.encode(value)))
    return row,path


def deleted(row,rid=None):
    rid=rid or row['run_id'];now=time.time()
    with db.tx() as c:
        c.execute("INSERT INTO retention_plans VALUES(?,?,'deleted',?,?,?,?,?,NULL,0,?,?,NULL)",
                  (rid,row['user_id'],'private','tenants/'+row['user_id']+'/runs/'+rid+'/',now,now,'{}',now,now))


def test_deleted_worker_cleans_only_derived_cache_and_json(viewer):
    row,path=viewer;deleted(row)
    assert viewer_retention.cleanup()['requests_expired']==1
    assert not path.exists()
    after=db.query('SELECT * FROM viewer_requests WHERE id=?',(row['id'],),one=True)
    assert after['state']=='expired' and after['result'] is None
    assert db.query('SELECT * FROM assets WHERE id=?',(row['asset_id'],),one=True)['path']=='/original/source.vcf'
    assert db.query('SELECT * FROM runs WHERE id=?',(row['run_id'],),one=True)
    assert remote_viewer.status(row['user_id'],row['id'])['state']=='expired'
    assert viewer_retention.cleanup()['requests_expired']==0
    assert any('viewer.results.expired' in x['body'] for x in db.query('SELECT body FROM audit'))


def test_live_run_and_read_lease_protect_cache(viewer):
    row,path=viewer
    assert viewer_retention.cleanup()['requests_expired']==0
    viewer_retention.lease_index(row['user_id'],row['asset_id'])
    assert db.query('SELECT * FROM retention_leases WHERE run_id=?',(row['run_id'],),one=True)
    deleted(row)
    assert viewer_retention.cleanup()['requests_expired']==0 and path.exists()
    with db.tx() as c:c.execute('DELETE FROM retention_leases')
    assert viewer_retention.cleanup()['requests_expired']==1 and not path.exists()


def test_worker_expiry_preserves_newer_replacement_index(viewer):
    row,path=viewer;deleted(row)
    with workbench.connection(path) as c:c.execute("UPDATE meta SET value=? WHERE key='isolated_run'",(db.encode('newer-run'),))
    assert viewer_retention.cleanup()['requests_expired']==1 and path.exists()
    assert viewer_retention.index_metadata(row['user_id'],row['asset_id'])['isolated_run']=='newer-run'


def test_newer_ingestion_hold_protects_same_asset_cache(viewer):
    row,path=viewer;deleted(row)
    with db.tx() as c:
        c.execute("INSERT INTO viewer_requests VALUES(?,?,?,?,?,?,'ingesting',NULL,NULL,?,?)",
                  ('newer',row['user_id'],row['asset_id'],'variant-index','new-identity','new-run',time.time(),time.time()))
        c.execute('INSERT INTO viewer_cache_holds VALUES(?,?,?,?)',('newer',row['user_id'],100,time.time()+60))
    assert viewer_retention.cleanup()['requests_expired']==0 and path.exists()
    with db.tx() as c:c.execute("DELETE FROM viewer_cache_holds WHERE id='newer'")
    assert viewer_retention.cleanup()['requests_expired']==0 and path.exists()


def test_cleanup_identity_check_and_unlink_serialize_with_new_index_publish(viewer,monkeypatch):
    row,path=viewer;deleted(row)
    replacement=path.with_suffix('.new');replacement.write_bytes(b'new verified generation')
    checking=threading.Event();release=threading.Event();writer_started=threading.Event();published=threading.Event()
    unlink=viewer_retention._unlink
    def paused(*args,**kwargs):
        checking.set();assert release.wait(3)
        return unlink(*args,**kwargs)
    monkeypatch.setattr(viewer_retention,'_unlink',paused)
    def publish():
        writer_started.set()
        with db.tx():replacement.replace(path)
        published.set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        cleanup=pool.submit(viewer_retention.cleanup)
        assert checking.wait(3)
        writer=pool.submit(publish);assert writer_started.wait(3)
        assert not published.wait(.05)
        release.set();assert cleanup.result(timeout=3)['requests_expired']==1
        writer.result(timeout=3)
    assert path.read_bytes()==b'new verified generation'


def test_missing_ready_index_reports_regenerate_without_worker_submission(viewer):
    row,path=viewer;path.unlink()
    status=remote_viewer.status(row['user_id'],row['id'])
    assert status['state']=='regenerate' and 'Prepare again' in status['error']
    assert db.query('SELECT result FROM viewer_requests WHERE id=?',(row['id'],),one=True)['result'] is None
    with pytest.raises(ValueError,match='Prepare again'):viewer_retention.lease_index(row['user_id'],row['asset_id'])


def test_symlink_never_deletes_target_and_cleanup_retries(viewer,tmp_path):
    row,path=viewer;deleted(row);path.unlink()
    source=tmp_path/'source.vcf';source.write_text('source preserved')
    path.symlink_to(source)
    assert viewer_retention.cleanup()['deferred']==1
    assert source.read_text()=='source preserved' and path.is_symlink()
    path.unlink()
    assert viewer_retention.cleanup()['deferred']==0


def test_tombstoned_artifact_cache_is_removed_but_other_tenants_preserved(viewer):
    row,path=viewer;aid='expired-output';rid='old-source-run';deleted(row,rid)
    old=workbench.index_path(row['user_id'],aid);old.write_bytes(b'cache')
    other=workbench.index_path('other-tenant',aid);other.write_bytes(b'other cache')
    with db.tx() as c:
        c.execute('INSERT INTO retention_tombstones VALUES(?,?,?,?,?)',(aid,row['user_id'],rid,'{}',time.time()))
    result=viewer_retention.cleanup()
    assert result['cache_bytes']==5 and not old.exists() and other.read_bytes()==b'other cache'
    assert path.exists() and db.query('SELECT state FROM viewer_requests WHERE id=?',(row['id'],),one=True)['state']=='ready'


def test_reference_dependency_expiration_invalidates_pileup_json(viewer):
    row,path=viewer;reference='expired-reference';rid='reference-run';deleted(row,rid)
    with db.tx() as c:
        c.execute('INSERT INTO retention_tombstones VALUES(?,?,?,?,?)',(reference,row['user_id'],rid,'{}',time.time()))
        c.execute('UPDATE runs SET graph=? WHERE id=?',(db.encode({'nodes':[{'tool':'asset.file','params':{'asset_id':reference}}]}),row['run_id']))
        c.execute("UPDATE viewer_requests SET operation='pileup' WHERE id=?",(row['id'],))
    assert viewer_retention.cleanup()['requests_expired']==1
    assert path.exists()  # A pileup request never authorizes deleting an index.
    assert db.query('SELECT result FROM viewer_requests WHERE id=?',(row['id'],),one=True)['result'] is None
