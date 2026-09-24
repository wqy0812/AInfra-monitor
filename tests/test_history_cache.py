"""Concurrency and lifecycle guarantees for the bounded history cache."""
import asyncio

import pytest
import pytest_asyncio

from monitoring import api


@pytest_asyncio.fixture
async def service():
    instance = api.Service('dcu-pd')
    yield instance
    await instance.close()


@pytest.mark.asyncio
async def test_same_key_runs_one_complete_query_group(service):
    calls = []
    release = asyncio.Event()

    async def query(expr, *args):
        calls.append(expr)
        await release.wait()
        return []

    service.query = query
    requests = [asyncio.create_task(service.history(1, 100, 200)) for _ in range(20)]
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(service.history_tasks) == 1
    assert sum('monitoring_chart_' in expr for expr in calls) == 3
    count = len(calls)
    release.set()
    results = await asyncio.gather(*requests)
    assert all(result is results[0] for result in results)
    assert await service.history(1, 100, 200) is results[0]
    assert len(calls) == count
    assert not service.history_tasks


@pytest.mark.asyncio
async def test_lru_expiry_and_view_isolation(service, monkeypatch):
    calls = []
    clock = [100.0]
    # Replace the module reference, not the event loop's global time.monotonic.
    class Clock:
        monotonic = staticmethod(lambda: clock[0])
    monkeypatch.setattr(api, 'time', Clock)

    async def query(*args):
        calls.append(args)
        return {'args': args}
    service._history = query
    first = await service.history(1, 100, 200)
    summary = await service.history(1, 100, 200, view='summary')
    assert summary is not first
    for hour in range(2, 8):
        await service.history(hour, 100, 200)
    assert len(service.cache) == 8
    assert await service.history(1, 100, 200) is first
    await service.history(8, 100, 200)
    assert len(service.cache) == 8
    assert await service.history(1, 100, 200) is first
    assert await service.history(1, 100, 200, view='summary') is not summary
    assert len(calls) == 10
    clock[0] += 5
    assert await service.history(1, 100, 200) is not first
    assert len(service.cache) == 1


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_other_waiters(service):
    entered, release = asyncio.Event(), asyncio.Event()
    async def query(*args):
        entered.set()
        await release.wait()
        return {'ok': True}
    service._history = query
    first = asyncio.create_task(service.history(1, 100, 200))
    await entered.wait()
    second = asyncio.create_task(service.history(1, 100, 200))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert len(service.history_tasks) == 1
    release.set()
    assert await second == {'ok': True}


@pytest.mark.asyncio
async def test_failure_and_shared_deadline_allow_retry(service, monkeypatch):
    async def fail(*args):
        raise ValueError('upstream failed')
    service._history = fail
    with pytest.raises(ValueError):
        await service.history(1, 100, 200)
    assert not service.cache and not service.history_tasks
    stopped = asyncio.Event()
    async def hang(*args):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    service._history = hang
    monkeypatch.setattr(api, 'HISTORY_TIMEOUT', 0.02)
    with pytest.raises(TimeoutError):
        await service.history(1, 100, 200)
    assert stopped.is_set()
    assert not service.cache and not service.history_tasks
    async def succeed(*args):
        return {'ok': True}
    service._history = succeed
    assert await service.history(1, 100, 200) == {'ok': True}


@pytest.mark.asyncio
async def test_close_cancels_and_joins_shared_tasks(service):
    entered, stopped = asyncio.Event(), asyncio.Event()
    async def query(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    service._history = query
    caller = asyncio.create_task(service.history(1, 100, 200))
    await entered.wait()
    await service.close()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert stopped.is_set() and service.client.is_closed
    assert not service.history_tasks and not service.cache


@pytest.mark.asyncio
async def test_failure_cancels_sibling_vm_queries(service):
    started, stopped = asyncio.Event(), asyncio.Event()
    async def query(expr, *args):
        if 'chart_value' in expr:
            await started.wait()
            raise ValueError('broken VM response')
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    service.query = query
    with pytest.raises(ValueError):
        await service.history(1, 100, 200)
    assert stopped.is_set()
    assert not service.history_tasks


@pytest.mark.asyncio
async def test_environment_caches_and_inflight_tasks_are_isolated(service):
    other = api.Service('a3-vllm')
    async def query(*args):
        return []
    service.query = other.query = query
    try:
        first, second = await asyncio.gather(service.history(1, 100, 200), other.history(1, 100, 200))
        assert first['environment'] == 'dcu-pd'
        assert second['environment'] == 'a3-vllm'
        assert first is not second
        assert service.cache is not other.cache
    finally:
        await other.close()
