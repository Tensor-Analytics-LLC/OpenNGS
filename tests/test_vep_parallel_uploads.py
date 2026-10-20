"""Synthetic concurrency and recovery proofs; no provider or AWS access."""
from collections import Counter
import gc
import hashlib
import threading
import time

import pytest

from scripts import acquire_exact_vep_archive as acquisition
from test_acquire_exact_vep_archive import MemoryProvider, MemoryS3, RESERVATION, saved


def original(monkeypatch, count=40):
    raw=bytes(range(251))*count
    monkeypatch.setattr(acquisition,'PART_BYTES',64)
    monkeypatch.setattr(acquisition,'ARCHIVE_BYTES',len(raw))
    monkeypatch.setattr(acquisition,'ARCHIVE_SHA256',hashlib.sha256(raw).hexdigest())
    owner=acquisition.migration.APPROVED_OWNER
    obj={'name':acquisition.ARCHIVE_NAME,'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),
         'dataset_id':'synthetic-original','key':f'tenants/{owner}/datasets/synthetic-original/cache.tar.gz'}
    plan={'plan_id':acquisition.migration.APPROVED_PLAN,'owner_id':owner,
          'bucket':acquisition.migration.APPROVED_BUCKET,'kms':'synthetic-key'}
    return plan,obj,raw


class DelayedS3(MemoryS3):
    """Consume request bodies as a network would, retaining only byte copies."""
    def __init__(self,*,reverse=True,delay=.001):
        super().__init__();self.reverse=reverse;self.delay=delay;self.active=0;self.peak=0
        self.lock=threading.Lock();self.second_done=threading.Event();self.acks=[];self.upload_ids=[]
    def upload_part(self,**request):
        number=request['PartNumber']
        with self.lock:
            self.active+=1;self.peak=max(self.peak,self.active);self.upload_ids.append(request['UploadId'])
        try:
            if self.reverse and number==1:
                assert self.second_done.wait(2),'Part two must be admitted before waiting for part one'
            if self.delay:time.sleep(self.delay)
            # The in-memory fake represents remote storage. Do not retain the
            # producer's tracked raw body in its simulated remote byte store.
            copied={**request,'Body':memoryview(request['Body']).tobytes()}
            response=super().upload_part(**copied)
            with self.lock:self.acks.append(number)
            if number==2:self.second_done.set()
            return response
        finally:
            with self.lock:self.active-=1


def test_out_of_order_s3_acknowledgments_keep_real_whole_hash_and_ordered_commit(tmp_path,monkeypatch):
    plan,obj,raw=original(monkeypatch);client=DelayedS3();provider=MemoryProvider(raw)
    coordinator=threading.get_ident();checkpoints=[];original_checkpoint=acquisition.checkpoint
    from contextlib import contextmanager
    @contextmanager
    def checkpoint(*args,**kwargs):
        with original_checkpoint(*args,**kwargs) as (state,save):
            def capture(**changes):
                assert threading.get_ident()==coordinator
                if 'parts' in changes:
                    checkpoints.append((list(map(int,changes['parts'])),changes['provider_bytes']))
                return save(**changes)
            yield state,capture
    monkeypatch.setattr(acquisition,'checkpoint',checkpoint)
    def before_complete(request):
        assert client.active==0
        value=saved(tmp_path,obj)
        assert value['verified_original_sha256']==hashlib.sha256(raw).hexdigest()
        assert value['provider_bytes']==len(raw)
        assert request['IfNoneMatch']=='*'
        assert [p['PartNumber'] for p in request['MultipartUpload']['Parts']]==list(range(1,(len(raw)+63)//64+1))
    client.complete_hook=before_complete
    result=acquisition.acquire_archive(client,plan,obj,provider,tmp_path,RESERVATION)
    assert client.acks[:2]==[2,1] and client.peak==2
    assert result['sha256']==hashlib.sha256(raw).hexdigest() and client.bodies[obj['key']]==raw
    assert all(numbers==list(range(1,len(numbers)+1)) and count==min(len(raw),64*len(numbers)) for numbers,count in checkpoints)
    assert client.create_calls==client.complete_calls==1


def test_reader_and_queued_uploader_references_share_eight_raw_buffers(tmp_path,monkeypatch):
    plan,obj,raw=original(monkeypatch,count=100);lock=threading.Lock();counts={'live':0,'peak':0,'reads':0}
    returned=[];upload_tasks=[];executor=acquisition.concurrent.futures.ThreadPoolExecutor
    class RetainingExecutor(executor):
        def submit(self,function,*args,**kwargs):
            if getattr(function,'__name__','')=='read':
                def retain_result():
                    result=function(*args,**kwargs);returned.append(result)
                    return result
                return super().submit(retain_result)
            # Simulate executor WorkItems retaining their callable or Future
            # arguments after completion. Neither may retain the raw payload.
            upload_tasks.append(function)
            return super().submit(function,*args,**kwargs)
    monkeypatch.setattr(acquisition.concurrent.futures,'ThreadPoolExecutor',RetainingExecutor)
    class Tracked(bytes):
        def __new__(cls,value):
            result=super().__new__(cls,value)
            with lock:
                counts['live']+=1;counts['peak']=max(counts['peak'],counts['live'])
            return result
        def __del__(self):
            with lock:counts['live']-=1
    class Provider(MemoryProvider):
        def read_range(self,offset,length):
            with lock:counts['reads']+=1
            return Tracked(self.raw[offset:offset+length])
    client=DelayedS3(delay=.001)
    acquisition.acquire_archive(client,plan,obj,Provider(raw),tmp_path,RESERVATION)
    gc.collect()
    assert client.peak==2
    assert counts['reads']==(len(raw)+63)//64
    assert counts['peak']<=8 and counts['live']==0
    assert len(returned)==counts['reads'] and all(result.raw is None for result in returned)
    assert len(upload_tasks)==counts['reads'] and all(task.raw is None for task in upload_tasks)
    assert acquisition.PART_BYTES*acquisition.PREFETCH_PARTS==64*8


def test_partial_resume_reuses_same_layout_and_rehashes_every_original_byte(tmp_path,monkeypatch):
    plan,obj,raw=original(monkeypatch,count=5);client=DelayedS3(reverse=False);client.fail_part=4
    with pytest.raises(ConnectionError,match='interrupted part'):
        acquisition.acquire_archive(client,plan,obj,MemoryProvider(raw),tmp_path,RESERVATION)
    before=Counter(client.part_calls);upload_id=saved(tmp_path,obj)['upload_id']
    confirmed={number:item['checksum'] for number,item in client.uploads[upload_id]['parts'].items()}
    assert confirmed and client.complete_calls==0
    provider=MemoryProvider(raw)
    result=acquisition.acquire_archive(client,plan,obj,provider,tmp_path,RESERVATION)
    after=Counter(client.part_calls)
    assert all(after[number]==before[number] for number in confirmed)
    assert sorted(provider.ranges)==[(offset,min(64,len(raw)-offset)) for offset in range(0,len(raw),64)]
    assert result['sha256']==hashlib.sha256(raw).hexdigest()
    assert client.create_calls==client.complete_calls==1
    assert set(client.upload_ids)=={upload_id} and client.completions[0]['UploadId']==upload_id
    assert client.bodies[obj['key']]==raw


def test_later_failure_preserves_earlier_acknowledged_checkpoint_prefix(tmp_path,monkeypatch):
    plan,obj,raw=original(monkeypatch,count=2)
    class FailSecond(DelayedS3):
        def __init__(self):super().__init__(reverse=False,delay=0);self.first_started=threading.Event();self.second_failed=threading.Event()
        def upload_part(self,**request):
            if request['PartNumber']==2:
                assert self.first_started.wait(2)
                self.second_failed.set()
                raise ConnectionError('later concurrent part failed')
            self.first_started.set()
            assert self.second_failed.wait(2)
            time.sleep(.01)
            return super().upload_part(**request)
    client=FailSecond()
    with pytest.raises(ConnectionError,match='later concurrent part failed'):
        acquisition.acquire_archive(client,plan,obj,MemoryProvider(raw),tmp_path,RESERVATION)
    value=saved(tmp_path,obj)
    assert list(value['parts'])==['1'] and value['provider_bytes']==64
    assert client.acks==[1] and client.complete_calls==0 and not client.puts


def test_bad_final_source_hash_cannot_commit_even_when_all_s3_parts_acknowledge(tmp_path,monkeypatch):
    plan,obj,raw=original(monkeypatch,count=2);client=DelayedS3()
    with pytest.raises(ValueError,match='inventoried SHA-256'):
        acquisition.acquire_archive(client,plan,obj,MemoryProvider(raw[:-1]+b'X'),tmp_path,RESERVATION)
    assert len(client.acks)==(len(raw)+63)//64
    assert saved(tmp_path,obj)['state']=='failed-sha256'
    assert client.complete_calls==0 and not client.puts


def test_deadline_and_upload_failure_never_commit_or_replace_private_upload(tmp_path,monkeypatch):
    plan,obj,raw=original(monkeypatch,count=10);client=DelayedS3(reverse=False,delay=.08)
    with pytest.raises(TimeoutError):
        acquisition.acquire_archive(client,plan,obj,MemoryProvider(raw),tmp_path,RESERVATION,expires_at=time.time()+.02)
    upload_id=saved(tmp_path,obj)['upload_id']
    assert client.create_calls==1 and client.complete_calls==0 and not client.puts
    assert upload_id in client.uploads and client.active==0
    assert len(client.part_calls)<=2
    # A fresh admission can reuse and independently verify any requests which
    # committed after their caller timed out, without another Create/Complete.
    client.delay=0
    acquisition.acquire_archive(client,plan,obj,MemoryProvider(raw),tmp_path,RESERVATION)
    assert client.create_calls==client.complete_calls==1


def test_identical_part_retries_stay_bounded_and_admission_checked_between_retries(tmp_path,monkeypatch):
    plan,obj,raw=original(monkeypatch,count=1)
    class Lost(DelayedS3):
        def __init__(self):super().__init__(reverse=False,delay=0);self.lost=[]
        def upload_part(self,**request):
            if request['PartNumber']==1:
                self.lost.append((request['UploadId'],bytes(request['Body']),request['ChecksumSHA256']))
                raise acquisition.ConnectionClosedError(endpoint_url='https://synthetic.invalid')
            return super().upload_part(**request)
    client=Lost()
    with pytest.raises(acquisition.ConnectionClosedError):
        acquisition.acquire_archive(client,plan,obj,MemoryProvider(raw),tmp_path,RESERVATION)
    assert len(client.lost)==3 and len(set(client.lost))==1
    assert client.create_calls==1 and client.complete_calls==0 and not client.puts


def test_source_identity_failure_cancels_concurrent_upload_retries(tmp_path,monkeypatch):
    plan,obj,raw=original(monkeypatch,count=2);started=threading.Event();cancelled=threading.Event()
    class Provider(MemoryProvider):
        def read_range(self,offset,length):
            if offset==64:
                assert started.wait(2)
                raise ValueError('reviewed source identity changed')
            if offset:
                assert cancelled.wait(2)
            return super().read_range(offset,length)
        def cancel(self):cancelled.set()
    class Lost(MemoryS3):
        def __init__(self):super().__init__();self.attempts=0
        def upload_part(self,**request):
            self.attempts+=1;started.set()
            assert cancelled.wait(2)
            raise acquisition.ConnectionClosedError(endpoint_url='https://synthetic.invalid')
    client=Lost()
    with pytest.raises(ValueError,match='reviewed source identity changed'):
        acquisition.acquire_archive(client,plan,obj,Provider(raw),tmp_path,RESERVATION)
    assert client.attempts==1 and client.create_calls==1
    assert client.complete_calls==0 and not client.puts
