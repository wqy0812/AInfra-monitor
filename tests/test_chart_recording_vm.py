"""Compare raw native expressions to independent Python replay on real VM."""
import copy
import json
import os
import time

import httpx
import pytest
from monitoring import chart_recording as charts, a3, host_cpu
from monitoring.api import get
from monitoring.replay import decode_export, replay
from tests.test_host_cpu_vm import cpu_vm_at


@pytest.fixture(scope="module")
def chart_cpu_vm():
    url=os.environ.get("HOST_CPU_TEST_VM_URL")
    if not url:pytest.skip("Requires disposable local VictoriaMetrics")
    yield from cpu_vm_at(url, int(time.time()//5)*5-12000)


@pytest.mark.parametrize('field', [f for f in charts.FIELDS if f['schema'] == host_cpu.SCHEMA], ids=lambda f:f['path'])
def test_cpu_expression_matches_existing_replay_acceptance(chart_cpu_vm, field):
    query, ends = chart_cpu_vm
    for end in ends.values():
        expected = host_cpu.samples(query(host_cpu.query_expression(), end-65, end), end-65, end)
        role, metric = field['path'].split('.')[1:]
        actual = {int(t):float(v) for r in query(field['expression'], end-65, end) for t,v in r['values']}
        want = {t:v for (t,r,m),v in expected.items() if r == role and m == metric}
        assert actual == pytest.approx(want, rel=1e-12, abs=1e-10)
        vector={int(t):float(v) for row in query(charts.cpu_vector(),end-65,end)
                if row['metric']['path']==field['path'] for t,v in row['values']}
        assert vector == pytest.approx(want,rel=1e-12,abs=1e-10)


CASES = ('normal','zero','no_capacity','oversized','negative','missing','down','stale','newer_up',
         'extra_label','duplicate','extra_source','staggered_source','missing_up','gap','jitter')


def observations(environment, start, case):
    job = 'mooncake-a3' if environment == 'a3-vllm' else 'mooncake'
    rows = []
    for offset in range(0,81,5):
        if case == 'stale' and offset >= 65: continue
        if case == 'gap' and offset in (65,70): continue
        ts = start + offset + (.37 if case == 'jitter' else 0)
        values = {'up': 0 if case == 'down' and offset>=70 else 1,
                  'master_allocated_bytes': 32, 'master_total_capacity_bytes':128,
                  'master_allocated_file_size_bytes': 0, 'master_total_file_capacity_bytes':1024}
        if case == 'missing_up': values.pop('up')
        if offset >= 70:
            if case == 'zero': values['master_total_file_capacity_bytes'] = 0
            if case == 'no_capacity': values.update(master_allocated_file_size_bytes=32,master_total_file_capacity_bytes=0)
            if case == 'oversized': values['master_allocated_file_size_bytes'] = 2048
            if case == 'negative': values['master_allocated_file_size_bytes'] = -1
            if case == 'missing': values.pop('master_allocated_file_size_bytes')
            if case == 'newer_up': values = {'up':1}
        for name,value in values.items():
            tags = {'__name__':name,'job':job,'instance':'host:9003','node':'store','environment':environment}
            if case == 'extra_label' and name == 'master_allocated_bytes': tags['unexpected'] = 'label'
            item = {'metric':tags,'timestamps':[round(ts*1000)],'values':[value]}
            rows.append(item)
            if case == 'duplicate' and name == 'master_allocated_bytes':
                extra = copy.deepcopy(item);extra['metric']['duplicate']='yes';rows.append(extra)
            if case in ('extra_source','staggered_source') and offset>=70:
                extra=copy.deepcopy(item);extra['metric']['instance']='another:9003'
                if case == 'staggered_source':extra['timestamps']=[round((ts-1)*1000)]
                rows.append(extra)
    return rows


@pytest.fixture(scope='module')
def store_vm():
    url=os.environ.get('MONITORING_TEST_VM_URL')
    if not url:pytest.skip('Requires disposable local VictoriaMetrics')
    assert url.startswith('http://127.0.0.1:')
    client=httpx.Client(base_url=url,trust_env=False,timeout=30)
    base=int(time.time()//5)*5-30000
    cases={}
    lines=[]
    for env in ('dcu-pd','a3-vllm'):
        for i,case in enumerate(CASES):
            start=base+i*300
            rows=observations(env,start,case)
            cases[(env,case)]=(start,rows)
            for row in rows:
                tags=dict(row['metric']);name=tags.pop('__name__')
                labels=','.join(k+'='+json.dumps(v) for k,v in tags.items())
                for ts,v in zip(row['timestamps'],row['values']):lines.append(f'{name}{{{labels}}} {v} {ts}')
    client.post('/api/v1/import/prometheus',content='\n'.join(lines)+'\n').raise_for_status()
    client.get('/internal/force_flush').raise_for_status()
    yield client,cases
    client.close()


@pytest.mark.parametrize('environment', ['dcu-pd','a3-vllm'])
@pytest.mark.parametrize('case', CASES)
def test_store_values_and_validity_match_replay(store_vm,environment,case):
    client,cases=store_vm; start,raw=cases[(environment,case)]
    parser,runner=(a3.decode_export,a3.replay) if environment=='a3-vllm' else (decode_export,replay)
    _,points=runner(parser(raw),start,start+95)
    for field in charts.FIELDS:
        if field['environment']!=environment or not field['path'].startswith('mooncake.'):continue
        response=client.get('/api/v1/query_range',params={'query':field['expression'],'start':start,'end':start+95,'step':5,'nocache':1,'latency_offset':'1ms'})
        response.raise_for_status()
        actual={int(t):float(v) for r in response.json()['data']['result'] for t,v in r['values']}
        expected={p['ts']:get(p,field['path']) for p in points if get(p,field['path']) is not None}
        assert actual == pytest.approx(expected,rel=1e-12,abs=1e-10), (environment,case,field['path'],actual,expected)
