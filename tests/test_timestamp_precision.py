import contextlib

import pytest

from backend import db
from scripts.promote_timestamp_precision import fingerprint, promote


class Result:
    def __init__(self, rows):
        self.rows = rows
    def fetchone(self):
        return self.rows[0] if self.rows else None
    def fetchall(self):
        return self.rows


class Connection:
    def __init__(self, busy=False):
        self.columns = {'created','updated'}
        self.busy = busy
        self.statements = []
    @contextlib.contextmanager
    def transaction(self):
        old = self.columns.copy()
        try:
            yield
        except Exception:
            self.columns = old
            raise
    def execute(self, statement, args=()):
        text = statement if isinstance(statement,str) else statement.as_string()
        self.statements.append(text)
        if 'pg_try_advisory' in text:
            return Result([{'locked':not self.busy}])
        if "data_type='real'" in text:
            return Result([{'column_name':name} for name in self.columns])
        if text.startswith('ALTER TABLE'):
            assert text.startswith('ALTER TABLE public."datasets" ')
            assert 'USING "created"::double precision' in text
            assert 'USING "updated"::double precision' in text
            self.columns.clear()
        if "data_type!='double precision'" in text:
            return Result([{'column_name':name} for name in self.columns])
        return Result([])


def plan():
    columns=[{'table_name':'datasets','column_name':name} for name in ['created','updated']]
    return {'columns':columns,'tables':{'datasets':{'rows':360}},'plan_sha256':fingerprint(columns)}


def test_reviewed_precision_promotion_is_bounded_and_idempotent(client,monkeypatch):
    audit=[]
    monkeypatch.setattr(db,'audit',lambda *args,**kwargs:audit.append(args))
    connection=Connection();reviewed=plan()
    first=promote(connection,reviewed,reviewed['plan_sha256'])
    assert first[0]['state']=='promoted' and len(audit)==1
    assert "SET LOCAL lock_timeout='500ms'" in connection.statements
    assert "SET LOCAL statement_timeout='5s'" in connection.statements
    assert len([s for s in connection.statements if s.startswith('ALTER TABLE')])==1
    second=promote(connection,reviewed,reviewed['plan_sha256'])
    assert second[0]['state']=='already-promoted' and len(audit)==1


def test_precision_promotion_refuses_unreviewed_plan_and_defers_busy_app(client):
    connection=Connection(busy=True);reviewed=plan()
    with pytest.raises(ValueError,match='exact current'):
        promote(connection,reviewed,'wrong')
    assert not connection.statements
    result=promote(connection,reviewed,reviewed['plan_sha256'])
    assert result[0]['state']=='deferred'
    assert not any(s.startswith('ALTER TABLE') for s in connection.statements)


def test_precision_change_rolls_back_if_audit_write_fails(client,monkeypatch):
    def fail(*args,**kwargs):
        raise RuntimeError('Audit is unavailable')
    monkeypatch.setattr(db,'audit',fail)
    connection=Connection();reviewed=plan()
    result=promote(connection,reviewed,reviewed['plan_sha256'])
    assert result[0]['state']=='deferred'
    assert connection.columns=={'created','updated'}
