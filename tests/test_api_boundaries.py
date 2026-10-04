import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from monitoring import api, exporter, request_profile


def request_for(service):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(services={'dcu-pd': service}, service=service)))


@pytest.mark.asyncio
@pytest.mark.parametrize('error,stamp', [(None, 100), ('offline', 100), (None, 80), (None, None)])
async def test_latest_rejects_failed_missing_or_stale_data(monkeypatch, error, stamp):
    monkeypatch.setattr(api.time, 'time', lambda: 100)
    service = SimpleNamespace(error=error, latest={'ts': stamp, 'environment': 'dcu-pd'})
    if error or stamp is None or stamp == 80:
        with pytest.raises(HTTPException) as failure:
            await api.latest(request_for(service))
        assert failure.value.status_code == 503
    else:
        assert await api.latest(request_for(service)) == service.latest


@pytest.mark.asyncio
@pytest.mark.parametrize('arguments', [
    {'hours': 0}, {'hours': 721}, {'hours': float('nan')}, {'start': 10}, {'end': 20},
    {'start': 20, 'end': 10}, {'start': 10, 'end': float('inf')},
    {'start': 10, 'end': 106}, {'view': 'invalid'}, {'environment': 'invalid'},
])
async def test_history_rejects_invalid_ranges_before_query(monkeypatch, arguments):
    monkeypatch.setattr(api.time, 'time', lambda: 100)
    service = SimpleNamespace(history=AsyncMock())
    with pytest.raises(HTTPException) as failure:
        await api.history(request_for(service), **arguments)
    assert failure.value.status_code == 400
    service.history.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [None, TimeoutError('timeout'), httpx.ConnectError('offline'), ValueError('bad grid')])
async def test_history_translates_upstream_errors_and_preserves_arguments(monkeypatch, failure):
    monkeypatch.setattr(api.time, 'time', lambda: 100)
    service = SimpleNamespace(history=AsyncMock(return_value={'points': []}, side_effect=failure))
    if failure:
        with pytest.raises(HTTPException) as error:
            await api.history(request_for(service), 1, 10, 90, view='summary')
        assert error.value.status_code == 503
    else:
        assert await api.history(request_for(service), 1, 10, 90, view='summary') == {'points': []}
    service.history.assert_awaited_once_with(1, 10, 90, view='summary')


@pytest.mark.asyncio
@pytest.mark.parametrize('catalog', [None, 'invalid', 'valid'])
async def test_lifespan_initializes_all_sources_and_closes_on_exit(tmp_path, monkeypatch, catalog):
    services, accelerators = [], []
    class Service:
        def __init__(self, environment):
            self.environment, self.error, self.watermark = environment, None, 100
            self.close = AsyncMock()
            services.append(self)
        async def run(self):
            await asyncio.Event().wait()
    class Accelerator:
        def __init__(self, vm, state, data, source_ready):
            self.ready, self.close = source_ready, AsyncMock()
            accelerators.append(self)
        async def run(self):
            await asyncio.Event().wait()
    monkeypatch.setattr(api, 'Service', Service)
    monkeypatch.setattr(api, 'AccelerationService', Accelerator)
    monkeypatch.setattr(api.time, 'time', lambda: 100)
    if catalog:
        path = tmp_path / 'catalog.json'
        path.write_text('bad json' if catalog == 'invalid' else '{}')
        monkeypatch.setenv('PERSES_ACCELERATION_CATALOG', str(path))
    else:
        monkeypatch.delenv('PERSES_ACCELERATION_CATALOG', raising=False)
    app = FastAPI()
    async with api.lifespan(app):
        assert set(app.state.services) == set(api.ENVIRONMENTS)
        assert app.state.service is app.state.services['dcu-pd']
        if catalog == 'valid':
            assert accelerators[0].ready()
            services[0].error = 'offline'
            assert not accelerators[0].ready()
        elif catalog == 'invalid':
            assert app.state.perses_acceleration is None and 'JSONDecodeError' in app.state.perses_acceleration_error
    for service in services:
        service.close.assert_awaited_once()
    if accelerators:
        accelerators[0].close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [TimeoutError(), httpx.ConnectError('offline'), ValueError('bad'), KeyError('missing')])
async def test_profile_query_failure_is_503_and_releases_admission(monkeypatch, error):
    now = api.time.time()
    service = SimpleNamespace(slots=asyncio.Semaphore(1))
    monkeypatch.setattr(request_profile, 'metrics', AsyncMock(side_effect=error))
    with pytest.raises(HTTPException) as failure:
        await request_profile.profile_metrics(request_for(service), now-60, now)
    assert failure.value.status_code == 503
    assert not service.slots.locked()


@pytest.mark.parametrize('sample_error', [False, True])
def test_exporter_sampler_records_failures_and_keeps_period(tmp_path, monkeypatch, sample_error):
    monkeypatch.setattr(exporter, 'STATE', {})
    sleep = Mock(side_effect=KeyboardInterrupt())
    monkeypatch.setattr(exporter, 'time', SimpleNamespace(time=lambda: 100, monotonic=lambda: 10, sleep=sleep))
    run = Mock(return_value=SimpleNamespace(stdout='header\ndevice,HCU use (%)\ncard0,10\n'),
               side_effect=OSError('rocm-smi failed') if sample_error else None)
    monkeypatch.setattr(exporter.subprocess, 'run', run)
    with pytest.raises(KeyboardInterrupt):
        exporter.sample()
    assert exporter.STATE['ok'] is not sample_error and exporter.STATE['ts'] == 100
    assert bool(exporter.STATE['rows']) is not sample_error
    sleep.assert_called_once_with(5)
