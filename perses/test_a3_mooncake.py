import json
import re
from pathlib import Path
import a3_mooncake as m


def test_selected_catalog_and_idempotence():
    doc = json.loads((Path(__file__).parent / 'projects/a3-monitoring/dashboards/a3-cache.json').read_text())
    output = m.configure(doc)
    assert output == m.configure(output)
    queries = '\n'.join(q['spec']['plugin']['spec']['query'] for key,p in output['spec']['panels'].items() if key.startswith('mooncake-') for q in p['spec']['queries'])
    assert set(re.findall(r'\b(?:master_|segment_|ha_)\w+', queries)) <= set(m.METRICS)
    assert sum(key.startswith('mooncake-') for key in output['spec']['panels']) == 10
    assert '$role' not in queries and 'or vector(0)' not in queries
    refs = [i['content']['$ref'].split('/')[-1] for l in output['spec']['layouts'] for i in l['spec']['items']]
    assert len(refs) == len(set(refs)) and set(refs) == set(output['spec']['panels'])
    scrape = (Path(__file__).parents[1] / 'deploy/scrape.yml').read_text().split('- job_name: mooncake-a3')[1]
    assert all(name in scrape for name in m.METRICS)


def test_queries_real_vm():
    import os, time, httpx, pytest
    url = os.environ.get('MONITORING_TEST_VM_URL')
    if not url: pytest.skip('Requires disposable local VictoriaMetrics')
    assert url.startswith('http://127.0.0.1:')
    client = httpx.Client(base_url=url, trust_env=False, timeout=20)
    end = int(time.time() // 5) * 5 - 18000
    lines = []
    for i in range(61):
        ts = (end - 300 + i * 5) * 1000
        def emit(name, value, extra=''):
            lines.append(f'{name}{{environment="a3-vllm",job="mooncake-a3",instance="native:9003"{extra}}} {value} {ts}')
        emit('up', 1)
        for name in m.GAUGES: emit(name, 10 if 'allocated' in name else 100, ',segment="one"' if name.startswith('segment_') else '')
        for name in m.COUNTERS: emit(name, (i + 100) * (1 if 'failures' in name else 10))
        for j,bound in enumerate(m.BOUNDS): emit('master_value_size_bytes_bucket', (i+100)*(j+1), ',le="' + bound + '"')
        emit('master_value_size_bytes_count', (i+100)*len(m.BOUNDS))
        emit('master_value_size_bytes_sum', (i+100)*100000)
    client.post('/api/v1/import/prometheus', content='\n'.join(lines)+'\n').raise_for_status()
    client.get('/internal/force_flush').raise_for_status()
    doc=json.loads((Path(__file__).parent/'projects/a3-monitoring/dashboards/a3-cache.json').read_text())
    for key,panel in doc['spec']['panels'].items():
        if not key.startswith('mooncake-'): continue
        for query in panel['spec']['queries']:
            expr=query['spec']['plugin']['spec']['query'].replace('$__interval','15s')
            r=client.get('/api/v1/query',params={'query':expr,'time':end,'nocache':'1'});r.raise_for_status()
            assert r.json()['data']['result'], (key,expr,r.text)
    client.close()
