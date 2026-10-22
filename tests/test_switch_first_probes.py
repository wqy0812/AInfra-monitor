"""Request-count and attribution tests through the production scheduler/SSE path."""
import asyncio
import copy
import json

import httpx
import pytest
import pytest_asyncio

from monitoring import probe_catalog as catalog, probe_worker as worker, probes

SSE = ('data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
       'data: [DONE]\n\n')


class Harness:
    def __init__(self, path):
        self.path, self.now = path, 599
        self.calls, self.raw = [], []
        self.replies, self.gates = {}, {}
        self.import_failure = False
        self.value = {'schema_version': 1, 'revision': 1, 'foundations': [{
            'id': 'one', 'name': 'One', 'enabled': True, 'monitoring_adapter': 'a3-vllm-v1',
            'router': {'id': 'r1', 'proxy_url': 'http://proxy-one', 'maintenance': False}}],
            'gateways': [{'id': 'one', 'name': 'Switch', 'business_url': 'http://switch-one',
                          'router_id': 'r1', 'enabled': True}]}
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.respond))

    async def respond(self, request):
        host = request.url.host
        if host == 'vm':
            if self.import_failure:
                return httpx.Response(503)
            self.raw.append(request.content.decode())
            return httpx.Response(204)
        self.calls.append(host)
        if host in self.gates:
            await self.gates[host].wait()
        identity, status, text = self.replies.get(host, ('r1', 200, SSE))
        if host.startswith('proxy'):
            assert request.headers['X-Aigate-Expected-Router'] == ('r2' if host.endswith('two') else 'r1')
        headers = {'content-type': 'text/event-stream'}
        if identity:
            headers['X-Aigate-Backend'] = identity
        return httpx.Response(status, headers=headers, text=text)

    def save(self):
        (self.path / 'catalog.json').write_text(json.dumps(self.value))
        catalog.atomic_json(catalog.state_file('control'),
                            {'heartbeat_at': self.now, 'paused_routers': {}})

    async def tick(self, now, drain=True):
        self.now = now
        self.save()
        await self.scheduler.tick(now)
        if drain:
            await asyncio.gather(*self.scheduler.running.values())
        else:
            await asyncio.sleep(0)

    def records(self, now=900):
        return probes.decode(''.join(self.raw), 1, now,
            targets=catalog.targets(value=self.value, control={'heartbeat_at': now}, now=now))['services']


@pytest_asyncio.fixture
async def rig(tmp_path, monkeypatch):
    monkeypatch.setenv('STATE_DIR', str(tmp_path))
    monkeypatch.setenv('MONITORING_CATALOG_FILE', str(tmp_path / 'catalog.json'))
    h = Harness(tmp_path)
    h.save()
    monkeypatch.setattr(worker.time, 'time', lambda: h.now)
    catalog.atomic_json(tmp_path / 'probe-offsets.json',
        {'foundation:one': 11, 'foundation:two': 13, 'gateway:one': 0, 'gateway:two': 20})
    h.scheduler = worker.ManagedProbes(h.client, 'http://vm')
    yield h
    for task in h.scheduler.running.values():
        task.cancel()
    await asyncio.gather(*h.scheduler.running.values(), return_exceptions=True)
    await h.client.aclose()


@pytest.mark.asyncio
async def test_normal_chain_uses_one_request_per_minute_and_preserves_source(rig):
    await rig.tick(599)
    for minute in (600, 660, 720):
        await rig.tick(minute)
        await rig.tick(minute+11)
    assert rig.calls == ['switch-one']*3
    foundation, gateway = rig.records()
    assert len(foundation['records']) == len(gateway['records']) == 3
    assert foundation['latest']['observation_source'] == 'switch_shared'
    assert foundation['latest']['source_probe_id'] == 'gateway:one'
    assert foundation['latest']['scheduled_at'] == 720
    assert foundation['latest']['actual_router_id'] == 'r1'
    assert foundation['latest']['started_at'] == gateway['latest']['started_at']
    assert foundation['dispatch_status'] == 'active'


@pytest.mark.asyncio
@pytest.mark.parametrize('identity,status,text', [(None,200,SSE),('unknown',200,SSE),('r1',503,''),
    ('r1',200,'data: [DONE]\n\n'),('r1',200,'data: {"error":"failure"}\n\n')])
async def test_failed_or_unidentified_switch_restores_direct_probe(rig, identity, status, text):
    rig.replies['switch-one'] = identity, status, text
    await rig.tick(599)
    await rig.tick(600)
    await rig.tick(611)
    assert rig.calls == ['switch-one','proxy-one']
    foundation, _ = rig.records()
    assert foundation['latest']['observation_source'] == 'direct'
    assert foundation['latest']['status'] == 'success'
    rig.replies.clear()
    await rig.tick(660)
    await rig.tick(671)
    assert rig.calls == ['switch-one','proxy-one','switch-one']


@pytest.mark.asyncio
async def test_waits_for_all_sources_and_success_of_any_source_avoids_direct(rig):
    rig.value['gateways'].append(rig.value['gateways'][0] | {'id':'two','business_url':'http://switch-two'})
    rig.replies['switch-one'] = 'r1', 503, ''
    await rig.tick(599)
    await rig.tick(600)
    await rig.tick(611)
    assert rig.calls == ['switch-one']
    await rig.tick(620)
    await rig.tick(621)
    assert rig.calls == ['switch-one','switch-two']
    assert rig.records()[0]['latest']['source_probe_id'] == 'gateway:two'


@pytest.mark.asyncio
async def test_all_sources_fail_once_and_direct_failure_stays_separate(rig):
    rig.value['gateways'].append(rig.value['gateways'][0] | {'id':'two','business_url':'http://switch-two'})
    rig.replies = {host: ('r1',503,'') for host in ('switch-one','switch-two','proxy-one')}
    await rig.tick(599)
    for ts in (600,611,620,621,622):
        await rig.tick(ts)
    assert rig.calls == ['switch-one','switch-two','proxy-one']
    assert all(s['latest']['status'] == 'failure' for s in rig.records())


@pytest.mark.asyncio
async def test_missing_dispatch_expires_and_does_not_catch_up_after_suspend(rig):
    await rig.tick(599)
    await rig.tick(611)  # switch slot was missed, not replayed
    assert rig.calls == []
    await rig.tick(655)
    assert rig.calls == ['proxy-one']
    await rig.tick(900)  # old pending rounds cannot generate a burst
    assert rig.calls == ['proxy-one']


@pytest.mark.asyncio
async def test_import_failure_is_not_coverage_and_does_not_retry_generation(rig):
    await rig.tick(599)
    rig.import_failure = True
    await rig.tick(600)
    rig.import_failure = False
    await rig.tick(611)
    assert rig.calls == ['switch-one','proxy-one']
    assert rig.records()[0]['latest']['observation_source'] == 'direct'


@pytest.mark.asyncio
async def test_cutover_leaves_old_router_independent_and_new_router_shared(rig):
    rig.value['foundations'].append(copy.deepcopy(rig.value['foundations'][0]) |
        {'id':'two','name':'Two','router':{'id':'r2','proxy_url':'http://proxy-two'}})
    rig.replies['proxy-two'] = 'r2',200,SSE
    await rig.tick(599)
    for ts in (600,611,613):
        await rig.tick(ts)
    assert rig.calls == ['switch-one','proxy-two']
    rig.value['revision'] += 1
    rig.value['gateways'][0]['router_id'] = 'r2'
    rig.replies['switch-one'] = 'r2',200,SSE
    for ts in (630,660,671,673):
        await rig.tick(ts)
    assert rig.calls == ['switch-one','proxy-two','switch-one','proxy-one']
    one, two, switch = rig.records()
    assert one['records'][0]['actual_router_id'] == 'r1'
    assert one['latest']['observation_source'] == 'direct'
    assert two['latest']['observation_source'] == 'switch_shared'
    assert [r['actual_router_id'] for r in switch['records']] == ['r1','r2']


@pytest.mark.asyncio
async def test_late_pre_cutover_response_keeps_actual_identity_and_does_not_cover_new_router(rig):
    rig.value['foundations'].append(copy.deepcopy(rig.value['foundations'][0]) |
        {'id':'two','name':'Two','router':{'id':'r2','proxy_url':'http://proxy-two'}})
    rig.replies['proxy-two'] = 'r2',200,SSE
    rig.gates['switch-one'] = asyncio.Event()
    await rig.tick(599)
    await rig.tick(600, drain=False)
    rig.value['gateways'][0]['router_id'] = 'r2'
    await rig.tick(610, drain=False)
    rig.gates['switch-one'].set()
    await asyncio.gather(*rig.scheduler.running.values())
    await rig.tick(611)
    await rig.tick(613)
    await rig.tick(655)
    assert rig.records()[0]['latest']['actual_router_id'] == 'r1'
    assert rig.records()[1]['latest']['observation_source'] == 'direct'


@pytest.mark.asyncio
async def test_pause_stops_fallback_and_old_direct_request_drains_serially(rig):
    rig.value['gateways'] = []
    rig.gates['proxy-one'] = asyncio.Event()
    await rig.tick(599)
    await rig.tick(611, drain=False)
    rig.value['foundations'][0]['router']['maintenance'] = True
    await rig.tick(612, drain=False)
    await rig.tick(731, drain=False)
    assert rig.calls == ['proxy-one']
    assert rig.scheduler.running['foundation:one'].done() is False
    rig.gates['proxy-one'].set()
    await asyncio.gather(*rig.scheduler.running.values())


def test_provenance_roundtrip_old_compatibility_and_partial_metadata_are_unknown():
    target = {'probe_id':'foundation:one','probe_kind':'foundation','foundation_id':'one','name':'One',
              'environment':'','offset_seconds':11}
    result = {'started_at':602,'finished_at':603,'duration_seconds':1,'first_output_seconds':.1,
              'reason':'success','http_status':200,'actual_router_id':'r1'}
    raw = probes.samples('', result, target=target)
    assert probes.decode(raw,1,900,targets=[target])['services'][0]['latest']['status'] == 'success'
    raw = probes.samples('',result | {'observation_source':'switch_shared','source_probe_id':'gateway:one',
                                      'scheduled_at':600},target=target)
    assert probes.decode(raw,1,900,targets=[target])['services'][0]['latest']['scheduled_at'] == 600
    partial = '\n'.join(raw.splitlines()[:-1])
    assert probes.decode(partial,1,900,targets=[target])['services'][0]['latest']['status'] == 'unknown'
    # Two switches completing at the same millisecond remain two observations.
    second = raw.replace('gateway:one','gateway:two')
    assert len(probes.decode(raw+second,1,900,targets=[target])['services'][0]['records']) == 2


@pytest.mark.asyncio
async def test_newer_success_cancels_old_round_waiting_for_missing_source(rig):
    rig.value['gateways'].append(rig.value['gateways'][0] | {'id':'two','business_url':'http://switch-two'})
    rig.scheduler.offsets['gateway:two'] = 40
    rig.replies['switch-one'] = 'r1',503,''
    await rig.tick(599)
    await rig.tick(600)
    await rig.tick(611)
    # Miss the second switch's slot, then succeed in the following minute.
    rig.replies.clear()
    await rig.tick(660)
    await rig.tick(661)
    await rig.tick(671)
    assert rig.calls == ['switch-one','switch-one']
    assert not rig.scheduler.rounds


@pytest.mark.asyncio
async def test_serial_direct_requests_skip_busy_round_and_never_retry_immediately(rig):
    rig.value['gateways'] = []
    rig.gates['proxy-one'] = asyncio.Event()
    await rig.tick(599)
    await rig.tick(611,drain=False)
    await rig.tick(671,drain=False)
    assert rig.calls == ['proxy-one']
    rig.gates['proxy-one'].set()
    await asyncio.gather(*rig.scheduler.running.values())
    await rig.tick(672)
    assert rig.calls == ['proxy-one']
    await rig.tick(731)
    assert rig.calls == ['proxy-one','proxy-one']
