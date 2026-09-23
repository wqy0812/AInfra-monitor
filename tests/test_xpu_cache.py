import copy
import asyncio
import httpx
import pytest
from monitoring import xpu_cache
from monitoring.replay import decode_export
from monitoring.xpu import replay


def test_replay_native_gauge_zero_and_outage():
    labels={'environment':'xpu-pd','job':'sglang-prefill','instance':'xpu','model_name':'glm','tp_rank':'0','pp_rank':'0','engine_type':'unified'}
    groups=decode_export([
        {'metric':{'__name__':'up','job':'sglang-prefill','instance':'xpu'},'timestamps':[100000,105000,110000],'values':[1,1,0]},
        {'metric':{'__name__':'sglang:cache_hit_rate',**labels},'timestamps':[100000,105000,110000],'values':[.9744,0,.9]},
        {'metric':{'__name__':'sglang:cache_hit_rate',**labels,'tp_rank':'1'},'timestamps':[100000,105000,110000],'values':[.1,.1,.1]},
    ])
    original=copy.deepcopy(groups)
    snaps,points=replay(groups,100,125)
    assert groups==original
    assert points[0]['nodes']['prefill']['cache_60s']['ratio']==.9744
    assert points[1]['nodes']['prefill']['cache_60s']['ratio']==0
    assert snaps[0]['nodes']['prefill']['metrics']['data']['cache_hit_ratio']==.9744
    assert all(p['nodes']['prefill']['cache_60s']['ratio'] is None for p in points[2:])


@pytest.mark.parametrize('value',[None,-.1,1.1,float('nan'),float('inf')])
def test_invalid(value):assert xpu_cache.cache(value)['ratio'] is None


def test_ambiguous_rank_not_averaged():
    row={'name':'sglang:cache_hit_rate','labels':{'tp_rank':'0','pp_rank':'0'},'value':.5}
    assert xpu_cache.from_rows([row,row])['ratio'] is None


@pytest.mark.asyncio
async def test_history_restores_previously_blank_values_and_keeps_gaps():
    calls=[]
    async def query(expr,start,end,step):
        calls.append(expr)
        assert (start,end,step)==(100,115,5)
        assert 'environment="xpu-pd"' in expr and 'job="sglang-prefill"' in expr
        if expr.startswith('default_rollup'):
            return [{'metric':{},'values':[[100,'0.9'],[105,'0'],[110,'1.2']]}]
        return [{'metric':{},'values':[[100,'1']]}]
    points=[{'ts':ts,'nodes':{'prefill':{'cache_60s':None,'gap_before':['cache','ttft']}}} for ts in (100,105,110,115)]
    xpu_cache.attach(points,await xpu_cache.history(query,100,115,5))
    assert len(calls)==2
    assert [p['nodes']['prefill']['cache_60s']['ratio'] for p in points]==[.9,0,None,None]
    assert points[0]['nodes']['prefill']['gap_before']==['ttft']
    assert 'cache' in points[1]['nodes']['prefill']['gap_before']


@pytest.mark.asyncio
@pytest.mark.parametrize('failed_query', ['value', 'continuity'])
@pytest.mark.parametrize('failure', ['timeout', 'http', 'invalid'])
async def test_optional_cache_failure_preserves_other_history(tmp_path, monkeypatch, failed_query, failure):
    from monitoring import api
    monkeypatch.setattr(api, 'STATE', tmp_path)
    service = api.Service('xpu-pd')
    async def query(expr, *args):
        if 'sglang:cache_hit_rate' in expr:
            kind = 'value' if expr.startswith('default_rollup') else 'continuity'
            if kind == failed_query:
                if failure == 'timeout': raise httpx.ReadTimeout('fixture timeout')
                if failure == 'http': raise httpx.HTTPStatusError('fixture 503', request=httpx.Request('GET', 'http://vm.test'), response=httpx.Response(503))
                return [{'values': [[100, 'invalid']]}]
            return [{'values': [[100, '.75' if kind == 'value' else '1']]}]
        if 'monitoring_chart_' in expr:
            return [{'metric': {'path': 'nodes.decode.decode_tokens'}, 'values': [[100, '42' if 'chart_value' in expr else '1']]}]
        return []
    service.query = query
    try:
        result = await service.history(1, 100, 105)
        point = result['points'][0]
        assert point['nodes']['decode']['decode_tokens'] == 42
        assert all(status == 'ok' for status in result['gateway_status'].values())
        prefill = point['nodes']['prefill']
        assert prefill['cache_60s']['ratio'] == (None if failed_query == 'value' else .75)
        assert 'cache' in prefill['gap_before']
    finally:
        await service.client.aclose()


@pytest.mark.asyncio
async def test_cache_query_deadline_cancels_pending_queries():
    finished = []
    async def query(expr, *args):
        try:
            await asyncio.Event().wait()
        finally:
            finished.append(expr)
    async with asyncio.timeout(1):
        assert await xpu_cache.history(query, 100, 105, 5, timeout=.01) == ({}, {})
    assert len(finished) == 2


@pytest.mark.asyncio
async def test_cache_preserves_request_cancellation():
    async def query(*args):
        raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await xpu_cache.history(query, 100, 105, 5)
