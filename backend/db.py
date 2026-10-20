import contextlib
import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from .config import STATE, TRIAL_SECONDS, DATABASE_URL

LOCK = threading.RLock()
PATH = STATE / 'studio.sqlite'

def ident(prefix=''):
    return prefix + uuid.uuid4().hex

def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)

def connect():
    if DATABASE_URL:
        from .postgres import Connection
        return Connection()
    c = sqlite3.connect(PATH, timeout=30, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    c.execute('PRAGMA busy_timeout=30000')
    return c

@contextlib.contextmanager
def tx():
    with LOCK, contextlib.closing(connect()) as c:
        c.execute('BEGIN IMMEDIATE')
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise

def query(sql, args=(), one=False):
    with contextlib.closing(connect()) as c:
        rows = [dict(r) for r in c.execute(sql, args)]
    return (rows[0] if rows else None) if one else rows

def audit(c, actor, action, resource='', detail=None):
    prev = c.execute('SELECT hash FROM audit ORDER BY seq DESC LIMIT 1').fetchone()
    previous = prev[0] if prev else '0' * 64
    event = {'event_id': ident('a_'), 'time': time.time(), 'actor': actor, 'action': action, 'resource': resource, 'detail': detail or {}}
    body = encode(event)
    digest = hashlib.sha256(b'NGS-LOCAL-AUDIT-v1\0' + bytes.fromhex(previous) + body.encode()).hexdigest()
    c.execute('INSERT INTO audit(body,previous_hash,hash) VALUES(?,?,?)', (body, previous, digest))
    return digest

def event(run_id, kind, node_id=None, **payload):
    with tx() as c:
        c.execute('INSERT INTO events(run_id,node_id,kind,at,payload) VALUES(?,?,?,?,?)', (run_id,node_id,kind,time.time(),encode(payload)))

def initialize():
    with contextlib.closing(connect()) as c:
        if not DATABASE_URL:c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,email TEXT UNIQUE NOT NULL,password TEXT NOT NULL,role TEXT NOT NULL,tier TEXT NOT NULL DEFAULT 'trial',frozen INTEGER NOT NULL DEFAULT 0,quota_seconds INTEGER NOT NULL DEFAULT 28800,used_seconds INTEGER NOT NULL DEFAULT 0,reserved_seconds INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),csrf TEXT NOT NULL,expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS settings(user_id TEXT PRIMARY KEY REFERENCES users(id),body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS graphs(id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),name TEXT NOT NULL,revision INTEGER NOT NULL,body TEXT NOT NULL,updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),operation_id TEXT NOT NULL,state TEXT NOT NULL,graph TEXT NOT NULL,created REAL NOT NULL,started REAL,finished REAL,reserved INTEGER NOT NULL,used INTEGER NOT NULL DEFAULT 0,cancel_requested INTEGER NOT NULL DEFAULT 0,error TEXT,UNIQUE(user_id,operation_id));
        CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES runs(id),node_id TEXT NOT NULL,state TEXT NOT NULL,started REAL,finished REAL,pid INTEGER,provider_id TEXT,outputs TEXT NOT NULL DEFAULT '{}',metrics TEXT NOT NULL DEFAULT '{}',error TEXT);
        CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL REFERENCES runs(id),node_id TEXT,kind TEXT NOT NULL,at REAL NOT NULL,payload TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS events_run_seq ON events(run_id,seq);
        CREATE TABLE IF NOT EXISTS artifacts(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,run_id TEXT,node_id TEXT,name TEXT NOT NULL,path TEXT NOT NULL,format TEXT NOT NULL,bytes INTEGER NOT NULL,digest TEXT,metadata TEXT NOT NULL DEFAULT '{}',created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS assets(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,path TEXT NOT NULL,name TEXT NOT NULL,format TEXT NOT NULL,bytes INTEGER NOT NULL,mtime REAL NOT NULL,family TEXT NOT NULL,assembly TEXT,metadata TEXT NOT NULL,UNIQUE(user_id,path));
        CREATE TABLE IF NOT EXISTS scans(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,state TEXT NOT NULL,root TEXT NOT NULL,files INTEGER NOT NULL DEFAULT 0,bytes INTEGER NOT NULL DEFAULT 0,error TEXT,started REAL NOT NULL,finished REAL);
        CREATE TABLE IF NOT EXISTS secrets(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,provider TEXT NOT NULL,label TEXT NOT NULL,binding TEXT NOT NULL,encrypted TEXT NOT NULL,key_envelope TEXT,mode TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,revoked INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL,UNIQUE(user_id,label));
        CREATE TABLE IF NOT EXISTS transfers(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,asset_id TEXT NOT NULL,state TEXT NOT NULL,bytes INTEGER NOT NULL DEFAULT 0,total INTEGER NOT NULL,error TEXT,s3_uri TEXT,digest TEXT,started REAL NOT NULL,finished REAL,cancel_requested INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS audit(seq INTEGER PRIMARY KEY AUTOINCREMENT,body TEXT NOT NULL,previous_hash TEXT NOT NULL,hash TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS custom_tools(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,body TEXT NOT NULL,approved INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS quarantine(tool_id TEXT PRIMARY KEY,reason TEXT NOT NULL,at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS controls(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        ''')
        for statement in [
            'CREATE INDEX IF NOT EXISTS assets_user_name ON assets(user_id,name)',
            'CREATE INDEX IF NOT EXISTS runs_user_created ON runs(user_id,created)',
            'CREATE INDEX IF NOT EXISTS runs_state_created ON runs(state,created)',
            'CREATE INDEX IF NOT EXISTS attempts_run ON attempts(run_id)',
            'CREATE INDEX IF NOT EXISTS artifacts_user_run ON artifacts(user_id,run_id)',
            'CREATE INDEX IF NOT EXISTS transfers_user_state ON transfers(user_id,state)']:
            c.execute(statement)
    if not DATABASE_URL:os.chmod(PATH, 0o600)

def recover():
    # A process restart cannot prove a prior external workload stopped.
    # Interrupted reservations remain held until explicit reconciliation.
    with tx() as c:
        rows = c.execute("SELECT id FROM runs WHERE state IN ('queued','running','cancelling')").fetchall()
        for row in rows:
            c.execute("UPDATE runs SET state='interrupted',error='Service restarted; reservation held pending resource reconciliation',finished=? WHERE id=?", (time.time(),row['id']))
            audit(c, 'system', 'run.interrupted', row['id'])

initialize()
