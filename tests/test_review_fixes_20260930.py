"""Observed access/health faults and recovery from unavailable optional data."""
import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from monitoring import api, gateway_live, host_cpu, xpu_cache
from monitoring.access import client_allowlist
from monitoring.query_client import QueryClient


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('value', [None, '', ' ', '*', '127.0.0.1,*', '127.0.0.1,', 'localhost', '0.0.0.0/0'])
def test_allowlist_rejects_missing_or_unrestricted_configuration(value):
    with pytest.raises(ValueError):
        client_allowlist(value)


def test_allowlist_preserves_verified_peers_and_normalizes_addresses():
    assert client_allowlist(' 127.0.0.1, ::1,122.247.53.162,122.247.53.250,127.0.0.1 ') == [
        '127.0.0.1', '::1', '122.247.53.162', '122.247.53.250']


@pytest.mark.asyncio
@pytest.mark.parametrize('peer,expected', [('127.0.0.1', 404), ('122.247.53.250', 404), ('203.0.113.123', 403)])
async def test_real_middleware_checks_transport_peer_even_with_forwarded_header(monkeypatch, peer, expected):
    monkeypatch.setattr(api, 'ALLOWED', set(client_allowlist('127.0.0.1,122.247.53.250')))
    # The unknown endpoint is intercepted before app state or workers are needed.
    transport = httpx.ASGITransport(app=api.app, client=(peer, 12345))
    async with httpx.AsyncClient(transport=transport, base_url='http://monitoring') as client:
        response = await client.get('/unknown-review-endpoint', headers={'X-Forwarded-For': '127.0.0.1'})
    assert response.status_code == expected


@pytest.mark.parametrize('entry', ['start.py', 'start_test4.py'])
def test_startup_requires_allowlist_before_directory_or_container_changes(tmp_path, monkeypatch, entry):
    import runpy
    import sys
    spec = importlib.util.spec_from_file_location('review_start', ROOT/'deploy/start.py')
    start = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(start)
    monkeypatch.delenv('ALLOWED_CLIENTS', raising=False)
    monkeypatch.setattr(start, 'ROOT', tmp_path/'central')
    monkeypatch.setattr(start, 'RELEASE', tmp_path/'release')
    monkeypatch.setattr(start, 'run', lambda *args: pytest.fail('No build before allowlist validation'))
    monkeypatch.setattr(start, 'start', lambda *args: pytest.fail('No container before allowlist validation'))
    with pytest.raises(ValueError, match='explicit client IP'):
        if entry == 'start.py':
            monkeypatch.setattr(sys, 'argv', ['start.py', 'central'])
            start.main()
        else:
            monkeypatch.setitem(sys.modules, 'start', start)
            runpy.run_path(str(ROOT/'deploy/start_test4.py'))
    assert not list(tmp_path.iterdir())


def healthy_state(now):
    services = {}
    for env in api.ENVIRONMENTS:
        nodes = {role: {'metrics': {'status': 'ok'},
                        'telemetry': {'status': 'ok', 'data': {'gpus': [{}]*8}},
                        'host_cpu': {'status': 'ok'}} for role in ('prefill', 'decode')}
        services[env] = SimpleNamespace(latest={'nodes': nodes, 'mooncake': {'status': 'ok'}},
            watermark=now-5, error=None, started=now-100, host_cpu_status='ok', retention_gap=None)
    return SimpleNamespace(service=services['dcu-pd'], services=services)


@pytest.mark.asyncio
@pytest.mark.parametrize('environment', api.ENVIRONMENTS)
@pytest.mark.parametrize('fault', ['source-error', 'missing-role', 'stale', 'worker-error'])
async def test_health_detects_each_environment_failure(monkeypatch, environment, fault):
    monkeypatch.setattr(api, 'time', SimpleNamespace(time=lambda: 200))
    state = healthy_state(200)
    service = state.services[environment]
    if fault == 'source-error': service.latest['nodes']['prefill']['metrics']['status'] = 'error'
    elif fault == 'missing-role': service.latest['nodes'].pop('prefill')
    elif fault == 'stale': service.watermark = 180
    else: service.error = 'VM query failed'
    result = await api.health(SimpleNamespace(app=SimpleNamespace(state=state)))
    assert result['status'] == 'degraded'
    assert result['environments'][environment]['status'] == 'degraded'


@pytest.mark.asyncio
async def test_health_does_not_require_unintegrated_xpu_telemetry(monkeypatch):
    monkeypatch.setattr(api, 'time', SimpleNamespace(time=lambda: 200))
    state = healthy_state(200)
    for node in state.services['xpu-pd'].latest['nodes'].values():
        node['telemetry'] = {'status': 'not_integrated'}
    result = await api.health(SimpleNamespace(app=SimpleNamespace(state=state)))
    assert result['status'] == 'ok'
    assert all(env['status'] == 'ok' for env in result['environments'].values())


@pytest.mark.asyncio
async def test_optional_gateway_and_cache_deadlines_overlap_and_preserve_backend(tmp_path, monkeypatch):
    monkeypatch.setattr(api, 'STATE', tmp_path)
    monkeypatch.setattr(api, 'HISTORY_TIMEOUT', .3)
    service = api.Service('xpu-pd')
    entered = set()
    originals = gateway_live.history, xpu_cache.history
    async def gateway(*args): return await originals[0](*args, timeout=.05)
    async def cache(*args): return await originals[1](*args, timeout=.05)
    monkeypatch.setattr(gateway_live, 'history', gateway)
    monkeypatch.setattr(xpu_cache, 'history', cache)
    async def query(expression, start, end, step):
        if 'monitoring_chart_' in expression:
            return [{'metric': {'path': 'nodes.decode.decode_tokens'},
                     'values': [[start, '42' if 'chart_value' in expression else '1']]}]
        entered.add('cache' if 'sglang:cache_hit_rate' in expression else 'gateway')
        # Serial scheduling would reach this deadline with just one group entered.
        await asyncio.Event().wait()
    service.query = query
    request = asyncio.create_task(service.history(1, 100, 105))
    try:
        for _ in range(30):
            await asyncio.sleep(0)
            if entered == {'gateway', 'cache'}: break
        assert entered == {'gateway', 'cache'}
        result = await request
        assert result['points'][0]['nodes']['decode']['decode_tokens'] == 42
        assert set(result['gateway_status'].values()) == {'unavailable'}
        assert result['points'][0]['nodes']['prefill']['cache_60s']['ratio'] is None
        assert 'cache' in result['points'][0]['nodes']['prefill']['gap_before']
    finally:
        await service.close()
        await asyncio.gather(request, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('environment', api.ENVIRONMENTS)
@pytest.mark.parametrize('failure', [None, 'export', 'import'])
async def test_old_watermark_recovers_with_bounded_queries_and_durable_gap(tmp_path, monkeypatch, environment, failure):
    end = 4_000_000
    monkeypatch.setattr(api, 'STATE', tmp_path)
    monkeypatch.setattr(api, 'time', SimpleNamespace(time=lambda: end+3))
    service = api.Service(environment)
    original = end-31*86400
    service.watermark = original
    service.watermark_file.write_text(json.dumps({'ts': original}))
    calls = []
    async def raw(start, until):
        calls.append((start, until))
        if failure == 'export': raise httpx.ConnectError('VM offline')
        return {}
    def transport(request):
        if request.method == 'POST' and failure == 'import': return httpx.Response(503)
        return httpx.Response(200, json={'data': {'result': []}})
    async def collect(client, vm, start, until):
        assert start == max(service.watermark,end-api.RETENTION_SECONDS-5)+5
        assert until-start == 295
        return {}, 'ok'
    service.raw = raw
    service.client = QueryClient(transport=httpx.MockTransport(transport))
    monkeypatch.setattr(host_cpu, 'collect', collect)
    try:
        if failure:
            with pytest.raises(httpx.HTTPError): await service.cycle()
            assert service.watermark == original
            assert json.loads(service.watermark_file.read_text()) == {'ts': original}
            assert service.retention_gap is None
        else:
            await service.cycle()
            floor = end-api.RETENTION_SECONDS
            assert service.watermark == floor+295
            saved = json.loads(service.watermark_file.read_text())
            assert saved['retention_gap'] == {'start': original+5, 'end': floor-5,
                'detected_at': end+3, 'reason': 'outside_raw_retention'}
            resumed = api.Service(environment)
            assert resumed.watermark == service.watermark and resumed.retention_gap == saved['retention_gap']
            await resumed.close()
            await service.cycle()
            assert service.watermark == floor+595
            assert service.retention_gap == saved['retention_gap']
        assert all(start <= until and until-start <= 380 for start, until in calls)
    finally:
        await service.close()
