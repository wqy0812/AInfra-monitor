import asyncio
import json

import httpx
import pytest

from monitoring import probe_catalog as catalog, probe_worker as worker, probes


def manifest():
    return {'schema_version': 1, 'revision': 3, 'foundations': [{
        'id': 'new-foundation', 'name': '新底座', 'enabled': True,
        'monitoring_source': 'foundation-new-foundation',
        'monitoring_adapter': 'a3-vllm-v1',
        'router': {'id': 'router-one', 'proxy_url': 'http://proxy.test', 'maintenance': False},
    }], 'gateways': [{
        'id': 'entrance-one', 'name': '入口', 'enabled': True, 'business_url': 'http://gateway.test/v1',
        'router_id': 'router-one', 'legacy_probe_environment': 'a3-vllm', 'probe_migrated_at': 900,
    }]}


@pytest.fixture
def managed(tmp_path, monkeypatch):
    monkeypatch.setenv('STATE_DIR', str(tmp_path))
    monkeypatch.setenv('MONITORING_CATALOG_FILE', str(tmp_path/'catalog.json'))
    (tmp_path/'catalog.json').write_text(json.dumps(manifest()))
    catalog.atomic_json(catalog.state_file('control'), {'revision': 3, 'heartbeat_at': 1000, 'paused_routers': {}})
    return tmp_path


def result(at, **values):
    return {'started_at': at-1, 'finished_at': at, 'duration_seconds': 1,
            'first_output_seconds': .1, 'reason': 'success', 'http_status': 200, **values}


def test_managed_targets_keep_two_scopes_and_expire_only_foundation(managed):
    foundation, gateway = catalog.targets(now=1030)
    assert foundation['probe_id'] == 'foundation:new-foundation'
    assert foundation['url'] == 'http://proxy.test/v1/chat/completions'
    assert foundation['offset_seconds'] % 2 == 1
    assert gateway['probe_id'] == 'gateway:entrance-one'
    assert gateway['offset_seconds'] == 0
    assert gateway['url'] == 'http://gateway.test/v1/chat/completions'
    assert all(t['dispatch_status'] == 'active' for t in (foundation, gateway))
    expired = catalog.targets(now=1031)
    assert expired[0]['pause_reason'] == 'permit_expired'
    assert expired[1]['dispatch_status'] == 'active'
    catalog.atomic_json(catalog.state_file('control'), {'heartbeat_at': 1000, 'paused_routers': {'router-one':'op'}})
    assert catalog.targets(now=1001)[0]['pause_reason'] == 'maintenance'


def test_legacy_history_requires_explicit_unique_mapping(managed):
    value = manifest()
    value['gateways'][0].pop('probe_migrated_at')
    with pytest.raises(ValueError, match='migration boundary'):
        catalog.targets(value, now=1000)
    value = manifest()
    value['gateways'].append(value['gateways'][0] | {'id':'second'})
    with pytest.raises(ValueError, match='Duplicate legacy'):
        catalog.targets(value, now=1000)
    (managed/'catalog.json').write_text('{bad')
    with pytest.raises(ValueError, match='unavailable'):
        catalog.targets(now=1000)


def test_unrelated_gateway_is_not_reenabled_and_drafts_do_not_probe(managed):
    value = manifest()
    value['gateways'].append({'id':'retired', 'name':'PPU', 'router_id':'retired-router',
                              'business_url':'http://retired.test', 'enabled':True})
    value['gateways'].append({'id':'draft', 'name':'Draft', 'router_id':None,
                              'business_url':'http://draft.test', 'enabled':True})
    targets = catalog.targets(value, now=1000)
    assert 'gateway:retired' not in {t['probe_id'] for t in targets}
    assert targets[-1]['pause_reason'] == 'awaiting_router'
    value['foundations'][0]['monitoring_adapter'] = None
    targets = catalog.targets(value, now=1000)
    assert targets[0]['pause_reason'] == targets[1]['pause_reason'] == 'awaiting_adapter'


@pytest.mark.asyncio
async def test_retirement_removes_targets_and_history_without_cancelling_inflight(managed):
    scheduler = worker.ManagedProbes(None, 'http://vm.test')
    await scheduler.tick(1000)
    release = asyncio.Event()
    running = asyncio.create_task(release.wait())
    scheduler.running['foundation:new-foundation'] = running
    value = manifest()
    value['revision'] += 1
    value['foundations'][0]['enabled'] = False
    (managed/'catalog.json').write_text(json.dumps(value))
    await scheduler.tick(1001)
    assert scheduler.targets == {} and not running.done()
    assert catalog.read_json(catalog.state_file('status'))['target_ids'] == []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail('No retired history query'))) as client:
        assert (await probes.history(client, 'http://vm.test', now=1001))['services'] == []
    release.set()
    await running


def test_decode_stitches_legacy_only_to_gateway_and_preserves_actual_router(managed):
    targets = catalog.targets(now=1000)
    raw = (probes.samples('a3-vllm', result(800)) + probes.samples('a3-vllm', result(950))
           + probes.samples(targets[0]['environment'], result(990, actual_router_id='router-one'), target=targets[0])
           + probes.samples('a3-vllm', result(995, actual_router_id='router-after-switch'), target=targets[1]))
    foundation, gateway = probes.decode(raw, 1, 1000, targets=targets)['services']
    assert [r['finished_at'] for r in foundation['records']] == [990]
    assert [r['finished_at'] for r in gateway['records']] == [800, 995]
    assert gateway['records'][0]['actual_router_id'] is None
    assert gateway['latest']['actual_router_id'] == 'router-after-switch'
    assert 'url' not in foundation and 'expected_router' not in foundation


@pytest.mark.asyncio
@pytest.mark.parametrize('returned,expected', [('router-one','success'),('wrong','identity_mismatch'),(None,'identity_mismatch')])
async def test_fixed_router_assertion_and_response_identity(returned, expected):
    calls = []
    body = b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
    def respond(request):
        calls.append(request)
        assert request.headers['X-Aigate-Expected-Router'] == 'router-one'
        headers = {'Content-Type':'text/event-stream'}
        if returned: headers['X-Aigate-Backend'] = returned
        return httpx.Response(200, headers=headers, content=body)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        value = await worker.probe(client, 'http://proxy.test/v1/chat/completions', expected_router='router-one')
    assert value['reason'] == expected and len(calls) == 1


@pytest.mark.asyncio
async def test_dispatch_pause_ack_does_not_cancel_inflight_and_resume_skips_missed_slots(managed):
    scheduler = worker.ManagedProbes(None, 'http://vm.test')
    calls, release = [], asyncio.Event()
    async def execute(target):
        calls.append(target['probe_id'])
        await release.wait()
    scheduler.execute = execute
    await scheduler.tick(1000)
    fid = 'foundation:new-foundation'
    due = scheduler.due[fid]
    catalog.atomic_json(catalog.state_file('control'), {'revision':3, 'heartbeat_at':due, 'paused_routers':{}})
    await scheduler.tick(due)
    await asyncio.sleep(0)
    assert fid in calls
    original = scheduler.running[fid]
    catalog.atomic_json(catalog.state_file('control'), {'revision':3, 'heartbeat_at':due+1, 'paused_routers':{'router-one':'pause-1'}})
    await scheduler.tick(due+1)
    acknowledgement = catalog.read_json(catalog.state_file('status'))
    assert acknowledgement['catalog_revision'] == 3
    assert fid in acknowledgement['target_ids']
    assert acknowledgement['pause_operations'] == {'router-one':'pause-1'}
    assert acknowledgement['inflight'][fid] == 1
    assert not original.done()
    await scheduler.tick(due+120)
    assert calls.count(fid) == 1
    catalog.atomic_json(catalog.state_file('control'), {'revision':4, 'heartbeat_at':due+121, 'paused_routers':{}})
    await scheduler.tick(due+121)
    assert scheduler.due[fid] > due+121
    assert scheduler.intervals[fid][-1]['end'] == due+121
    release.set()
    await asyncio.gather(*scheduler.running.values())


@pytest.mark.asyncio
async def test_history_uses_identity_selectors_and_actual_runtime_pause_intervals(managed):
    catalog.atomic_json(catalog.state_file('status'), {'pause_intervals':{'foundation:new-foundation':[
        {'start':990,'end':None,'reason':'maintenance'}]}})
    calls = []
    def respond(request):
        calls.append(request)
        selectors = request.url.params.get_list('match[]')
        assert 'probe_id=~' in selectors[0]
        assert 'probe_id=""' in selectors[1]
        return httpx.Response(200, text='')
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        data = await probes.history(client, 'http://vm.test', now=1000)
    assert len(calls) == 1 and data['services'][0]['pause_intervals'][0]['reason'] == 'maintenance'
