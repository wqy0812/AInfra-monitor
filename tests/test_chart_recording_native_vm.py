"""Official vmalert replay -> stored chart records -> existing API contract."""
import json
import os
import subprocess
import time

import httpx
import pytest
from monitoring import chart_recording as charts
from monitoring.api import history_expression
from tests.test_chart_recording_vm import observations


@pytest.mark.skipif(not os.environ.get('CHART_NATIVE_VM_TEST'), reason='Requires disposable VM and local vmalert image')
def test_native_replay_preserves_history_cutover_and_gap_flags(tmp_path, monkeypatch):
    url=os.environ['MONITORING_TEST_VM_URL']
    assert url == 'http://127.0.0.1:18543'
    assert subprocess.check_output(['docker','context','inspect','--format','{{.Endpoints.docker.Host}}'],text=True).strip().startswith('unix://')
    client=httpx.Client(base_url=url,trust_env=False,timeout=30)
    start=int(time.time()//5)*5-50000
    lines=[]
    for env in ('a3-vllm','dcu-pd'):
        for row in observations(env,start,'gap'):
            tags=dict(row['metric']);name=tags.pop('__name__')
            labels=','.join(k+'='+json.dumps(v) for k,v in tags.items())
            lines.append(f"{name}{{{labels}}} {row['values'][0]} {row['timestamps'][0]}")
    # The pre-cutover sentinel must not be rewritten by native replay.
    labels='environment="a3-vllm",schema="v1",path="mooncake.capacity.used"'
    lines.append(f'monitoring_chart_value{{{labels}}} 999 {(start+5)*1000}')
    client.post('/api/v1/import/prometheus',content='\n'.join(lines)+'\n').raise_for_status()
    client.get('/internal/force_flush').raise_for_status()
    config=tmp_path/'rules.json';config.write_text(json.dumps(charts.recording_rules(start+10)))
    from datetime import datetime,timezone
    iso=lambda t:datetime.fromtimestamp(t,timezone.utc).isoformat()
    # The checked local image is fixed by version and must already exist.
    image=subprocess.check_output(['docker','image','inspect','--format','{{.Id}}','victoriametrics/vmalert:v1.151.0'],text=True).strip()
    result=subprocess.run(['docker','run','--rm','--network','monitoring-local-tests_default',
        '--cpus','1','--memory','512m','-v',str(config)+':/rules.json:ro',image,
        '-rule=/rules.json','-datasource.url=http://monitoring-test-vm:8428','-remoteWrite.url=http://monitoring-test-vm:8428',
        '-datasource.queryStep=0','-replay.timeFrom='+iso(start),'-replay.timeTo='+iso(start+101),
        '-replay.rulesDelay=0','-replay.disableProgressBar','-replay.maxDatapointsPerQuery=60'],
        text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=120)
    assert result.returncode==0,result.stderr
    client.get('/internal/force_flush').raise_for_status()
    def query(expression,left,right,step=5):
        r=client.get('/api/v1/query_range',params={'query':expression,'start':left,'end':right,'step':step,'latency_offset':'1ms','nocache':1})
        r.raise_for_status();return r.json()['data']['result']
    before=query('monitoring_chart_value{'+labels+'}',start+5,start+5)
    assert float(before[0]['values'][0][1])==999
    for env in ('a3-vllm','dcu-pd'):
        values=query(history_expression(env,'value',5),start+10,start+100)
        valid=query(history_expression(env,'valid',5),start+10,start+100)
        by_path={r['metric']['path']:dict(r['values']) for r in values}
        flags={r['metric']['path']:dict(r['values']) for r in valid}
        for field in [f for f in charts.FIELDS if f['environment']==env]:
            path=field['path']
            expected={t:float(v) for r in query(field['expression'],start+10,start+100) for t,v in r['values']}
            actual={t:float(v) for t,v in by_path.get(path,{}).items() if flags[path].get(t)=='1'}
            assert actual==pytest.approx(expected)
            assert len(flags[path])==19
        gaps=query(history_expression(env,'gaps',60),start+100,start+100,60)
        assert all(float(r['values'][0][1])==0 for r in gaps)
    import asyncio
    from monitoring import api
    monkeypatch.setattr(api, 'VM', url)
    monkeypatch.setattr(api, 'STATE', tmp_path)
    async def history():
        service=api.Service('a3-vllm')
        try:
            data=await service._history(1,start+10,start+80,5,'summary')
            last=data['points'][-1]
            assert last['mooncake']['capacity']=={'used':32,'total':128,'ratio':.25}
            assert last['mooncake']['ssd_capacity']=={'used':0,'total':1024,'ratio':0}
            assert 'capacity' not in last['mooncake']['gap_before']
        finally:await service.close()
    asyncio.run(history())
    client.close()
