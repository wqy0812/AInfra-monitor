"""Migration safety: accounting, query preservation, grouping and rollback."""
import copy
import json
from pathlib import Path
from unittest.mock import patch
import pytest
from generate import panel, variable
from dashboard_reorg import migrate, verify, RETIRED
from project_split import read_resources, validate, build


def fixture():
    resources={'projects':[], 'datasources':[], 'dashboards':[]}
    for project in RETIRED:
        a3=project=='a3-monitoring'
        names={'a3-overview' if a3 else 'overview':['p0','p1','p6'] if a3 else ['p0','p1'],
               'gateway':['p0','p1','p2','p3','p4','p5','p6','p7'],
               'gateway-generation':['generation-0','live-waiting'],
               'gateway-requests':['extra-first','extra-input'],
               'backend-diagnostics':['extra-request_prefill_time_seconds','extra-request_decode_time_seconds'] if a3 else ['extra-queue-prefill','extra-queue-decode','extra-latency-samples'],
               'a3-hosts' if a3 else 'hosts-xpu' if project=='xpu-monitoring' else 'hosts-dcu':['p0','npu-memory-used' if a3 else 'p5'],
               'monitoring-health':['extra-up']}
        for name,keys in names.items():
            ps={k:panel(k, [('metric{node=~"$node"}', '{{node}}')], 'ms', '指标含义\n耗时\n\nY 轴单位\nms\n\n曲线与范围\n按节点展示；复制 rank 不求和。') for k in keys}
            resources['dashboards'].append({'kind':'Dashboard','metadata':{'name':name,'project':project},'spec':{
                'display':{'name':name},'variables':[variable('node','节点',[{'value':'.*','label':'全部'}]),variable('unused','无用',[{'value':'.*','label':'全部'}])],
                'panels':ps,'layouts':[{'kind':'Grid','spec':{'items':[{'x':0,'y':i*8,'width':12,'height':8,'content':{'$ref':'#/spec/panels/'+k}} for i,k in enumerate(keys)]}}]}})
    return resources


def test_migration_accounts_for_every_query_and_removes_only_retired_indices():
    before=fixture();original=copy.deepcopy(before)
    after,manifest=migrate(before)
    assert verify(before,after,manifest)['retired']==6
    assert before==original
    assert migrate(after)[0]==after
    for d in after['dashboards']:
        assert d['metadata']['name'] not in RETIRED[d['metadata']['project']]
        assert [v['spec']['name'] for v in d['spec']['variables']]==['node']
        for p in d['spec']['panels'].values():
            assert p['spec']['display']['name'].endswith('（ms）')
            assert '复制 rank 不求和' in p['spec']['display']['description']
            assert '曲线与范围' not in p['spec']['display']['description']
            assert 'label' not in p['spec']['plugin']['spec']['yAxis']


def test_changed_query_or_missing_manifest_is_rejected():
    before=fixture();after,manifest=migrate(before)
    with pytest.raises(AssertionError):verify(before,after,manifest[:-1])
    after['dashboards'][0]['spec']['panels'][next(iter(after['dashboards'][0]['spec']['panels']))]['spec']['queries'][0]['spec']['plugin']['spec']['query']='vector(0)'
    with pytest.raises(AssertionError):verify(before,after,manifest)


def test_current_resources_are_valid_and_regenerate_without_overview():
    current=read_resources(Path(__file__).parent/'projects')
    validate(current)
    by_id=lambda r: {(d['metadata']['project'],d['metadata']['name']):d for d in r['dashboards']}
    assert by_id(build(current))==by_id(current)
    for d in current['dashboards']:
        if d['metadata']['name']=='gateway-requests':
            assert [l['spec']['display']['title'] for l in d['spec']['layouts']]==['流量与结果','延迟','Token 与 Usage']
        if d['metadata']['name']=='monitoring-health':
            assert d['spec']['layouts'][0]['spec']['display']['title']=='项目采集链路'


def test_rollback_restores_deleted_dashboard_and_preserves_concurrent_recreation(tmp_path):
    import project_release as r
    old=fixture()['dashboards'][0]
    (tmp_path/'journal.json').write_text(json.dumps([{'action':'delete','before':old}]))
    calls=[]
    def missing(url,method='GET',data=None):
        if method=='GET':raise RuntimeError('HTTP 404')
        calls.append((method,data))
    with patch.object(r,'http',missing):r.rollback(tmp_path)
    assert calls[0][0]=='POST' and calls[0][1]['spec']==old['spec']
    edited=copy.deepcopy(old);edited['spec']['display']['name']='用户修改'
    with patch.object(r,'http',return_value=edited),pytest.raises(AssertionError):r.rollback(tmp_path)


def test_runtime_install_and_rollback_detect_concurrent_changes(tmp_path):
    import reorg_runtime as runtime
    evidence=tmp_path/'evidence';evidence.mkdir()
    release=evidence/'release';release.mkdir()
    target=tmp_path/'runtime';target.mkdir()
    for name in runtime.MODULES:
        (release/name).write_text('new')
    (target/'project_split.py').write_text('old')
    old=target/'projects/dcu-monitoring/dashboards/overview.json'
    old.parent.mkdir(parents=True);old.write_text('old overview')
    runtime.sync(evidence,target)
    assert not old.exists()
    assert (target/'project_split.py').read_text()=='new'
    (target/'project_split.py').write_text('concurrent')
    with pytest.raises(AssertionError):runtime.sync(evidence,target,True)
    (target/'project_split.py').write_text('new')
    runtime.sync(evidence,target,True)
    assert old.read_text()=='old overview'
    assert (target/'project_split.py').read_text()=='old'
    assert not (target/'dashboard_reorg.py').exists()
