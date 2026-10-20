"""Validate the untrusted worker's interchange and shared cache accounting."""
import io
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from backend import db, remote_viewer, workbench
from backend.portable import execute
from backend.viewer_protocol import validate_result
from test_extensions import bind, vcf


def valid_pileup():
    return {'chrom': 'MT', 'start': 10, 'end': 12, 'reference': 'AC',
            'coverage': [1, 1], 'forward': [1, 1], 'reverse': [0, 0],
            'reads': [{'name': 'read', 'start': 10, 'end': 12, 'mapq': 60,
                       'cigar': '2M', 'reverse': False, 'duplicate': False,
                       'bases': [[10, 'A', 30], [11, 'C', 30]]}],
            'truncated': False, 'coverage_scope': 'Rendered reads', 'transport': 'isolated'}


@pytest.mark.parametrize('change', [
    {'coverage': [-1, 1]}, {'start': 10.5, 'end': 12.5}, {'reference': 'A'},
    {'truncated': 'false'},
])
def test_typed_pileup_rejects_invalid_scientific_coordinates_and_flags(change):
    with pytest.raises(ValueError):
        validate_result('pileup', {**valid_pileup(), **change})


def test_typed_omics_and_header_reject_nonfinite_numbers_and_unknown_metadata():
    omics = {'method': 'actual', 'limit': 20000, 'columns': ['x'], 'numeric': ['x'],
             'matrix_columns': ['x'], 'row_labels': ['r'], 'matrix': [[1]],
             'points': [{'x': float('inf'), 'y': 2, 'label': 'r', 'row': {'x': '1'}}]}
    with pytest.raises(ValueError):
        validate_result('omics', omics)
    with pytest.raises(ValueError):
        validate_result('header', {'format': 'VCF', 'metadata': {'path': '/untrusted/override'}})
    with pytest.raises(ValueError):
        validate_result('header', {'format': 'VCF', 'metadata': {'contig_count': -1, 'header_inspected': True}})
    assert validate_result('pileup', valid_pileup())['coverage'] == [1, 1]


def fixture_result(uid, tmp_path):
    item = vcf(tmp_path/'private.vcf', [(10, 'A', 'G', .001, 'GENE', 'Pathogenic')])
    output = execute({'tool': 'results.prepare', 'inputs': {'data': [item]},
                      'params': {'operation': 'variant-index'}, 'workdir': str(tmp_path/'prepared')})['report'][0]
    aid, prepared = bind(uid, item), bind(uid, output)
    return aid, workbench.owned(uid, prepared), Path(output['path']).read_bytes()


def test_live_index_reservation_blocks_competing_writer_and_is_released(client, owner, tmp_path, monkeypatch):
    uid = owner['id']
    aid, artifact, body = fixture_result(uid, tmp_path)
    workbench.index_path(uid, aid)
    folder = remote_viewer.DATA/'workbench'
    used = sum(p.stat().st_size for p in folder.rglob('*') if p.is_file())
    monkeypatch.setattr(remote_viewer, 'TOTAL_INDEX_LIMIT', used+remote_viewer.INDEX_LIMIT+1024)
    entered, finish = threading.Event(), threading.Event()

    def paused_stream(user, output, maximum):
        entered.set()
        assert finish.wait(5), 'Test reader was not released'
        return io.BytesIO(body)
    monkeypatch.setattr(remote_viewer, 'stream', paused_stream)
    first = {'id': 'cache-first-'+uid, 'asset_id': aid, 'identity': 'first', 'run_id': 'run-test'}
    second = {**first, 'id': 'cache-second-'+uid, 'identity': 'second'}
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(remote_viewer.ingest_index, uid, first, artifact)
        try:
            assert entered.wait(3)
            assert db.query('SELECT bytes FROM viewer_cache_holds WHERE id=?', (first['id'],), one=True)['bytes'] == remote_viewer.INDEX_LIMIT
            with pytest.raises(ValueError, match='storage is full'):
                remote_viewer.ingest_index(uid, second, artifact)
        finally:
            finish.set()
        assert task.result(timeout=5)['rows'] == 1
    assert not db.query('SELECT id FROM viewer_cache_holds WHERE user_id=?', (uid,))
    assert workbench.query(uid, aid, {})['total'] == 1


def test_failed_index_keeps_committed_results_and_releases_reservation(client, owner, tmp_path, monkeypatch):
    uid = owner['id']
    aid, artifact, body = fixture_result(uid, tmp_path)
    monkeypatch.setattr(remote_viewer, 'stream', lambda *args: io.BytesIO(body))
    request = {'id': 'good-'+uid, 'asset_id': aid, 'identity': 'good', 'run_id': 'run-test'}
    remote_viewer.ingest_index(uid, request, artifact)
    monkeypatch.setattr(remote_viewer, 'stream', lambda *args: io.BytesIO(b'{"bad":"schema"}\n'))
    with pytest.raises(ValueError):
        remote_viewer.ingest_index(uid, {**request, 'id': 'failed-'+uid, 'identity': 'changed'}, artifact)
    assert workbench.query(uid, aid, {})['total'] == 1
    assert not db.query('SELECT id FROM viewer_cache_holds WHERE user_id=?', (uid,))
    assert not list(workbench.index_path(uid, aid).parent.glob('*.building'))


def test_real_s3_streaming_body_survives_http_eof_close(client, owner, tmp_path, monkeypatch):
    from botocore.response import StreamingBody
    from urllib3.response import HTTPResponse
    uid = owner['id']
    aid, artifact, body = fixture_result(uid, tmp_path)
    raw = HTTPResponse(body=io.BytesIO(body), preload_content=False,
                       headers={'Content-Length': str(len(body))})
    wrapped = StreamingBody(raw, len(body))
    monkeypatch.setattr(remote_viewer, 'stream', lambda *args: wrapped)
    request = {'id': 's3-http-'+uid, 'asset_id': aid, 'identity': 'exact', 'run_id': 'run-test'}
    assert remote_viewer.ingest_index(uid, request, artifact)['rows'] == 1
    assert workbench.query(uid, aid, {})['total'] == 1
    assert raw.closed


def test_chunked_interchange_rejects_oversized_lines_and_total_bytes(monkeypatch):
    monkeypatch.setattr(remote_viewer, 'MAX_LINE', 8)
    assert list(remote_viewer.bounded_lines(io.BytesIO(b'one\ntwo\nlast'))) == [b'one\n', b'two\n', b'last']
    with pytest.raises(ValueError, match='record exceeds'):
        list(remote_viewer.bounded_lines(io.BytesIO(b'a'*9)))
    with pytest.raises(ValueError, match='byte allowance'):
        list(remote_viewer.bounded_lines(io.BytesIO(b'one\ntwo\n'), maximum=7))
