"""Source changes and failed scrapes must end counter windows."""
import pytest

from monitoring.api import Service
from monitoring.replay import decode_export, replay
from monitoring.xpu import replay as xpu_replay


def observations(failure=None, replacement=None):
    exported = []
    for tick in range(100, 241, 5):
        instance = 'new:8501' if replacement and tick >= replacement else 'old:8501'
        def add(name, value, **labels):
            exported.append({'metric': {'__name__': name, 'job': 'sglang-prefill',
                'instance': instance, 'environment': 'dcu-pd', **labels},
                'timestamps': [tick * 1000], 'values': [value]})
        add('up', int(tick != failure))
        if tick == failure:
            continue
        # The new process has larger counters: a decrease check alone cannot
        # protect against subtracting the old process's baseline.
        counter = tick + (10000 if instance.startswith('new') else 0)
        for name in ('num_requests_total', 'prompt_tokens_total', 'generation_tokens_total'):
            add('sglang:' + name, counter, model_name='m')
        labels = dict(model_name='m', engine_type='prefill', dp_rank='0', tp_rank='0')
        add('sglang:num_running_reqs', 1, **labels)
        for mode in ('device_hit', 'host_hit', 'storage_hit', 'input'):
            add('sglang:prefill_effective_tokens_total', counter, mode=mode, **labels)
    return decode_export(exported)


@pytest.mark.parametrize('adapter', [replay, xpu_replay])
@pytest.mark.parametrize('fault', ['failure', 'replacement'])
def test_rate_baseline_does_not_cross_failed_scrape_or_source_change(adapter, fault):
    groups = observations(**{fault: 165})
    _, points = adapter(groups, 100, 240)
    points = {p['ts']: p['nodes']['prefill'] for p in points}
    assert points[160]['requests'] == 1
    first = 170 if fault == 'failure' else 165
    assert points[first]['requests'] is None
    assert points[first]['output_tokens'] is None
    assert points[first + 5]['requests'] == 1


@pytest.mark.parametrize('fault', ['failure', 'replacement'])
def test_cache_requires_new_continuous_window_after_fault(fault):
    _, points = replay(observations(**{fault: 165}), 100, 240)
    points = {p['ts']: p['nodes']['prefill'] for p in points}
    assert points[160]['cache_60s']['ratio'] == .75
    first = 170 if fault == 'failure' else 165
    for tick in range(first, first + 55, 5):
        assert points[tick]['cache_60s']['ratio'] is None
    assert points[first + 60]['cache_60s']['ratio'] == .75


@pytest.mark.asyncio
async def test_history_cache_preserves_exact_query_bounds_and_response_metadata(tmp_path, monkeypatch):
    import monitoring.api as api
    monkeypatch.setattr(api, 'STATE', tmp_path)
    service = Service()
    calls = []
    async def query(expr, start, end, step):
        calls.append((start, end))
        if 'aigate' in expr:
            return []
        return [{'metric': {'path': 'nodes.prefill.requests'}, 'values': [[start, '1'], [end, '1']]}]
    service.query = query
    try:
        first = await service.history(1, 100, 3700)
        assert await service.history(1, 100, 3700) == first
        assert len(calls) == 5
        second = await service.history(1, 105, 3705)
        assert [p['ts'] for p in second['points']] == [105, 3705]
        assert len(calls) == 10
        assert (await service.history(2, 105, 3705))['hours'] == 2
    finally:
        await service.client.aclose()
