"""Tenant-scoped discovery metadata; executable bindings are resolved separately."""
import json
import re

from . import config, db
from .asset_presentation import describe as describe_asset


def public_metadata(value):
    """Remove historical host paths from discovery responses, not stored records."""
    if isinstance(value, dict):
        return {key: public_metadata(item) for key, item in value.items()
                if key not in {'root', 'local_path', 'source_path', 'original_path', 'ssd_root'}
                and not (isinstance(item, str) and (item.startswith('/') or re.match(r'^[A-Za-z]:[\\/]', item)))}
    if isinstance(value, list):
        return [public_metadata(item) for item in value
                if not (isinstance(item, str) and (item.startswith('/') or re.match(r'^[A-Za-z]:[\\/]', item)))]
    return value


def list_assets(uid, query='', limit=None, cloud=None, primary_only=False, filters=None, offset=0, page=False, scope='all', node_id=''):
    if len(query) > 200:
        raise ValueError('Search is too long')
    if limit is not None and (type(limit) is not int or not 1 <= limit <= 1000):
        raise ValueError('Invalid asset listing limit')
    filters = filters or {}
    if set(filters)-{'family','assembly','format','release'} or any(not isinstance(v,str) or len(v)>200 for v in filters.values()):
        raise ValueError('Invalid asset filters')
    if type(offset) is not int or not 0 <= offset <= 1000000:
        raise ValueError('Invalid asset offset')
    if scope not in {'all','references','databases'}:
        raise ValueError('Invalid asset scope')
    from .registry import NODES
    database_families = {n.get('asset_family') for n in NODES if n.get('group') == 'Databases'} - {None}
    node = next((n for n in NODES if n['id'] == node_id), None) if node_id else None
    if node_id and node is None:
        raise ValueError('Unknown asset binding node')
    cloud = config.CLOUD if cloud is None else cloud
    # Build the owner's verified ID set once. UNION deduplicates source IDs even
    # when several dataset receipts refer to one source; there is no per-alias
    # canonical/companion lookup on the catalogue or search hot path.
    if cloud:
        sql = """WITH ready AS (
          SELECT id,source_asset FROM datasets WHERE user_id=? AND state='Ready'
          AND version_id IS NOT NULL AND version_id NOT IN ('','null')
        ), bindings AS (
          SELECT id AS asset_id FROM ready
          UNION SELECT source_asset FROM ready WHERE source_asset IS NOT NULL
          UNION SELECT m.asset_id FROM migration_assets m
                JOIN ready d ON d.id=m.dataset_id WHERE m.user_id=?
        )
        SELECT a.*,m.source_path AS discovery_source_path,canonical.metadata AS canonical_metadata,CASE WHEN m.asset_id IS NOT NULL THEN canonical.path ELSE a.path END AS listed_path
        FROM assets a JOIN bindings b ON b.asset_id=a.id
        LEFT JOIN migration_assets m ON m.asset_id=a.id AND m.user_id=a.user_id
        LEFT JOIN assets canonical ON canonical.id=m.canonical_asset_id AND canonical.user_id=a.user_id
        WHERE a.user_id=? AND (
          (m.asset_id IS NULL AND a.path LIKE 's3://%')
          OR (canonical.path LIKE 's3://%' AND m.dataset_id IN (SELECT id FROM ready))
        )"""
        args = [uid, uid, uid]
    else:
        sql = 'SELECT a.*,a.path AS discovery_source_path,NULL AS canonical_metadata,a.path AS listed_path FROM assets a WHERE a.user_id=?'
        args = [uid]
    if query:
        sql += " AND (LOWER(a.name) LIKE ? OR LOWER(a.family) LIKE ? OR LOWER(a.format) LIKE ? OR LOWER(COALESCE(a.assembly,'')) LIKE ? OR LOWER(a.metadata) LIKE ?)"
        args.extend(['%' + query.lower() + '%'] * 5)
    if primary_only:
        # Filter before LIMIT so companion-heavy inventories do not crowd out
        # primary datasets. Exact companion attachment is handled separately.
        sql += " AND a.format<>'INDEX'"
        for suffix in ['.bai','.crai','.tbi','.csi','.fai','.gzi','.dict','.amb','.ann','.bwt','.bwt.2bit.64','.pac','.sa','.bt2','.bt2l','.ht2','.ht2l']:
            sql += ' AND LOWER(a.name) NOT LIKE ?'
            args.append('%'+suffix)
    sql += ' ORDER BY a.family,a.name,a.id'
    # Classify all matching rows before pagination. Cache fragments can exceed
    # 10,000 files; LIMIT before classification would hide valid primary inputs.
    rows = db.query(sql, tuple(args))
    prepared, seen = [], set()
    for row in rows:
        row['metadata'] = json.loads(row['metadata'])
        canonical = json.loads(row.pop('canonical_metadata') or '{}')
        for key in ['sha256','version_id','dataset_id','bundle_verification']:
            if canonical.get(key):
                row['metadata'][key] = canonical[key]
        row['presentation'] = describe_asset(row, row.pop('discovery_source_path') or '')
        row['path'] = row.pop('listed_path')
        p = row['presentation']
        if primary_only and not p['primary_eligible']:
            continue
        if node:
            family = node.get('asset_family')
            assembly = next((parameter.get('default') for parameter in node.get('parameters',[]) if parameter['key']=='assembly'), None)
            if not p['primary_eligible'] or (family and p['family'] != family):
                continue
            if family and assembly and p['assembly'] and p['assembly'] != assembly:
                continue
            if node['group']=='References' and p['format']!='FASTA':
                continue
            if node['id']=='database.asset' and p['format'] not in {'DATABASE','INDEXED_VCF','VCF','BGZIP_VCF','TSV','CSV','ARCHIVE'}:
                continue
            if family and node['outputs'][0]['type']=='VCF' and p['format'] not in {'VCF','BGZIP_VCF','INDEXED_VCF'}:
                continue
        if scope == 'references' and p['format'] != 'FASTA':
            continue
        if scope == 'databases' and p['family'] not in database_families and p['format'] not in {'DATABASE','ARCHIVE'}:
            continue
        # Collapse aliases of the exact same named scientific component. Distinct
        # contigs must remain distinct even when empty gzip bytes are identical.
        identity = (row['metadata'].get('sha256') or row['id'], row['metadata'].get('version_id'),
                    p['basename'], p['family'], p['assembly'], p['release'], p.get('contig'), p.get('interval'))
        if identity not in seen:
            seen.add(identity)
            prepared.append(row)
    facets = {key: sorted({str(r['presentation'].get(key) or '') for r in prepared}-{''}) for key in ['family','assembly','format','release']}
    matching = [row for row in prepared if all(not value or str(row['presentation'].get(key) or '') == value for key,value in filters.items())]
    total = len(matching)
    rows = matching[offset:offset+limit if limit is not None else None]
    from . import asset_rights
    grants = asset_rights.grant_map(uid)
    sources = asset_rights.known_sources(uid)
    for row in rows:
        row['data_rights'] = asset_rights.disclosure(uid,row,grants,sources)
        row['metadata'] = public_metadata(row['metadata'])
    if page:
        return {'assets':rows,'total':total,'offset':offset,'limit':limit,'has_more':offset+len(rows)<total,'facets':facets}
    return rows
