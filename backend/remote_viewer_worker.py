"""Genomic parsing entrypoint, executed only inside a funded isolated worker."""
import json
import os
import time
from pathlib import Path
from .viewer_protocol import VERSION, MAX_LINE, validate_row

def execute(job):
    from .portable import artifact
    import pysam
    work = Path(job['workdir'])
    params = job.get('params', {})
    operation = params.get('operation', 'variant-index')
    item = job['inputs']['data'][0]
    output = work / ('viewer-alleles.jsonl' if operation == 'variant-index' else 'viewer-result.json')
    maximum = int(job.get('max_output_bytes', 64 * 1024**2))
    if operation == 'variant-index':
        from .variant_rows import rows, annotation_schema
        with pysam.VariantFile(item['path']) as source, output.open('wb') as dest:
            metadata = {'schema_version': VERSION, 'header': str(source.header),
                        'annotation_schema': annotation_schema(source),
                        'assembly': item.get('metadata', {}).get('assembly')}
            def write(value):
                line = (json.dumps(value, allow_nan=False, separators=(',', ':')) + '\n').encode()
                if len(line) > MAX_LINE or dest.tell() + len(line) > maximum:
                    raise ValueError('Viewer index exceeds its accepted record/output allowance; no partial result is published')
                dest.write(line)
            write(metadata)
            count = 0
            for row in rows(source):
                write(validate_row(row))
                count += 1
                if count > 100_000_000:
                    raise ValueError('100M allele safety ceiling exceeded')
        return {'report': [artifact(output, 'REPORT', viewer_operation=operation,
                                   viewer_schema=VERSION, allele_rows=count)]}
    if operation not in {'header', 'omics', 'pileup'}:
        raise ValueError('Unsupported viewer operation')
    # Reuse the same scientific readers in an ephemeral LOCAL database. Never
    # point this process at the application database or give it app credentials.
    from .config import CLOUD, DATABASE_URL, STATE
    if CLOUD or DATABASE_URL or not STATE.is_relative_to(work):
        raise ValueError('Viewer parsing requires an isolated local worker state')
    from . import data, db, workbench
    paths = []
    for values in job['inputs'].values():
        for row in values:
            paths.append(row['path'])
            meta = row.get('metadata', {})
            paths.extend(meta[field] for field in ['index', 'mate', 'dictionary'] if meta.get(field))
            paths.extend(meta.get('index_paths', []))
    data.bind_worker_inputs(paths, work)
    uid = 'isolated_viewer'
    with db.tx() as c:
        c.execute("INSERT INTO users(id,email,password,role,tier,created) VALUES(?,?,?,'user','pro',?) ON CONFLICT(id) DO NOTHING",
                  (uid, 'isolated@invalid.local', 'no-login', time.time()))
        for port, values in job['inputs'].items():
            row = values[0]
            c.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?,?)',
                      (port, uid, row['path'], row['name'], row['format'], row['bytes'], 0, 'viewer',
                       row.get('metadata', {}).get('assembly'), db.encode(row.get('metadata', {}))))
    if operation == 'header':
        result = workbench.inspect_header(uid, 'data')
        # File paths are worker internals, not usable cloud bindings.
        result['metadata'] = {k: v for k, v in result['metadata'].items()
                              if k in {'samples', 'info_fields', 'sort_order', 'contig_style',
                                       'contig_count', 'contig_preview', 'header_inspected', 'assembly_verification'}}
    elif operation == 'pileup':
        result = workbench.pileup(uid, 'data', 'reference', params['chrom'], int(params['start']), int(params['end']))
        result['transport'] = 'Isolated worker; exact versioned input staging and bounded region rendering'
    else:
        result = workbench.omics(uid, 'data', params.get('x') or None, params.get('y') or None)
    encoded = json.dumps({'schema_version': VERSION, 'operation': operation, 'result': result}, allow_nan=False).encode()
    if len(encoded) > min(maximum, 16 * 1024**2):
        raise ValueError('Viewer response exceeds its accepted output allowance')
    output.write_bytes(encoded)
    return {'report': [artifact(output, 'REPORT', viewer_operation=operation, viewer_schema=VERSION)]}
