import pytest
from backend import postgres


class Raw:
    def __init__(self, answers):
        self.answers=iter(answers);self.calls=[];self.answer=False
    def execute(self, statement, args=()):
        self.calls.append(statement)
        if 'pg_try_advisory_xact_lock' in statement:self.answer=next(self.answers)
        return self
    def fetchone(self):return [self.answer]
    def cursor(self):return self


def connection(raw):
    c=object.__new__(postgres.Connection);c.raw=raw;c.transaction=False;return c


def test_writer_contention_retries_only_before_transaction_body(monkeypatch):
    raw=Raw([False,False,True]);c=connection(raw);waits=[]
    monkeypatch.setattr(postgres.time,'sleep',lambda seconds:waits.append(seconds))
    c.execute('BEGIN IMMEDIATE');c.execute('UPDATE reviewed_state SET value=?',('accepted',));c.commit()
    assert len(waits)==2 and raw.calls.count('ROLLBACK')==2
    assert raw.calls.count('UPDATE reviewed_state SET value=%s')==1
    assert raw.calls[-1]=='COMMIT' and not c.transaction
    assert all('pg_advisory_xact_lock(' not in q for q in raw.calls)


def test_exhaustion_is_bounded_and_leaves_no_open_transaction(monkeypatch):
    raw=Raw([False]*1000);c=connection(raw);clock=[0.0]
    monkeypatch.setattr(postgres.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(postgres.time,'sleep',lambda seconds:clock.__setitem__(0,clock[0]+seconds))
    with pytest.raises(TimeoutError,match='no transaction body'):c.execute('BEGIN IMMEDIATE')
    assert not c.transaction and raw.calls[-1]=='ROLLBACK' and clock[0]<8.1


def test_query_or_commit_errors_are_never_replayed(monkeypatch):
    raw=Raw([True]);c=connection(raw);c.execute('BEGIN IMMEDIATE')
    def broken(statement,args=()):
        raw.calls.append(statement)
        raise RuntimeError('uncertain commit')
    raw.execute=broken
    with pytest.raises(RuntimeError,match='uncertain commit'):c.commit()
    assert raw.calls.count('COMMIT')==1
