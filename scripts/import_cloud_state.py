"""One-shot, insert-only import of a consistent SQLite backup into PostgreSQL.

Never truncates existing tables. Checks row counts and the audit chain. A second
run accepts identical rows and refuses conflicting values.
"""
import json
import hashlib
import os
import sqlite3
from pathlib import Path
from backend import db,cache,budget,cloud_data

def import_state(path):
    fingerprint=hashlib.sha256(Path(path).read_bytes()).hexdigest()
    marker=db.query("SELECT value FROM controls WHERE key='sqlite_import_complete'",one=True)
    if marker:
        if marker['value']!=fingerprint:raise ValueError('A different initial database was already imported')
        return {'already_imported':True,'source_sha256':fingerprint}
    source=sqlite3.connect(path);source.row_factory=sqlite3.Row
    tables=['users','sessions','settings','graphs','runs','attempts','events','artifacts','assets','scans','secrets','transfers','audit','custom_tools','quarantine','controls','node_cache','file_digests']
    counts={}
    with db.tx() as c:
        for table in tables:
            rows=source.execute('SELECT * FROM '+table).fetchall()
            columns=[r[1] for r in source.execute('PRAGMA table_info('+table+')')]
            primary=[r[1] for r in source.execute('PRAGMA table_info('+table+')') if r[5]]
            for row in rows:
                where=' AND '.join(k+'=?' for k in primary)
                prior=c.execute('SELECT * FROM '+table+' WHERE '+where,tuple(row[k] for k in primary)).fetchone()
                if prior:
                    if any(prior[k]!=row[k] for k in columns):raise ValueError('Import conflicts with existing '+table+' data')
                    continue
                c.execute('INSERT INTO '+table+'('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',tuple(row))
            counts[table]=len(rows)
        if os.environ.get('NGS_DATABASE_URL'):
            for table in ['events','audit']:
                c.raw.execute("SELECT setval(pg_get_serial_sequence(%s,'seq'),GREATEST(COALESCE((SELECT MAX(seq) FROM "+table+"),0),1),true)",(table,))
    from backend.security import verify_audit
    audit=verify_audit()
    if not audit['valid']:raise ValueError('Imported audit chain failed verification')
    with db.tx() as c:c.execute("INSERT INTO controls(key,value) VALUES('sqlite_import_complete',?) ON CONFLICT(key) DO NOTHING",(fingerprint,))
    source.close()
    return {'counts':counts,'audit':audit}

if __name__=='__main__':
    import sys
    try:print(json.dumps(import_state(sys.argv[1])))
    finally:
        if os.environ.get('NGS_DATABASE_URL'):
            from backend.postgres import close
            close()
