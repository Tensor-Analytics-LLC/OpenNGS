import math

import pytest

from scripts.verify_private_viewer_cloud import graph_for, reserve


def test_validation_cost_cap_survives_resume_and_unknown_submission_outcomes():
    checkpoint = {'reservations': {}}
    reserve(checkpoint, 'first', .4)
    reserve(checkpoint, 'second', .4)
    reserve(checkpoint, 'first', .4)
    assert sum(checkpoint['reservations'].values()) == .8
    with pytest.raises(RuntimeError, match='ceiling'):
        reserve(checkpoint, 'third', .21)
    with pytest.raises(RuntimeError, match='changed'):
        reserve(checkpoint, 'first', .5)
    assert sum(checkpoint['reservations'].values()) == .8


def test_validation_cost_cap_rejects_nonfinite_or_negative_reservations():
    for value in [math.nan, math.inf, -.1, True, '0.1']:
        with pytest.raises(RuntimeError):
            reserve({'reservations': {}}, 'bad', value)


def test_validation_estimate_uses_the_runtime_resource_ceiling_and_exact_sources():
    allowance = {'max_storage_bytes': 128*1024**2, 'max_scratch_mb': 20480, 'max_node_seconds': 120}
    graph = graph_for('exact-bam', 'pileup', allowance, {'chrom': 'MT', 'start': 10, 'end': 20}, 'exact-ref')
    assert [n['params']['asset_id'] for n in graph['nodes'][:-1]] == ['exact-bam', 'exact-ref']
    assert graph['nodes'][-1]['resources']['timeout_seconds'] == 120
    assert graph['nodes'][-1]['resources']['scratch_mb'] == 20480
    assert graph['maximum_output_bytes'] == 64*1024**2
