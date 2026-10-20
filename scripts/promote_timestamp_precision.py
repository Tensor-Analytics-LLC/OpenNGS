"""Audit or explicitly promote legacy PostgreSQL REAL columns without data resets.

The default is read-only. Applying requires the exact reviewed plan fingerprint.
Each table commits independently, under a 500 ms lock timeout and 5 s statement
timeout; a busy table is deferred. Existing rounded values are widened exactly.
Lost historical timestamp precision cannot be reconstructed.
"""
import argparse
import hashlib
import json
import time
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from backend.config import DATABASE_URL


def fingerprint(columns):
    targets = sorted((row['table_name'], row['column_name']) for row in columns)
    return hashlib.sha256(json.dumps(targets, separators=(',', ':')).encode()).hexdigest()


def inspect(connection):
    columns = connection.execute(
        "SELECT table_name,column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND data_type='real' "
        "ORDER BY table_name,ordinal_position").fetchall()
    tables = {}
    for table in sorted({row['table_name'] for row in columns}):
        size = connection.execute(
            'SELECT pg_total_relation_size(%s::regclass) AS bytes',
            (f'public."{table.replace(chr(34), chr(34)*2)}"',)).fetchone()['bytes']
        count = connection.execute(
            sql.SQL('SELECT COUNT(*) AS count FROM public.{}').format(sql.Identifier(table))
        ).fetchone()['count']
        tables[table] = {'rows': int(count), 'total_relation_bytes': int(size)}
    return {'plan_sha256': fingerprint(columns), 'columns': columns, 'tables': tables,
            'checked_at': time.time(), 'lock_timeout_ms': 500, 'statement_timeout_ms': 5000,
            'scope': 'Only listed public REAL columns. Existing values, IDs and audit history are preserved; historical quantization is not repaired.'}


def promote(connection, plan, approved_sha):
    if approved_sha != plan['plan_sha256']:
        raise ValueError('Approval must match the exact current column plan SHA256')
    from backend import db, postgres
    class AuditCursor:
        def __init__(self, cursor):
            self.cursor = cursor
        def fetchone(self):
            row = self.cursor.fetchone()
            return postgres.Row(row) if row is not None else None
    class AuditConnection:
        def execute(self, statement, args=()):
            return AuditCursor(connection.execute(postgres.sql(statement), args))
    results = []
    for table, table_info in plan['tables'].items():
        names = [row['column_name'] for row in plan['columns'] if row['table_name'] == table]
        began = time.monotonic()
        try:
            with connection.transaction():
                connection.execute("SET LOCAL lock_timeout='500ms'")
                connection.execute("SET LOCAL statement_timeout='5s'")
                if not connection.execute('SELECT pg_try_advisory_xact_lock(684731923) AS locked').fetchone()['locked']:
                    raise RuntimeError('Application state write is active; table deferred')
                current = connection.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema='public' "
                    "AND table_name=%s AND data_type='real'", (table,)).fetchall()
                present = {row['column_name'] for row in current}
                if present-set(names):
                    raise RuntimeError('A new unreviewed REAL column appeared; inspect again')
                changed = [name for name in names if name in present]
                if changed:
                    clauses = [sql.SQL('ALTER COLUMN {} TYPE DOUBLE PRECISION USING {}::double precision').format(
                        sql.Identifier(name), sql.Identifier(name)) for name in changed]
                    connection.execute(sql.SQL('ALTER TABLE public.{} {}').format(
                        sql.Identifier(table), sql.SQL(', ').join(clauses)))
                remaining = connection.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema='public' "
                    "AND table_name=%s AND column_name=ANY(%s) AND data_type!='double precision'",
                    (table, names)).fetchall()
                if remaining:
                    raise RuntimeError('Column promotion did not reach its reviewed target type')
                if changed:
                    db.audit(AuditConnection(), 'timestamp-precision-migration',
                             'database.timestamp.precision.promoted', table,
                             {'columns': changed, 'plan_sha256': approved_sha,
                              'old_values_preserved': True, 'history_precision_restored': False})
            result = {'table': table, 'columns': changed, 'state': 'promoted' if changed else 'already-promoted',
                      'seconds': round(time.monotonic()-began, 4), 'audited_rows_before': table_info['rows']}
            results.append(result)
        except Exception as error:
            results.append({'table': table, 'state': 'deferred', 'exception': type(error).__name__,
                            'reason': str(error).splitlines()[0][:240],
                            'seconds': round(time.monotonic()-began, 4)})
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--approved-plan-sha256')
    parser.add_argument('--output')
    args = parser.parse_args()
    if not DATABASE_URL:
        raise ValueError('This maintenance operation requires the existing PostgreSQL connection')
    options = '-c lock_timeout=500 -c statement_timeout=5000'
    if not args.apply:
        options += ' -c default_transaction_read_only=on'
    with psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row, options=options) as connection:
        report = inspect(connection)
        report['applied'] = False
        if args.apply:
            report['results'] = promote(connection, report, args.approved_plan_sha256)
            report['applied'] = True
            report['remaining'] = inspect(connection)
    if args.apply:
        from backend import postgres
        postgres.close()
    encoded = json.dumps(report, indent=2)
    if args.output:
        Path(args.output).write_text(encoded)
    print(encoded)


if __name__ == '__main__':
    main()
