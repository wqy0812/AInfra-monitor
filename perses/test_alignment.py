import copy
import json
import re
from pathlib import Path
import pytest
from generate import panel
from project_split import read_resources, build, validate
from align_dashboards import align, transform, role_variable, scrape, MOONCAKE_METRICS, NODES, ordered

ROOT=Path(__file__).parent


def test_all_core_catalogs_match_and_a3_mixed_dashboard_is_gone():
    r=read_resources(ROOT/'projects');validate(r)
    assert len(r['dashboards'])==30
    assert all(d['metadata']['name']!='backend-diagnostics' for d in r['dashboards'])
    for family in ('backend-performance','backend-prefill','backend-decode','accelerator-resources','gateway-generation','gateway-requests','monitoring-health'):
        sets=[]
        for d in r['dashboards']:
            if d['metadata']['name']!=family:continue
            keys=ordered(d)
            if family.startswith('backend-') or family=='accelerator-resources':keys=[k for k in keys if k.startswith('core-')]
            if family in ('backend-prefill','backend-decode'):
                # DCU shows KV pool Token values instead of the core-kv ratio; A3/XPU only expose the ratio.
                dcu=d['metadata']['project']=='dcu-monitoring'
                assert ('core-kv' in keys)!=dcu and (family.replace('backend','bn')+'-kv-capacity' in ordered(d)[:2])==dcu,(d['metadata']['project'],family)
                keys=[k for k in keys if k!='core-kv']
            sets.append([(k,d['spec']['panels'][k]['spec']['display']['name']) for k in keys])
        assert len(sets)==3 and sets[0]==sets[1]==sets[2],family
    hosts=[d for d in r['dashboards'] if d['metadata']['name'] in ('a3-hosts','hosts-dcu','hosts-xpu')]
    cores=[[(k,d['spec']['panels'][k]['spec']['display']['name']) for k in ordered(d) if k.startswith('core-')] for d in hosts]
    assert len(cores[0])==8 and cores[0]==cores[1]==cores[2]


def test_alignment_and_generation_are_idempotent_for_project_subsets():
    r=read_resources(ROOT/'projects')
    for p in NODES:
        sub={k:[d for d in ds if d['metadata'].get('project',d['metadata']['name'])==p] for k,ds in r.items()}
        assert align(sub)[0]==sub
        validate(build(sub))


def test_roles_partition_raw_and_derived_without_relabelling_series():
    p=panel('test',[('m{environment="a3-vllm",node=~"$node"}','{{node}}')],'%')
    old=copy.deepcopy(p);a=transform(p,'a3-monitoring',role='prefill')
    q=a['spec']['queries'][0]['spec']['plugin']['spec']['query']
    assert 'node=~"a3-1"' in q and 'node=~"$node"' in q and p==old
    raw=transform(p,'a3-monitoring',selectable=True)['spec']['queries'][0]['spec']['plugin']['spec']['query']
    assert 'node=~"$role"' in raw
    for project in NODES:
        values=role_variable(project)['spec']['plugin']['spec']['values']
        for idx,role in enumerate(('prefill','decode')):
            regex=values[idx+1]['value']
            assert re.fullmatch(regex,role) and re.fullmatch(regex,NODES[project][idx])
            assert not re.fullmatch(regex,NODES[project][1-idx])
    p['spec']['queries'][0]['spec']['plugin']['spec']['query']='monitoring_chart_value{environment="a3-vllm",path=~"nodes.(prefill|decode).percentiles.itl.samples"}'
    q=transform(p,'a3-monitoring',selectable=True)['spec']['queries'][0]['spec']['plugin']['spec']['query']
    assert 'nodes.$role.' in q and 'node=' not in q


def test_mooncake_scrape_is_only_an_append_and_does_not_claim_cache_hits():
    old='global:\n  scrape_interval: 5s\nscrape_configs:\n- job_name: unrelated\n'
    new=scrape(old)
    assert new.startswith(old)
    assert 'environment: a3-vllm' in new and '9003' in new
    assert all(m in new for m in MOONCAKE_METRICS)
    assert 'mem_cache_hit_nums_' not in new
    with pytest.raises(ValueError):scrape(new)
    cache=json.loads((ROOT/'projects/a3-monitoring/dashboards/a3-cache.json').read_text())
    added={k:p for k,p in cache['spec']['panels'].items() if k.startswith('mooncake-')}
    assert len(added)==10
    for key,p in added.items():
        assert '命中率' not in p['spec']['display']['name']
        for q in p['spec']['queries']:
            expr=q['spec']['plugin']['spec']['query']
            assert 'environment="a3-vllm"' in expr and 'job="mooncake-a3"' in expr
            assert 'timestamp(' in expr and 'up{' in expr
            if key in ('mooncake-requests','mooncake-failures','mooncake-evictions','mooncake-evicted-keys','mooncake-evicted-bytes'):
                assert 'resets(' in expr and 'count_over_time(' in expr
