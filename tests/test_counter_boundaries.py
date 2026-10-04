from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from monitoring import profile_counters as core


@pytest.mark.parametrize('changes,baseline,born,expected', [
    ({'last_over_time': None}, None, True, (None, 'missing_sample')),
    ({'increase_prometheus': None, 'count_over_time': 1, 'first_over_time': 4, 'min_over_time': 4}, None, True, (4, None)),
    ({'last_over_time': -1}, None, True, (None, 'unexplained_counter_reset')),
    ({'min_over_time': -1}, None, True, (None, 'unexplained_counter_reset')),
    ({'increase_prometheus': 9}, None, True, (None, 'unexplained_counter_reset')),
    ({'tlast_over_time': 80}, None, True, (None, 'unobserved_lifecycle_tail')),
    ({}, None, False, (None, 'missing_boundary_baseline')),
    ({}, {'last_over_time': 3}, False, (None, 'unexplained_counter_reset')),
    ({}, {'last_over_time': 1}, False, (3, None)),
])
def test_counter_delta_never_fills_missing_or_reset_values(changes, baseline, born, expected):
    stats = dict(last_over_time=4, min_over_time=2, first_over_time=2, increase_prometheus=2, tlast_over_time=100)
    stats.update(changes)
    assert core.counter_delta(stats, baseline, born, 100) == expected


@pytest.mark.parametrize('value', [None, {}, 'bad', 'NaN', 'Inf'])
def test_counter_numbers_reject_nonfinite_and_non_numeric(value):
    assert core.number(value) is None


@pytest.mark.asyncio
async def test_counter_queries_reject_failed_summaries_and_resource_limits(monkeypatch):
    monkeypatch.setattr(core, 'MAX_SERIES', 1)
    replies = iter([{'status': 'error'}, {'status': 'success', 'data': {'result': [{}, {}]}}])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=next(replies)))) as client:
        window = core.CounterWindow(SimpleNamespace(client=client), 'http://fixture', 100, 200)
        for message in ('query failed', 'series limit'):
            with pytest.raises(ValueError, match=message):
                await window.query('metric', 100)
    with pytest.raises(ValueError, match='environment'):
        core.CounterWindow(None, '', 0, 1, environment='unknown')


@pytest.mark.asyncio
async def test_counter_summaries_preserve_rollups_and_skip_empty_ranges():
    window = core.CounterWindow(None, '', 100, 200)
    tags = {'__name__': 'aigate_requests_started_total', 'request_scope': 'all'}
    window.query = AsyncMock(side_effect=[
        [{'metric': dict(tags, rollup='last_over_time'), 'value': [200, '5']}],
        [{'metric': dict(tags), 'value': [200, '3']}],
    ])
    assert await window.summaries({}, ['requests_started_total'], 100, 100) == {}
    window.query.assert_not_called()
    result = await window.summaries({}, ['requests_started_total'], 100, 200)
    assert result[core.key(tags)] == {'last_over_time': 5, 'increase_prometheus': 3}


@pytest.mark.asyncio
async def test_lifetime_discovery_rejects_filters_future_epochs_and_excessive_history(monkeypatch):
    window = core.CounterWindow(None, '', 100, 200, backend='selected', model='m')
    common = {'environment': 'dcu-pd', 'request_scope': 'all'}
    name = 'aigate_profile_group_start_time_seconds'
    rows = [dict(common, __name__=name, profile_epoch='0', backend='selected', model='m'),
            dict(common, __name__=name, profile_epoch='201', backend='selected', model='m'),
            dict(common, __name__=name, profile_epoch='120', backend='other', model='m'),
            dict(common, __name__=name, profile_epoch='120', backend='selected', model='other'),
            dict(common, __name__=name, profile_epoch='120'),
            dict(common, __name__='aigate_profile_counter_start_time_seconds', profile_epoch='90'),
            dict(common, __name__='aigate_profile_counter_start_time_seconds', profile_epoch='110')]
    window.query = AsyncMock(return_value=[{'metric': labels} for labels in rows])
    await window.discover()
    assert len(window.lifetimes) == 2 and [row[2] for row in window.lifetimes] == [90, 110]
    window.lifetimes.clear()
    monkeypatch.setattr(core, 'MAX_LIFETIMES', 1)
    with pytest.raises(ValueError, match='lifetime limit'):
        await window.discover()
