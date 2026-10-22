"""Registration drives queries and rules without environment/name allowlists."""
import asyncio
import copy
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from monitoring import (accelerator_history, api, cache_observations, cache_sources,
                        gateway_live, native_rules, native_schema, raw_gauges,
                        request_profile, source_registry as registry)


def catalog(adapter='a3-vllm-v1', *, multi=False):
    names=[('compute.one', 'prefill'), ('compute.two', 'decode')]
    if multi:names.append(('compute.three', 'prefill'))
    return {'schema_version':1,'revision':7,'gateways':[], 'foundations':[{
        'id':'new-foundation','name':'新的底座','monitoring_source':'foundation-new',
        'monitoring_adapter':adapter,'enabled':True,
        'resource_nodes':[{'name':name,'role':role} for name,role in names],
        'monitoring_endpoints':[{'id':'endpoint-'+str(i),'name':name,'type':'inference',
            'url':'http://'+name+':9000/metrics','node':name,'role':role,'enabled':True,
            'auth':{'type':'basic','username':'reader','password':'do-not-return'}}
            for i,(name,role) in enumerate(names)]}]}


def flat(config):
    return [rule for group in config['groups'] for rule in group['rules']]


@pytest.mark.parametrize('adapter', list(registry.ADAPTERS))
def test_new_source_identity_binds_all_readers_and_preserves_adapter_semantics(adapter):
    data=catalog(adapter)
    with registry.use_catalog(data):
        assert list(registry.sources())==['foundation-new']
        public=registry.source('foundation-new')
        assert 'do-not-return' not in json.dumps(public)
        assert request_profile.validate_environment('foundation-new')=='foundation-new'
        assert cache_sources.source('foundation-new')['semantics']==registry.ADAPTERS[adapter]['semantics']
        queries=[api.history_expression('foundation-new'), raw_gauges.query('foundation-new'),
                 accelerator_history.expression('foundation-new',5), *cache_observations.selectors('foundation-new'),
                 *gateway_live.expressions('foundation-new',5).values()]
        for query in queries:
            assert 'foundation-new' in query
            assert not any('environment="'+old+'"' in query for old in registry.LEGACY_NODES)
        definition=api.source_catalog()['sources'][0]
        assert definition['foundation_id']=='new-foundation'
        assert definition['nodes']=={'prefill':'compute.one','decode':'compute.two'}


@pytest.mark.parametrize('adapter', list(registry.ADAPTERS))
def test_rule_topology_and_counts_come_from_registration(adapter):
    data=catalog(adapter,multi=True)
    config,audit=native_rules.build(data)
    rules=flat(config)
    assert audit['catalog_revision']==7
    expected=[r for r in rules if r['record'].endswith('backend_expected_instances')]
    assert {(r['labels'].get('node'),r['expr']) for r in expected if r['labels'].get('monitoring_scope')!='role'}=={
        ('compute.one','vector(1)'),('compute.two','vector(1)'),('compute.three','vector(1)')}
    assert next(r for r in expected if r['labels'].get('monitoring_scope')=='role')['expr']=='vector(2)'
    for rule in rules:
        assert not any('environment="'+old+'"' in rule['expr'] for old in registry.LEGACY_NODES)
        assert not any(rule['labels'].get('node')==old for nodes in registry.LEGACY_NODES.values() for old in nodes.values())
    pooled=next(r for r in rules if r['record'].endswith('backend_latency_seconds')
                and r['labels'].get('monitoring_scope')=='role' and r['labels']['kind']=='ttft')
    assert 'histogram_quantiles' in pooled['expr'] and 'sum by(le,environment)' in pooled['expr']
    assert 'by(le,environment,node)' not in pooled['expr']


def test_multi_node_values_do_not_replace_each_other_or_average_quantiles():
    def row(value,node=None,summary=False):
        labels={'__name__':native_rules.PREFIX+'backend_latency_seconds',
                'environment':'foundation-new','role':'prefill','kind':'ttft','quantile':'0.95'}
        if node:labels['node']=node
        if summary:labels['monitoring_scope']='role'
        return {'metric':labels,'values':[[100,str(value)]]}
    with registry.use_catalog(catalog(multi=True)):
        rows=[row(2,'compute.one'),row(8,'compute.three'),row(7,summary=True)]
        for ordered in (rows,rows[::-1]):
            point=native_schema.points(ordered,'foundation-new',100,100,5)[0]
            assert point['nodes']['prefill']['percentiles']['ttft']['p95']==7
            assert point['node_details']['compute%2Eone']['percentiles']['ttft']['p95']==2
            assert point['node_details']['compute%2Ethree']['percentiles']['ttft']['p95']==8
        point=native_schema.points(rows[:2],'foundation-new',100,100,5)[0]
        assert point['nodes']['prefill']['percentiles']['ttft']['p95'] is None


def test_dashboard_node_regex_and_label_replace_follow_new_topology():
    with registry.use_catalog(catalog(multi=True)):
        expression='label_replace(metric{environment="a3-vllm",node=~"a3-1"}, "role", "Prefill", "node", "a3-1")'
        rewritten=registry.rewrite_query(expression,'foundation-new')
        assert 'a3-1' not in rewritten
        assert rewritten.count('compute\\\\.one|compute\\\\.three')==2


def test_dashboard_literal_instance_inventory_and_expected_count_follow_registration():
    with registry.use_catalog(catalog(multi=True)):
        expression='count by(environment,node,instance) (up{environment="a3-vllm",job="vllm-a3",node="a3-1",instance=~"old:(7100|7101)"}) == 4)'
        rewritten=registry.rewrite_query(expression,'foundation-new')
        assert 'old:' not in rewritten and '== 4)' not in rewritten
        assert 'compute\\\\.one:9000|compute\\\\.three:9000' in rewritten
        assert '== on(environment,node) last_over_time(monitoring_native_backend_expected_instances' in rewritten
        variable='up{environment="a3-vllm",job="vllm-a3",instance=~"$instance"}'
        assert 'instance=~"$instance"' in registry.rewrite_query(variable,'foundation-new')


def test_dashboard_per_node_selectors_exclude_pooled_role_records():
    with registry.use_catalog(catalog(multi=True)):
        expression='sum(monitoring_native_backend_requests_per_second{environment="a3-vllm",node=~"$node"})'
        rewritten=registry.rewrite_query(expression,'foundation-new')
        assert 'monitoring_scope=""' in rewritten
        # Applying the binder twice must not duplicate constraints.
        assert registry.rewrite_query(rewritten,'foundation-new')==rewritten


def test_custom_paths_have_distinct_instances_and_import_preserves_identity():
    endpoint={'url':'http://host.example:9000/prefill/metrics'}
    assert registry.endpoint_instance(endpoint)=='host.example:9000/prefill/metrics'
    assert registry.endpoint_instance({'url':'http://host.example:9000'})=='host.example:9000/'
    assert registry.endpoint_instance({'url':'http://host.example:9000/metrics'})=='host.example:9000'
    assert registry.endpoint_instance({**endpoint,'scrape_labels':{}})=='host.example:9000'
    assert registry.endpoint_instance({**endpoint,'scrape_labels':{'instance':'original'}})=='original'


def test_empty_config_does_not_reenable_legacy_environments():
    with registry.use_catalog({'schema_version':1,'revision':8,'foundations':[],'gateways':[]}):
        assert not registry.sources() and not native_schema.NODES
        rules=flat(native_rules.build()[0])
        assert len(rules)==1 and rules[0]['expr']=='vector(0) < 0'
        assert api.source_catalog()=={'sources':[]}
        with pytest.raises(ValueError):raw_gauges.query('dcu-pd')


def test_shared_resource_node_keeps_details_without_becoming_prefill_or_decode():
    data=catalog();data['foundations'][0]['resource_nodes'].append({'name':'storage.shared','role':'shared'})
    row={'metric':{'__name__':raw_gauges.PREFIX+'host_gauge','environment':'foundation-new',
                  'node':'storage.shared','role':'shared','field':'memory_total'},'values':[[100,'1000']]}
    with registry.use_catalog(data):
        point=native_schema.points([row],'foundation-new',100,100,5)[0]
        assert point['node_details']['storage%2Eshared']['host']['memory_total']==1000
        assert 'shared' not in point['nodes']


def test_file_refresh_retains_last_valid_snapshot_on_invalid_update(tmp_path,monkeypatch):
    path=tmp_path/'catalog.json';path.write_text(json.dumps(catalog()))
    monkeypatch.setenv('MONITORING_CATALOG_FILE',str(path))
    assert registry.catalog()['revision']==7
    path.write_text('{bad json')
    assert registry.catalog()['revision']==7 and registry.load_error()
    changed=catalog();changed['revision']=8;changed['foundations']=[]
    path.write_text(json.dumps(changed))
    assert not registry.sources() and registry.load_error() is None


@pytest.mark.asyncio
async def test_directory_add_remove_and_change_restarts_only_affected_workers(tmp_path,monkeypatch):
    path=tmp_path/'catalog.json';path.write_text(json.dumps(catalog()))
    monkeypatch.setenv('MONITORING_CATALOG_FILE',str(path))
    async def run(self):await asyncio.Event().wait()
    monkeypatch.setattr(api.Service,'run',run)
    app=SimpleNamespace(state=SimpleNamespace(services={},source_tasks={},catalog_error=None))
    await api.refresh_directory(app)
    first=app.state.services['foundation-new']
    await api.refresh_directory(app)
    assert app.state.services['foundation-new'] is first
    changed=catalog();changed['revision']=8;changed['foundations'][0]['resource_nodes'][0]['name']='replacement'
    changed['foundations'][0]['monitoring_endpoints'][0]['node']='replacement'
    path.write_text(json.dumps(changed))
    await api.refresh_directory(app)
    assert app.state.services['foundation-new'] is not first
    assert first.client.is_closed
    changed['foundations']=[];path.write_text(json.dumps(changed))
    await api.refresh_directory(app)
    assert app.state.services=={} and app.state.source_tasks=={} and app.state.service is None


def test_legacy_and_new_cache_bindings_have_disjoint_time_windows(tmp_path,monkeypatch):
    legacy={'backend_url':'http://backend.old:9000','environment':'foundation-new','valid_from':100,'evidence':'original'}
    path=tmp_path/'bindings.json';path.write_text(json.dumps({'version':1,'bindings':[legacy]}))
    monkeypatch.setenv('CACHE_SOURCE_CONFIG',str(path))
    data=catalog()
    data['foundations'][0]['router']={'bindings':[{'backend_url':'http://backend.new:9000',
        'proxy_url':'http://proxy.example:18081','environment':'foundation-new','valid_from':200,'evidence':'catalog 7'}]}
    with registry.use_catalog(data):
        rows=cache_sources.bindings()
        assert rows[0]['valid_until']==200 and rows[1]['valid_from']==200
        assert cache_sources.resolve({'backend_url':legacy['backend_url'],'proxy_url':'http://proxy.example','at':150})['status']=='resolved'
        assert cache_sources.resolve({'backend_url':legacy['backend_url'],'proxy_url':'http://proxy.example','at':250})['status']=='unavailable'


@pytest.mark.skipif(not os.environ.get('NATIVE_RECORDING_TEST_VM'),reason='Disposable VM required')
def test_actual_vm_new_source_pools_histogram_buckets_and_counts_nodes():
    url=os.environ['NATIVE_RECORDING_TEST_VM'];assert url=='http://127.0.0.1:18543'
    data=catalog(multi=True);end=int(time.time()//5)*5-100
    lines=[]
    for index,node in enumerate(('compute.one','compute.three')):
        labels='environment="foundation-new",job="vllm-a3",node="'+node+'",instance="'+node+':9000"'
        for offset in range(-65,1,5):
            ts=(end+offset)*1000;count=100+offset+65
            lines.append(f'up{{{labels}}} 1 {ts}')
            for bound,factor in (('1',1 if index==0 else 0),('10',1),('+Inf',1)):
                lines.append(f'vllm:time_to_first_token_seconds_bucket{{{labels},le="{bound}"}} {count*factor} {ts}')
            lines.append(f'vllm:time_to_first_token_seconds_count{{{labels}}} {count} {ts}')
    rules=flat(native_rules.build(data)[0])
    pooled=next(r for r in rules if r['record'].endswith('backend_latency_seconds')
                and r['labels'].get('monitoring_scope')=='role' and r['labels']['kind']=='ttft')
    with httpx.Client(base_url=url,trust_env=False) as client:
        client.post('/api/v1/import/prometheus',content='\n'.join(lines)+'\n').raise_for_status()
        client.get('/internal/force_flush').raise_for_status()
        result=client.post('/api/v1/query',data={'query':pooled['expr'],'time':end,'step':'5s','nocache':1}).json()
        assert result['status']=='success'
        p95=next(r for r in result['data']['result'] if r['metric']['quantile']=='0.95')
        assert float(p95['value'][1])==pytest.approx(9.1)


@pytest.mark.skipif(not os.environ.get('NATIVE_RECORDING_TEST_VM'),reason='Disposable VM required')
def test_actual_vm_rebound_rules_and_dashboard_queries_parse_for_every_adapter():
    url=os.environ['NATIVE_RECORDING_TEST_VM'];assert url=='http://127.0.0.1:18543'
    projects={'dcu-sglang-v1':'dcu-monitoring','a3-vllm-v1':'a3-monitoring','xpu-sglang-v1':'xpu-monitoring'}
    with httpx.Client(base_url=url,trust_env=False,timeout=10) as client:
        for adapter,project in projects.items():
            data=catalog(adapter,multi=True)
            queries={rule['expr'] for rule in flat(native_rules.build(data)[0])}
            with registry.use_catalog(data):
                for path in (Path(__file__).parents[1]/'perses/projects'/project).rglob('*.json'):
                    document=json.loads(path.read_text())
                    for panel in document.get('spec',{}).get('panels',{}).values():
                        for query in panel['spec'].get('queries',[]):
                            expression=registry.rewrite_query(query['spec']['plugin']['spec']['query'],'foundation-new')
                            expression=expression.replace('$__interval','60s')
                            for variable in ('$role','$node','$device'):expression=expression.replace(variable,'.*')
                            queries.add(expression)
            for expression in queries:
                response=client.post('/api/v1/query',data={'query':expression,'time':int(time.time())-300,'nocache':1})
                assert response.status_code==200, (adapter,expression,response.text)
                assert response.json()['status']=='success', (adapter,expression,response.text)
