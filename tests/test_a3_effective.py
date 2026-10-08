import copy
import json
import os
import time

import httpx
import pytest

from monitoring import a3, a3_effective as effective
from monitoring.api import Service, encode
from tests.test_a3 import fixture


def groups(weights=(1, 1, 1, 1), idle=False, since=100, end=170):
    data = fixture(end=end)
    for (node, instance), (_, samples) in data.items():
        if node != 'a3-1':
            continue
        weight = weights[int(instance.rsplit(':', 1)[1]) - 7100]
        for tick, rows in samples.items():
            base = next(row['labels'] for row in rows if row['name'].startswith('vllm:'))
            count = 1 if idle else tick - 100 + 1
            amounts = dict(zip(effective.SOURCES, (50 * weight, 900 * weight, 50 * weight)))
            for source, value in amounts.items():
                rows.append({'name': 'vllm:prompt_tokens_by_source_total', 'labels': {**base, 'source': source}, 'value': value * count})
            for name, value in [('vllm:prompt_tokens_total', sum(amounts.values())), ('vllm:prompt_tokens_cached_total', amounts['local_cache_hit'] + amounts['external_kv_transfer'])]:
                rows.append({'name': name, 'labels': dict(base), 'value': value * count})
    return data


def point(data=None, since=100, end=170):
    return a3.replay(data or groups(), 100, end, effective_since=since)[1][-1]['nodes']['prefill']['cache_60s']


def test_partition_and_legacy_independence():
    value = point()
    assert value['ratio'] == value['external_ratio'] == .6
    new = value['effective']
    assert (new['ratio'], new['device'], new['storage']) == (.95, .9, .05)
    assert new['input_tokens'] == 240000
    assert new['hit_tokens'] == new['device_hit_tokens'] + new['storage_hit_tokens']
    assert new['input_tokens'] == new['hit_tokens'] + new['computed_tokens']
    assert new['semantics'] == effective.SCHEMA
    assert 'effective' not in a3.replay(groups(), 100, 170, effective_since=100)[1][-1]['nodes']['decode']['cache_60s']


def test_weighted_instances_and_valid_zero():
    data = groups(weights=(1, 2, 3, 10))
    for rows in data[('a3-1', '122.209.21.24:7103')][1].values():
        for row in rows:
            if row['name'] == 'vllm:prompt_tokens_by_source_total':
                if row['labels']['source'] == 'local_compute': row['value'] *= 20
                else: row['value'] = 0
            elif row['name'] == 'vllm:prompt_tokens_cached_total': row['value'] = 0
    new = point(data)['effective']
    assert new['ratio'] == pytest.approx(.95 * 6 / 16)
    all_compute = groups(weights=(0, 0, 0, 1))
    all_compute[('a3-1', '122.209.21.24:7103')] = data[('a3-1', '122.209.21.24:7103')]
    new = point(all_compute)['effective']
    assert new['ratio'] == new['device'] == new['storage'] == 0
    assert new['reason'] is None


def test_idle_and_activation_window():
    new = point(groups(idle=True))['effective']
    assert new['ratio'] is None and new['input_tokens'] == 0
    assert point(since=200)['effective']['ratio'] is None
    assert point(since=150)['effective']['ratio'] is None
    assert point(groups(end=220), since=150, end=220)['effective']['ratio'] == .95
    assert point(since=None)['effective']['ratio'] is None


@pytest.mark.parametrize('failure', ['missing', 'unknown', 'duplicate', 'reset', 'identity', 'model', 'partition', 'cached_total', 'nan', 'gap', 'down'])
def test_bad_native_windows_do_not_change_legacy_queries(failure):
    data = groups()
    key = ('a3-1', '122.209.21.24:7100')
    times, samples = data[key]
    rows = samples[140]
    target = next(row for row in rows if row['name'] == 'vllm:prompt_tokens_by_source_total' and row['labels']['source'] == 'local_cache_hit')
    if failure == 'missing': rows.remove(target)
    elif failure == 'unknown': target['labels']['source'] = 'other'
    elif failure == 'duplicate': rows.append(copy.deepcopy(target))
    elif failure == 'reset': target['value'] = 1
    elif failure == 'identity': target['labels']['engine'] = '3'
    elif failure == 'model':
        for native_rows in samples.values():
            for row in native_rows:
                if row['name'] in effective.NAMES: row['labels']['model_name'] = 'other'
    elif failure == 'partition': target['value'] += 1
    elif failure == 'cached_total': next(row for row in rows if row['name'] == 'vllm:prompt_tokens_cached_total')['value'] += 1
    elif failure == 'nan': target['value'] = float('nan')
    elif failure == 'gap': times.remove(140); del samples[140]
    elif failure == 'down': next(row for row in rows if row['name'] == 'up')['value'] = 0
    value = point(data)
    assert value['effective']['ratio'] is None
    if failure not in ('gap', 'down'):
        assert value['ratio'] == value['external_ratio'] == .6


def test_activation_is_durable_and_does_not_touch_watermarks(tmp_path):
    watermark = tmp_path / 'watermark.json'
    watermark.write_text('{"ts": 10}')
    assert effective.activate(tmp_path, 102) == 105
    assert effective.activate(tmp_path, 900) == 105
    assert watermark.read_text() == '{"ts": 10}'
    (tmp_path / effective.STATE_FILE).write_text('{}')
    with pytest.raises(ValueError): effective.load_since(tmp_path)


def test_materialization_starts_at_cutover_only():
    points = a3.replay(groups(), 100, 170, effective_since=150)[1]
    lines = encode(points, 'a3-vllm').splitlines()
    new = [line for line in lines if 'cache_60s.effective.' in line]
    assert new and all(int(line.rsplit(' ', 1)[1]) >= 150000 for line in new)
    assert all('schema="' + effective.SCHEMA + '"' in line for line in new)
    assert any('cache_60s.external_ratio' in line and 'schema="v1"' in line for line in lines)


@pytest.mark.asyncio
@pytest.mark.parametrize('view', ['full', 'summary'])
async def test_history_boundary_and_latest_gap(view):
    service = Service('a3-vllm')
    service.cache_effective_since = 150
    service.latest_point = {'ts': 170, 'nodes': {'prefill': {}, 'decode': {}}, 'mooncake': {}}
    async def query(expression, start, end, step):
        if 'monitoring_chart_' not in expression: return []
        paths = {effective.PREFIX + 'ratio': .95, effective.PREFIX + 'device': .9, effective.PREFIX + 'storage': .05}
        return [{'metric': {'path': path}, 'values': [[tick, str(value if 'chart_value' in expression else 1)]]} for path, value in paths.items() for tick in (140, 160)]
    service.query = query
    try:
        result = await service._history(1, 100, 170, 5, view)
        assert result['cache_effective_since'] == 150
        assert result['points'][0]['nodes']['prefill']['cache_60s']['effective']['ratio'] is None
        assert result['points'][1]['nodes']['prefill']['cache_60s']['effective']['ratio'] == .95
        assert 'effective_cache' in result['points'][-1]['nodes']['prefill']['gap_before']
    finally: await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('step', [15, 60])
@pytest.mark.parametrize('case', ['valid', 'invalid', 'missing'])
async def test_effective_history_real_vm(step, case, monkeypatch):
    url = os.environ.get('MONITORING_TEST_VM_URL')
    if not url: pytest.skip('Requires disposable local VictoriaMetrics')
    assert url.startswith('http://127.0.0.1:')
    monkeypatch.setattr('monitoring.api.VM', url)
    slot = ['valid', 'invalid', 'missing'].index(case) * 2 + (step == 60)
    end = int(time.time() // 60) * 60 - 600 - slot * 300
    lines = []
    for tick in range(end - 120, end + 1, 5):
        for field, amount in [('ratio', .95), ('device', .9), ('storage', .05), ('input_tokens', 1000)]:
            if case == 'missing' and tick == end - 5: continue
            tags = '{environment="a3-vllm",schema="' + effective.SCHEMA + '",path="' + effective.PREFIX + field + '"}'
            for kind, value in [('value', amount), ('valid', int(not (case == 'invalid' and tick == end - 5)))]:
                lines.append(f'monitoring_chart_{kind}{tags} {value} {tick * 1000}')
        # A foreign/legacy schema must never overwrite the new partition.
        lines.append(f'monitoring_chart_value{{environment="a3-vllm",schema="v1",path="{effective.PREFIX}ratio"}} 42 {tick * 1000}')
    async with httpx.AsyncClient(base_url=url, trust_env=False) as client:
        (await client.post('/api/v1/import/prometheus', content='\n'.join(lines) + '\n')).raise_for_status()
        (await client.get('/internal/force_flush')).raise_for_status()
    service = Service('a3-vllm'); service.cache_effective_since = end - 120
    try:
        result = await service._history(1, end - step, end, step, 'summary')
        pre = result['points'][-1]['nodes']['prefill']
        assert pre['cache_60s']['effective']['ratio'] == .95
        assert 'effective_cache' in pre['gap_before'] if case != 'valid' else 'effective_cache' not in pre['gap_before']
    finally: await service.close()
