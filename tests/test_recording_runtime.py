"""Restart identities, bounded replay and concurrent correction safety."""
import asyncio
import concurrent.futures
import json
import sys
from pathlib import Path

import pytest
from monitoring.recording_runtime import allocate, replay_range, clear_repaired, command, verify
from monitoring.perses_recording import RecordingService
from monitoring.perses_acceleration import digest

CATALOG = json.loads((Path(__file__).parents[1] / 'monitoring/perses_acceleration_catalog.json').read_text())


def test_identity_allocation_is_durable_and_concurrent(tmp_path):
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        identities = list(pool.map(lambda _: allocate(tmp_path), range(40)))
    generations = sorted(x['generation'] for x in identities)
    assert len(set(generations)) == len({x['publication'] for x in identities}) == 40
    assert allocate(tmp_path)['generation'] > generations[-1]
    assert json.loads((tmp_path/'perses-recording-generation.json').read_text())['generation'] > generations[-1]


def test_replay_bounds_align_and_reject_future_or_unbounded_work():
    start, end = replay_range(CATALOG, 200001)
    assert start % 3600 == 0 and end == 199941
    assert 43200 <= end-start < 46800
    for left, right in [(0, 200000), (100, 99), (100, float('nan')), (199000, 200000)]:
        with pytest.raises(ValueError): replay_range(CATALOG, 200001, left, right)
    args = command('/bin/vmalert', 'http://vm', Path('/rules'), '127.0.0.1:0', (0, 3600))
    assert '-replay.timeTo=1970-01-01T01:00:01+00:00' in args
    assert '-group.maxStartDelay=1s' in args


def test_repair_keeps_concurrent_invalidations_and_other_panels(tmp_path):
    old = {'start':100,'end':200,'panel':'p','reason':'first'}
    concurrent = dict(old, reason='new correction')
    other = dict(old, panel='q')
    path = tmp_path/'perses-acceleration-admin.json'
    path.write_text(json.dumps({'disabled_groups':['a3'],'invalidated':[old,concurrent,other]}))
    clear_repaired(tmp_path,[old],100,200,'p')
    assert json.loads(path.read_text()) == {'disabled_groups':['a3'],'invalidated':[concurrent,other]}


@pytest.mark.asyncio
async def test_coverage_cannot_pass_with_only_old_generation(tmp_path):
    service = RecordingService('http://vm', tmp_path, dict(CATALOG,panels=[CATALOG['panels'][0]],steps=[5]), client=object())
    async def stored(panel, step, ticks, minimum_generation):
        assert minimum_generation == 12
        return {}
    service.stored=stored
    with pytest.raises(ValueError,match='incomplete'):
        await verify(service,100,110,12)


def test_status_never_claims_freshness_from_an_old_supervisor(tmp_path):
    service=RecordingService('http://vm',tmp_path,CATALOG,client=object(),now=lambda:100)
    path=tmp_path/'perses-recording-runtime.json'
    path.write_text(json.dumps({'updated_at':50,'live':{'state':'running'}}))
    assert not service.status()['runtime_fresh']
    path.write_text(json.dumps({'updated_at':99,'live':{'state':'running'}}))
    assert service.status()['runtime_fresh']

@pytest.mark.asyncio
async def test_supervisor_survives_finished_replay_and_rotates_identity(tmp_path, monkeypatch):
    import httpx
    from types import SimpleNamespace
    import monitoring.recording_runtime as runtime
    binary=tmp_path/'fake-vmalert'
    binary.write_text('#!'+sys.executable+'\nimport sys,time\nif any(x.startswith("-replay.timeFrom") for x in sys.argv):sys.exit(0)\nwhile True:time.sleep(.1)\n')
    binary.chmod(0o700)
    catalog=tmp_path/'catalog.json';catalog.write_text(json.dumps(CATALOG))
    original=runtime.RecordingService
    class Service(original):
        def __init__(self,*args,**kwargs):
            kwargs['client']=kwargs.get('client') or httpx.AsyncClient(transport=httpx.MockTransport(
                lambda _:httpx.Response(200,json={'data':{'groups':[{'rules':[{}]*158}]}})))
            super().__init__(*args,**kwargs)
    async def verified(*args):return 123
    monkeypatch.setattr(runtime,'RecordingService',Service)
    monkeypatch.setattr(runtime,'verify',verified)
    loop=asyncio.get_running_loop()
    def handler(sig,callback):
        if sig==runtime.signal.SIGTERM:loop.call_later(4.5,callback)
    monkeypatch.setattr(loop,'add_signal_handler',handler)
    args=SimpleNamespace(state=tmp_path,catalog=catalog,binary=str(binary),vm='http://vm',listen='127.0.0.1:18432')
    await runtime.run(args)
    first=json.loads((tmp_path/'perses-recording-runtime.json').read_text())
    assert first['replay']['state']=='complete' and first['replay']['verified_ticks']==123
    assert first['live']['state']=='stopped'
    # A malformed operator request is reported without killing the live producer.
    (tmp_path/'perses-acceleration-admin.json').write_text(json.dumps({'disabled_groups':[], 'invalidated':[],
        'native_replays':[{'id':'bad-request','panel':'unknown'}]}))
    await runtime.run(args)
    second=json.loads((tmp_path/'perses-recording-runtime.json').read_text())
    assert second['live']['generation']>first['replay']['generation']
    assert second['replay']['state']=='failed' and 'Unknown replay panel' in second['replay']['error']
    assert second['live']['loaded_rules']==158
    assert second['chart_start']==first['chart_start']
    assert second['chart_rule_count']==32 and second['rule_count']==158
    config=json.loads(Path(second['live']['config']).read_text())
    assert sum(len(group['rules']) for group in config['groups'])==158
