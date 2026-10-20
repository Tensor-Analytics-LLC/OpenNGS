"""Bounded parallel source reads retain ordered hashing/commit semantics."""
import gc
import hashlib
import ssl
import threading
import time
from urllib.error import HTTPError, URLError

import pytest

from scripts import acquire_exact_vep_archive as acquisition
from test_acquire_exact_vep_archive import HTTPResponse, MemoryS3, http_headers


class ParallelProvider:
    def __init__(self,raw,*,out_of_order=False,fail=False,block=False):
        self.raw=raw;self.requests=[];self.completed=[];self.lock=threading.Lock()
        self.gate=threading.Barrier(8) if out_of_order else None
        self.cancelled=False;self.release=threading.Event();self.fail=fail;self.block=block
    def head(self):return {}
    def cancel(self):self.cancelled=True;self.release.set()
    def read_range(self,offset,length):
        number=offset//8+1
        with self.lock:self.requests.append(number)
        if self.fail and number==3:raise ValueError('Synthetic changed source identity')
        if self.block:
            self.release.wait(2)
            if self.cancelled:raise TimeoutError('Synthetic cancelled read')
        if self.gate and number<=8:
            self.gate.wait(timeout=2);time.sleep((9-number)*.002)
        with self.lock:self.completed.append(number)
        return self.raw[offset:offset+length]


def original(monkeypatch):
    raw=bytes(range(251))*3
    monkeypatch.setattr(acquisition,'PART_BYTES',8)
    monkeypatch.setattr(acquisition,'ARCHIVE_BYTES',len(raw))
    monkeypatch.setattr(acquisition,'ARCHIVE_SHA256',hashlib.sha256(raw).hexdigest())
    owner=acquisition.migration.APPROVED_OWNER
    plan={'plan_id':acquisition.migration.APPROVED_PLAN,'owner_id':owner,'bucket':acquisition.migration.APPROVED_BUCKET,'kms':'synthetic-key'}
    obj={'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),'dataset_id':'original','key':'tenants/'+owner+'/datasets/original/cache.tar.gz'}
    return plan,obj,raw


def test_actual_out_of_order_http_results_hash_and_upload_in_original_order(tmp_path,monkeypatch):
    plan,obj,raw=original(monkeypatch);provider=ParallelProvider(raw,out_of_order=True);client=MemoryS3()
    result=acquisition.acquire_archive(client,plan,obj,provider,tmp_path,'cost_'+'a'*32)
    assert provider.completed[:8]!=list(range(1,9))
    assert provider.completed[0]>1
    # Upload starts may interleave; each unique part and final archive remain ordered.
    assert sorted(client.part_calls)==list(range(1,(len(raw)+7)//8+1))
    assert client.bodies[obj['key']]==raw and result['sha256']==hashlib.sha256(raw).hexdigest()
    assert client.complete_calls==1


def test_eight_buffer_bound_and_no_eager_archive_future_array(monkeypatch):
    monkeypatch.setattr(acquisition,'PART_BYTES',8)
    lock=threading.Lock();counts={'live':0,'peak':0,'requests':0}
    class Tracked(bytes):
        def __new__(cls,raw):
            value=super().__new__(cls,raw)
            with lock:
                counts['live']+=1;counts['peak']=max(counts['peak'],counts['live'])
            return value
        def __del__(self):
            with lock:counts['live']-=1
    class Provider:
        def read_range(self,offset,length):
            with lock:counts['requests']+=1
            return Tracked(bytes([offset%251])*length)
    stream=acquisition.ordered_ranges(Provider(),8*3013,lambda:None,lambda:10)
    number,raw=next(stream)
    assert number==1 and counts['requests']<=8 and counts['peak']<=8
    del raw
    for number,raw in stream:
        assert len(raw)==8
        del raw
    gc.collect()
    assert counts['requests']==3013 and counts['peak']<=8 and counts['live']==0


def test_future_source_identity_failure_cancels_window_and_never_commits(tmp_path,monkeypatch):
    plan,obj,raw=original(monkeypatch);provider=ParallelProvider(raw,fail=True,block=True);client=MemoryS3()
    with pytest.raises(ValueError,match='changed source identity'):
        acquisition.acquire_archive(client,plan,obj,provider,tmp_path,'cost_'+'a'*32)
    assert provider.cancelled and len(provider.requests)<=8
    assert client.complete_calls==0 and obj['key'] not in client.objects and client.puts==[]


def test_deadline_cancels_current_window_without_submitting_the_remaining_archive(tmp_path,monkeypatch):
    plan,obj,raw=original(monkeypatch);provider=ParallelProvider(raw,block=True);client=MemoryS3()
    with pytest.raises(TimeoutError):
        acquisition.acquire_archive(client,plan,obj,provider,tmp_path,'cost_'+'a'*32,expires_at=time.time()+.05)
    assert provider.cancelled and len(provider.requests)<=8
    assert client.complete_calls==0 and client.puts==[]


def response(length=8):
    return HTTPResponse(http_headers(**{'Content-Length':str(length),'Content-Range':f'bytes 0-{length-1}/{acquisition.ARCHIVE_BYTES}'}),b'x'*length)


def test_readonly_transient_retries_are_limited_to_same_pinned_range():
    provider=acquisition.Provider.__new__(acquisition.Provider);calls=[]
    events=[URLError(TimeoutError('synthetic timeout')),HTTPError(acquisition.SOURCE,503,'retry',{},None),response()]
    def request(method,headers):
        calls.append((method,headers));value=events.pop(0)
        if isinstance(value,Exception):raise value
        return value
    provider._open=request
    assert provider.read_range(0,8)==b'x'*8
    assert calls==[('GET',{'Range':'bytes=0-7'})]*3


@pytest.mark.parametrize('error,attempts',[
    (URLError(TimeoutError('timeout')),3),
    (HTTPError(acquisition.SOURCE,412,'source changed',{},None),1),
    (URLError(ssl.SSLCertVerificationError('certificate changed')),1)])
def test_transient_retry_cap_and_identity_or_certificate_fail_closed(error,attempts):
    provider=acquisition.Provider.__new__(acquisition.Provider);calls=[]
    def request(*args):calls.append(args);raise error
    provider._open=request
    with pytest.raises(Exception):provider.read_range(0,8)
    assert len(calls)==attempts


def test_changed_range_headers_are_never_retried():
    provider=acquisition.Provider.__new__(acquisition.Provider);calls=[]
    value=response();value.headers['ETag']='changed'
    def request(*args):calls.append(args);return value
    provider._open=request
    with pytest.raises(ValueError,match='identity'):provider.read_range(0,8)
    assert len(calls)==1
