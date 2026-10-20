"""Tenant-scoped discovery metadata; executable bindings are resolved separately."""
import json

from . import config, db


def list_assets(uid, query='', limit=None, cloud=None):
    if len(query) > 200:
        raise ValueError('Search is too long')
    if limit is not None and (type(limit) is not int or not 1 <= limit <= 1000):
        raise ValueError('Invalid asset listing limit')
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
        SELECT a.*,canonical.metadata AS canonical_metadata,CASE WHEN m.asset_id IS NOT NULL THEN canonical.path ELSE a.path END AS listed_path
        FROM assets a JOIN bindings b ON b.asset_id=a.id
        LEFT JOIN migration_assets m ON m.asset_id=a.id AND m.user_id=a.user_id
        LEFT JOIN assets canonical ON canonical.id=m.canonical_asset_id AND canonical.user_id=a.user_id
        WHERE a.user_id=? AND (
          (m.asset_id IS NULL AND a.path LIKE 's3://%')
          OR (canonical.path LIKE 's3://%' AND m.dataset_id IN (SELECT id FROM ready))
        )"""
        args = [uid, uid, uid]
    else:
        sql = 'SELECT a.*,NULL AS canonical_metadata,a.path AS listed_path FROM assets a WHERE a.user_id=?'
        args = [uid]
    if query:
        sql += ' AND (LOWER(a.name) LIKE ? OR LOWER(a.family) LIKE ? OR LOWER(a.format) LIKE ?)'
        args.extend(['%' + query.lower() + '%'] * 3)
    sql += ' ORDER BY a.family,a.name,a.id'
    if limit is not None:
        sql += ' LIMIT ?'
        args.append(limit)
    rows = db.query(sql, tuple(args))
    from . import asset_rights
    grants = asset_rights.grant_map(uid)
    sources = asset_rights.known_sources(uid)
    for row in rows:
        row['path'] = row.pop('listed_path')
        row['metadata'] = json.loads(row['metadata'])
        canonical = json.loads(row.pop('canonical_metadata') or '{}')
        for key in ['sha256','version_id','dataset_id']:
            if canonical.get(key):
                row['metadata'][key] = canonical[key]
        row['data_rights'] = asset_rights.disclosure(uid,row,grants,sources)
    return rows
