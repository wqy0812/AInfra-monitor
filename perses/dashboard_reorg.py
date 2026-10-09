"""Lossless, idempotent dashboard migration; no network access on import."""
import argparse
import copy
import json
import re
from pathlib import Path

PROJECTS = ('dcu-monitoring', 'a3-monitoring', 'xpu-monitoring')
RETIRED = {'dcu-monitoring': ('overview', 'backend-diagnostics'),
           'a3-monitoring': ('a3-overview',),
           'xpu-monitoring': ('overview', 'backend-diagnostics')}
TITLES = {'gateway': '网关画像存储健康', 'gateway-requests': '网关请求流量与质量',
          'gateway-generation': '网关在途请求诊断', 'backend-performance': '后端请求性能',
          'backend-prefill': 'Prefill 诊断', 'backend-decode': 'Decode 诊断',
          'backend-diagnostics': '后端引擎诊断', 'monitoring-health': '采集与监控健康'}


def expressions(panel):
    return [q['spec']['plugin']['spec']['query'] for q in panel['spec'].get('queries', [])]


def present(panel, name):
    s = panel['spec']; display = s['display']
    axis = s['plugin']['spec'].get('yAxis', {})
    unit = axis.pop('label', '').strip()
    unit = {'req/s': '请求/秒', 'token/s': 'Token/秒', 's': '秒', '毫秒': 'ms'}.get(unit, unit)
    unit = re.sub(r'\s*/\s*', '/', unit)
    if unit and not display['name'].endswith('（' + unit + '）'):
        display['name'] += '（' + unit + '）'
    if name in ('gateway-requests', 'backend-performance'):
        prefix = '网关' if name == 'gateway-requests' else '后端'
        if not display['name'].startswith(prefix):
            display['name'] = prefix + ' · ' + display['name']
    parts = []
    for part in re.split(r'\n\s*\n', display.get('description', '').strip()):
        heading, _, body = part.partition('\n')
        heading = heading.strip().strip('*').strip()
        if heading == 'Y 轴单位':
            continue
        if heading == '曲线与范围':
            # Preserve population/duplication caveats without the removed heading.
            if body.strip(): parts.append(body.strip())
        elif part.strip():
            parts.append(part.strip())
    from metric_scope import request_description
    display['description'] = request_description('\n\n'.join(parts))
    for q in s.get('queries', []):
        spec = q['spec']['plugin']['spec']
        if 'seriesNameFormat' in spec:
            spec['seriesNameFormat'] = spec['seriesNameFormat'].replace('读取流式请求的错误响应', '读取完整响应')


def ordered(d):
    return [item['content']['$ref'].split('/')[-1]
            for layout in d['spec']['layouts']
            for item in sorted(layout['spec']['items'], key=lambda x: (x['y'], x['x']))]


def section(name, key):
    if name == 'gateway-requests':
        if key in ('extra-first', 'extra-duration'): return '延迟'
        if key in ('extra-tokens', 'extra-input', 'extra-output', 'extra-cache', 'extra-usage'): return 'Token 与 Usage'
        return '流量与结果'
    if name == 'monitoring-health': return '项目采集链路' if key.startswith('overview-') else '共享 VM / vmagent 服务'
    if name == 'backend-diagnostics':
        if key == 'extra-request_prefill_time_seconds': return 'Prefill'
        if key == 'extra-request_decode_time_seconds': return 'Decode'
        return '公共调度与性能'
    return ''


def migrate(resources):
    out = {k: copy.deepcopy(v) for k, v in resources.items() if k != 'dashboards'}
    out['dashboards'] = []; manifest = []
    for project in PROJECTS:
        sources = [d for d in resources['dashboards'] if d['metadata']['project'] == project]
        if not sources: continue
        if any(d['metadata']['name'] == 'backend-performance' for d in sources):
            assert not any(d['metadata']['name'] in RETIRED[project] for d in sources), 'Mixed migration state'
            out['dashboards'].extend(copy.deepcopy(sources))
            continue
        destinations = {}; variable_sets = {}
        for source in sources:
            old = source['metadata']['name']
            for key in ordered(source):
                panel = source['spec']['panels'][key]
                target, newkey = old, key
                if old == 'gateway' and key in ('p3', 'p4'):
                    manifest.append(dict(project=project, source=old, panel=key, action='retire', reason='画像索引指标已从源程序移除', targets=[]))
                    continue
                if old in ('overview', 'a3-overview'):
                    if key == 'p0' or key.startswith('extra-'):
                        target = 'monitoring-health'
                    elif old == 'a3-overview' and key == 'p6':
                        target = 'backend-diagnostics'
                    else: target = 'backend-performance'
                    newkey = 'overview-' + key
                elif old == 'gateway':
                    if key == 'p0': target, newkey = 'gateway-generation', 'gateway-inflight'
                    if key == 'p1': target, newkey = 'gateway-requests', 'gateway-arrivals'
                elif old == 'gateway-generation' and key.startswith('generation-'):
                    target = 'gateway-requests'
                elif old == 'backend-diagnostics' and project != 'a3-monitoring':
                    if key == 'extra-latency-samples': target = 'backend-performance'
                    elif 'prefill' in key: target = 'backend-prefill'
                    elif 'decode' in key: target = 'backend-decode'
                    else: raise ValueError('Unclassified backend panel: ' + key)
                elif old in ('hosts-dcu', 'hosts-xpu', 'a3-hosts'):
                    if key.startswith(('npu-', 'extra-vram-')) or (old != 'a3-hosts' and key in ('p5','p6','p7','p8')):
                        target = 'accelerator-resources'
                if target not in destinations:
                    d = copy.deepcopy(source)
                    d['metadata'] = {'name': target, 'project': project}
                    d['spec']['panels'] = {}; d['spec']['layouts'] = []
                    d['spec']['variables'] = []
                    title = TITLES.get(target)
                    if target.startswith('hosts-') or target == 'a3-hosts': title = '主机资源 · Node Exporter'
                    if target in ('cache-store', 'a3-cache'): title = '缓存与存储'
                    if target == 'accelerator-resources': title = '加速卡资源 · ' + {'dcu-monitoring':'DCU','a3-monitoring':'NPU','xpu-monitoring':'XPU'}[project] + ' Exporter'
                    d['spec']['display'] = {'name': title or source['spec']['display']['name'],
                        'description': '按指标观测位置分类；单位见图表标题，详细统计含义见图表提示。'}
                    destinations[target] = d; variable_sets[target] = {}
                d = destinations[target]
                assert newkey not in d['spec']['panels'], (project, target, newkey)
                d['spec']['panels'][newkey] = copy.deepcopy(panel)
                for var in source['spec'].get('variables', []):
                    n = var['spec']['name']
                    if n in variable_sets[target]: assert variable_sets[target][n] == var, (project,target,n)
                    variable_sets[target][n] = copy.deepcopy(var)
                manifest.append(dict(project=project, source=old, panel=key,
                    action='move' if old != target else 'retain', reason='按观测位置与职责分类',
                    targets=[{'dashboard':target,'panel':newkey}]))
        for name, d in destinations.items():
            querytext = '\n'.join(q for p in d['spec']['panels'].values() for q in expressions(p))
            used = set(re.findall(r'\$([A-Za-z_]\w*)', querytext)) - {'__interval'}
            assert used <= variable_sets[name].keys(), (project,name,used)
            d['spec']['variables'] = [v for n,v in variable_sets[name].items() if n in used]
            groups = {}
            for key, p in d['spec']['panels'].items():
                present(p, name)
                groups.setdefault(section(name, key), []).append(key)
            if name == 'gateway-requests': group_order = ['流量与结果','延迟','Token 与 Usage']
            elif name == 'monitoring-health': group_order = ['项目采集链路','共享 VM / vmagent 服务']
            elif name == 'backend-diagnostics': group_order = ['公共调度与性能','Prefill','Decode']
            else: group_order = list(groups)
            for group in group_order:
                keys = groups.get(group, [])
                if not keys: continue
                items = [{'x':i%2*12,'y':i//2*8,'width':12,'height':8,'content':{'$ref':'#/spec/panels/'+k}} for i,k in enumerate(keys)]
                d['spec']['layouts'].append({'kind':'Grid','spec':{'items':items, **({'display':{'title':group}} if group else {})}})
            out['dashboards'].append(d)
    out['dashboards'].extend(copy.deepcopy(d) for d in resources['dashboards'] if d['metadata']['project'] not in PROJECTS)
    return out, manifest


def verify(before, after, manifest):
    old = {(d['metadata']['project'],d['metadata']['name'],k):p for d in before['dashboards'] for k,p in d['spec']['panels'].items() if d['metadata']['project'] in PROJECTS}
    new = {(d['metadata']['project'],d['metadata']['name'],k):p for d in after['dashboards'] for k,p in d['spec']['panels'].items() if d['metadata']['project'] in PROJECTS}
    seen = set(); targets = set()
    for m in manifest:
        origin = (m['project'],m['source'],m['panel'])
        assert origin not in seen; seen.add(origin)
        for t in m['targets']:
            dest = (m['project'],t['dashboard'],t['panel'])
            assert dest not in targets; targets.add(dest)
            expected_queries = copy.deepcopy(old[origin]['spec']['queries'])
            for q in expected_queries:
                spec = q['spec']['plugin']['spec']
                if 'seriesNameFormat' in spec:
                    spec['seriesNameFormat'] = spec['seriesNameFormat'].replace('读取流式请求的错误响应', '读取完整响应')
            assert expected_queries == new[dest]['spec']['queries'], (origin,dest)
            plugin = copy.deepcopy(old[origin]['spec']['plugin'])
            plugin['spec'].get('yAxis',{}).pop('label',None)
            assert plugin == new[dest]['spec']['plugin'], (origin,'rendering semantics')
    assert seen == set(old), set(old)-seen
    assert targets == set(new), set(new)-targets
    return {'passed':True,'source_panels':len(old),'target_panels':len(new),'retired':len(old)-len(new),'queries_unchanged':True}


def write_resources(resources, root):
    from acceleration_publication import published
    resources = published(resources)
    for category, documents in resources.items():
        for d in documents:
            project = d['metadata'].get('project',d['metadata']['name'])
            if project not in PROJECTS: continue
            folder = root/project/('dashboards' if category == 'dashboards' else '')
            folder.mkdir(parents=True, exist_ok=True)
            filename = d['metadata']['name'] if category == 'dashboards' else 'project' if category == 'projects' else 'datasource' if d['metadata']['name'] == 'victoriametrics' else d['metadata']['name'] + '-datasource'
            (folder/(filename+'.json')).write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n')
    retired = copy.deepcopy(RETIRED)
    from panel_trim import RETIRED_DASHBOARDS
    retired = {p: names + RETIRED_DASHBOARDS for p, names in retired.items()}
    if any(d['metadata']['project']=='a3-monitoring' and d['metadata']['name']=='backend-prefill' for d in resources['dashboards']):
        retired['a3-monitoring'] += ('backend-diagnostics',)
    for project, names in retired.items():
        for name in names:
            path = root/project/'dashboards'/(name+'.json')
            if path.exists(): path.unlink()


def main():
    p = argparse.ArgumentParser();p.add_argument('--snapshot',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--evidence',type=Path,required=True)
    args=p.parse_args(); before=json.loads(args.snapshot.read_text()); after,manifest=migrate(before)
    from project_split import validate
    validate(after); report=verify(before,after,manifest)
    write_resources(after,args.output)
    for name,data in [('migration.json',manifest),('semantics.json',report),('candidate.json',after)]:
        (args.evidence/name).write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report))

if __name__ == '__main__': main()
