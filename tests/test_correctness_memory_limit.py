import io
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'deploy/perses_acceleration'))
import materialized_release as release


def fixture(monkeypatch, mismatch=False):
    ticks = [1000.125 + i*5 for i in range(13)]
    calls = []
    monkeypatch.setattr(release, 'CORRECTNESS_BATCH_POINTS', 4)
    def query(project, source, expr, start, end, step):
        assert step == 5
        calls.append((source, expr, start, end, step))
        points = [t for t in ticks if start <= t <= end]
        if expr == 'vector(time())':
            return [{'metric': {}, 'values': [[t, str(t)] for t in points]}]
        assert expr == 'frozen_expression'
        if len(points) > 4:
            raise urllib.error.HTTPError('vm', 422, 'memory', {}, io.BytesIO(b'not enough memory for processing histogram'))
        return [{'metric': {'rank': rank}, 'values': [[t, str(i + int(rank) * 100 + (1 if mismatch and source == release.NAME else 0))]
                for i,t in enumerate(ticks) if t in points and i not in (3, 4, 8)]} for rank in ('0', '1')]
    monkeypatch.setattr(release, 'proxy_query', query)
    return ticks, calls


def test_memory_limited_comparison_keeps_all_original_fractional_ticks_and_gaps(monkeypatch):
    ticks, calls = fixture(monkeypatch)
    counts, evidence = release.compare_correctness_window({'id':'p','project':'p'},'frozen_expression',ticks[0],ticks[-1],5)
    assert evidence['mode'] == 'pointwise_after_memory_limit' and evidence['grid_points'] == 13
    assert counts == {t:2 for i,t in enumerate(ticks) if i not in (3,4,8)}
    assert evidence['original_request_error']['status'] == 422
    assert all(c[4] == 5 for c in calls)
    assert evidence['chunks'] == 4


def test_pointwise_result_mismatch_still_blocks_admission(monkeypatch):
    ticks, calls = fixture(monkeypatch,mismatch=True)
    with pytest.raises(AssertionError,match='Pointwise mismatch'):
        release.compare_correctness_window({'id':'p','project':'p'},'frozen_expression',ticks[0],ticks[-1],5)


def test_other_422_failures_are_not_hidden_by_chunking(monkeypatch):
    def query(*args):
        raise urllib.error.HTTPError('vm',422,'invalid query',{},io.BytesIO(b'unknown function'))
    monkeypatch.setattr(release,'proxy_query',query)
    with pytest.raises(RuntimeError,match='unknown function'):
        release.compare_correctness_window({'id':'p','project':'p'},'invalid',0,3600,5)
