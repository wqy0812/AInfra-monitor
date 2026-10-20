import copy
import json
import os
import time

import httpx
import pytest

from monitoring import cache_sources as sources
from monitoring import cache_observations as observations


def binding(url='http://backend:8000/v1/chat/completions', environment='dcu-pd', **extra):
    return {'backend_url': url, 'environment': environment, 'proxy_url': 'http://a3-proxy:18084',
            'valid_from': 0, 'evidence': 'fixture runtime plus scrape inventory', **extra}


def request(url='http://backend:8000/v1/chat/completions', **extra):
    return {'backend_url': url, 'proxy_url': 'http://a3-proxy:18084', 'at': 100, **extra}


def test_actual_backend_overrides_gateway_location_and_model_text():
    catalog = [binding()]
    result = sources.resolve(request(), catalog)
    assert result['source']['environment'] == 'dcu-pd'
    assert sources.resolve(request('http://other:8000'), catalog)['status'] == 'unavailable'
    assert sources.resolve(request(), catalog*2)['reason'] == 'ambiguous_cache_source'
    assert sources.resolve(request(at=200), [binding(valid_until=150)])['status'] == 'unavailable'
    assert sources.canonical_url('HTTP://Example.COM:80/path/v1/chat/completions') == 'http://example.com/path'


def test_local_endpoints_require_exact_proxy_context():
    catalog = [binding('http://127.0.0.1:8077')]
    assert sources.resolve(request('http://127.0.0.1:8077'), catalog)['status'] == 'resolved'
    assert sources.resolve(request('http://127.0.0.1:8077', proxy_url='http://different:18084'), catalog)['status'] == 'unavailable'
    catalog[0].pop('proxy_url')
    assert sources.resolve(request('http://127.0.0.1:8077'), catalog)['status'] == 'unavailable'


def test_frozen_catalog_versions_and_validity_are_verified(tmp_path, monkeypatch):
    path = tmp_path/'sources.json'
    path.write_text(json.dumps({'version': 1, 'bindings': [binding()]}))
    monkeypatch.setenv('CACHE_SOURCE_CONFIG', str(path))
    result = sources.resolve(request())
    assert sources.validate_snapshot(result, 100, 130)['environment'] == 'dcu-pd'
    path.write_text(json.dumps({'version':1,'bindings':[binding(valid_until=200)]}))
    assert sources.validate_snapshot(result,100,130)['environment']=='dcu-pd'
    with pytest.raises(ValueError,match='outside_validity'):sources.validate_snapshot(result,190,220)
    changed = copy.deepcopy(result);changed['source']['environment'] = 'a3-vllm'
    with pytest.raises(ValueError):sources.validate_snapshot(changed, 100, 130)
    path.write_text(json.dumps({'version': 1, 'bindings': []}))
    with pytest.raises(ValueError, match='version_unavailable'):sources.validate_snapshot(result, 100, 130)


@pytest.mark.asyncio
async def test_internal_api_boundaries_and_unmapped_backends(tmp_path, monkeypatch):
    from monitoring.api import app
    path=tmp_path/'bindings.json'
    path.write_text(json.dumps({'version':1,'bindings':[binding()]}))
    monkeypatch.setenv('CACHE_SOURCE_CONFIG',str(path))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,client=('127.0.0.1',123)),base_url='http://monitor') as client:
        response=await client.post('/api/monitoring/cache-sources/resolve',json=request())
        assert response.status_code==200 and response.json()['source']['environment']=='dcu-pd'
        unknown=await client.post('/api/monitoring/cache-sources/resolve',json=request('http://unknown:9000'))
        assert unknown.json()['reason']=='cache_source_not_registered'
        assert (await client.post('/api/monitoring/cache-sources/resolve',json=request(environment='a3-vllm'))).status_code==422
        assert (await client.post('/api/monitoring/cache-observations',json={'snapshot':response.json(),'start':100,'end':130})).status_code==400
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,client=('203.0.113.9',123)),base_url='http://monitor') as client:
        assert (await client.post('/api/monitoring/cache-sources/resolve',json=request())).status_code==403


def stat(base=0, amount=6, right=130):
    return {'first_over_time': base+amount/6, 'last_over_time': base+amount,
            'min_over_time': base+amount/6, 'increase': amount*5/6, 'count_over_time': 6,
            'tfirst_over_time': right-25, 'tlast_over_time': right}


def test_disjoint_deltas_do_not_bridge_resets_or_missing_samples():
    baseline = {'last_over_time': 100, 'tlast_over_time': 100}
    assert observations.delta(stat(100), baseline, 130) == 6
    assert observations.delta({**stat(100), 'count_over_time': 4}, baseline, 130) is None
    assert observations.delta({**stat(100), 'increase': 999}, baseline, 130) is None
    assert observations.delta(stat(1), baseline, 130) is None
    assert observations.delta(stat(100), {}, 130) is None
    assert observations.delta(stat(100), {**baseline, 'tlast_over_time': 80}, 130) is None
    assert observations.delta(stat(0, 0), {'last_over_time': 0, 'tlast_over_time': 100}, 130) == 0


def test_representative_dp_groups_sum_tokens_but_not_physical_instance_coverage():
    current, baseline = {}, {}
    for dp in ('0', '1'):
        for mode in ('device_hit', 'host_hit', 'storage_hit', 'input'):
            key = tuple(sorted({'__name__': 'sglang:prefill_effective_tokens_total', 'instance': 'p:1', 'dp_rank': dp, 'mode': mode}.items()))
            current[key] = stat(100)
            baseline[key] = {'last_over_time': 100, 'tlast_over_time': 100}
    health = {(('instance', 'p:1'),): {**stat(), 'min_over_time': 1}}
    totals, available, partial = observations.bucket_counts('dcu-pd', current, baseline, health, 130)
    assert totals['input_tokens'] == 48 and totals['hit_tokens'] == 36
    assert available == 1 and not partial
    first = next(iter(current));current[first]['increase'] = 999
    totals, available, partial = observations.bucket_counts('dcu-pd', current, baseline, health, 130)
    assert totals['input_tokens'] == 24 and available == 1 and partial


@pytest.mark.asyncio
@pytest.mark.parametrize('environment', ['dcu-pd', 'a3-vllm', 'xpu-pd'])
async def test_real_vm_cache_report_bucket_semantics(environment, monkeypatch):
    url = os.environ.get('MONITORING_TEST_VM_URL')
    if not url:pytest.skip('dedicated real VM required')
    from monitoring import api
    # >=50 points activates VM's cache timestamp alignment. Preserve the
    # report's fractional timeline even for older, cacheable query ranges.
    end = int(time.time()//30)*30-3600+.31762
    start = end-1800
    monkeypatch.setattr(api, 'VM', url)
    lines = []
    job = 'vllm-a3' if environment == 'a3-vllm' else 'sglang-prefill'
    node = {'dcu-pd': 'dcu1', 'a3-vllm': 'a3-1', 'xpu-pd': 'xpu-2'}[environment]
    for ts in range(int(start)-15, int(end)+1, 5):
        index = ts-start+1000
        tags = f'environment="{environment}",job="{job}",instance="{environment}:1",node="{node}"'
        lines.append(f'up{{{tags}}} 1 {ts*1000}')
        if environment == 'xpu-pd':
            lines.append(f'sglang:cache_hit_rate{{{tags},tp_rank="0",pp_rank="0"}} 0.75 {ts*1000}')
        elif environment == 'dcu-pd':
            for mode, n in [('device_hit', 50), ('host_hit', 20), ('storage_hit', 10), ('input', 20)]:
                lines.append(f'sglang:prefill_effective_tokens_total{{{tags},mode="{mode}"}} {index*n} {ts*1000}')
        else:
            for source, n in [('local_cache_hit', 50), ('external_kv_transfer', 30), ('local_compute', 20)]:
                lines.append(f'vllm:prompt_tokens_by_source_total{{{tags},source="{source}"}} {index*n} {ts*1000}')
            for name, n in [('prompt_tokens_total', 100), ('prompt_tokens_cached_total', 80)]:
                lines.append(f'vllm:{name}{{{tags}}} {index*n} {ts*1000}')
        labels = f'environment="{environment}",node="{node}",role="prefill"'
        lines.extend([f'monitoring_native_backend_expected_instances{{{labels}}} 1 {ts*1000}',
                      f'monitoring_native_backend_available_instances{{{labels}}} 1 {ts*1000}'])
        if environment != 'xpu-pd':lines.append(f'monitoring_native_prefill_cache_hit_ratio{{{labels}}} 0.8 {ts*1000}')
    async with httpx.AsyncClient() as client:
        response = await client.post(url+'/api/v1/import/prometheus', content='\n'.join(lines)+'\n')
        response.raise_for_status()
        await client.get(url+'/internal/force_flush')
    service = api.Service(environment)
    try:
        async with service.client.scope():
            result = await observations.observe(service, {'source': sources.source(environment)}, start, end, 0)
        assert len(result['points']) == 60
        assert result['status'] == 'complete', result
        for point in result['points']:
            assert point['valid_seconds'] == 30
            if environment == 'xpu-pd':
                assert point['ratio_seconds'] == pytest.approx(22.5)
                assert point['trend']['ratio'] == pytest.approx(.75)
            else:
                assert point['counts']['hit_tokens']/point['counts']['input_tokens'] == pytest.approx(.8)
                assert point['counts']['input_tokens'] == pytest.approx(3000)
                assert point['trend']['ratio'] == pytest.approx(.8)
    finally:await service.close()
