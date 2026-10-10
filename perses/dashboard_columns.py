"""Publish section sources as two dashboards without changing panel semantics.

The source files keep their stable identities for query builders and admitted
acceleration catalogs. Only this publication projection changes navigation.
"""
import copy
import re

PROJECTS = ('dcu-monitoring', 'a3-monitoring', 'xpu-monitoring')
COLUMNS = {
    'gateway-monitoring': ('网关监控', (
        ('gateway-requests', '请求流量与质量'),
        ('gateway-generation', '在途请求诊断'),
    )),
    'model-monitoring': ('模型推理监控', (
        ('backend-performance', '后端请求性能'),
        ('cache-store', '缓存与存储'),
        ('accelerator-resources', '加速卡资源'),
    )),
}
CONTENT = {'display', 'panels', 'layouts', 'variables'}
SEPARATOR = '--'
NODES = {'dcu-monitoring': ('dcu1', 'dcu2'), 'a3-monitoring': ('a3-1', 'a3-2'), 'xpu-monitoring': ('xpu-2', 'xpu-1')}


def normalize_variable(variable, project):
    value = copy.deepcopy(variable)
    spec = value['spec']
    spec.setdefault('allowAllValue', False)
    spec.setdefault('allowMultiple', False)
    spec.setdefault('display', {}).setdefault('hidden', False)
    if spec['name'] == 'role':
        # Cache paths use prefill/decode; hardware labels use node names.
        # The shared selector must match both without rewriting any query.
        assert spec['plugin']['kind'] == 'StaticListVariable'
        options = spec['plugin']['spec']['values']
        canonical = ['.*'] + ['(' + role + '|' + node + ')' for role, node in zip(('prefill', 'decode'), NODES[project])]
        assert [item['value'] for item in options] in (['.*', 'prefill', 'decode'], canonical), 'Unknown role selector'
        spec['plugin']['spec']['values'] = [dict(value=v, label=label) for v, label in zip(canonical, ('全部', 'Prefill', 'Decode'))]
        spec['defaultValue'] = dict(zip(('prefill', 'decode'), canonical[1:])).get(spec.get('defaultValue'), spec.get('defaultValue'))
    return value


def sections(project, column):
    return tuple(('a3-cache' if project == 'a3-monitoring' and name == 'cache-store' else name, title)
                 for name, title in COLUMNS[column][1])


def retired(project):
    return tuple(name for column in COLUMNS for name, _ in sections(project, column))


def origin(column, key):
    return key.split(SEPARATOR, 1) if column in COLUMNS else (column, key)


def source_identity(project, dashboard, panel):
    """Keep catalog/state identities stable while changes use public resource keys."""
    if project in PROJECTS and dashboard in COLUMNS:
        source, separator, key = panel.partition(SEPARATOR)
        if not separator or not key or source not in dict(sections(project, dashboard)):
            raise ValueError('Unknown column panel: ' + repr((project, dashboard, panel)))
        return project, source, key
    return project, dashboard, panel


def panel_index(resources):
    """Resolve logical panel identities to the exact document and writable key."""
    result = {}
    for document in resources['dashboards']:
        project, dashboard = document['metadata']['project'], document['metadata']['name']
        for key in document['spec']['panels']:
            identity = source_identity(project, dashboard, key)
            if identity in result:
                raise ValueError('Ambiguous source/public panel: ' + repr(identity))
            result[identity] = document, key
    return result


def section_panels(resources, project, source):
    matches = [(document, key) for identity, (document, key) in panel_index(resources).items()
               if identity[:2] == (project, source)]
    if not matches:
        raise ValueError('Missing section: ' + repr((project, source)))
    return matches


def source_change(change):
    project, dashboard, panel = source_identity(change['project'], change['dashboard'], change['panel'])
    return dict(change, project=project, dashboard=dashboard, panel=panel)


def apply(resources):
    result = copy.deepcopy(resources)
    dashboards = result['dashboards']
    for project in PROJECTS:
        owned = {d['metadata']['name']: d for d in dashboards if d['metadata']['project'] == project}
        if not owned:
            continue
        for column, (title, _) in COLUMNS.items():
            members = sections(project, column)
            if column in owned:
                assert not any(name in owned for name, _ in members), (project, column, 'mixed column/source state')
                continue
            assert all(name in owned for name, _ in members), (project, column, 'missing section source')
            first = owned[members[0][0]]
            settings = {k: v for k, v in first['spec'].items() if k not in CONTENT}
            d = {'kind': 'Dashboard', 'metadata': {'project': project, 'name': column}, 'spec': {
                **copy.deepcopy(settings), 'display': {'name': title,
                    'description': '按分区查看：' + '、'.join(label for _, label in members) + '。分区可折叠；保留原图表查询与统计口径。'},
                'variables': [], 'panels': {}, 'layouts': []}}
            variables = {}
            for name, label in members:
                source = owned[name]['spec']
                assert settings == {k: v for k, v in source.items() if k not in CONTENT}, (project, column, 'incompatible dashboard settings')
                for variable in source.get('variables', []):
                    variable = normalize_variable(variable, project)
                    key = variable['spec']['name']
                    assert key not in variables or variables[key] == variable, (project, column, key, 'conflicting variable')
                    variables[key] = copy.deepcopy(variable)
                prefix = name + SEPARATOR
                d['spec']['panels'].update({prefix + key: copy.deepcopy(panel) for key, panel in source['panels'].items()})
                for layout in source['layouts']:
                    row = copy.deepcopy(layout)
                    display = row['spec'].setdefault('display', {})
                    subtitle = display.get('title', '')
                    display['title'] = label + (' · ' + subtitle if subtitle else '')
                    for item in row['spec']['items']:
                        key = item['content']['$ref'].rsplit('/', 1)[1]
                        item['content']['$ref'] = '#/spec/panels/' + prefix + key
                    d['spec']['layouts'].append(row)
            d['spec']['variables'] = list(variables.values())
            names = {name for name, _ in members}
            dashboards[:] = [old for old in dashboards if not (old['metadata']['project'] == project and old['metadata']['name'] in names)]
            dashboards.append(d)
    # Summary hints refer to the new public navigation, not the section source files.
    for d in dashboards:
        if d['metadata']['project'] in PROJECTS and d['metadata']['name'] == 'summary':
            for panel in d['spec']['panels'].values():
                display = panel['spec']['display']
                for old, new in (('「网关请求流量与质量」', '「网关监控」的请求流量与质量分区'),
                                 ('「网关在途请求诊断」', '「网关监控」的在途请求诊断分区'),
                                 ('「后端请求性能」', '「模型推理监控」的后端请求性能分区'),
                                 ('「缓存与存储」', '「模型推理监控」的缓存与存储分区'),
                                 ('「加速卡资源」', '「模型推理监控」的加速卡资源分区')):
                    display['description'] = display.get('description', '').replace(old, new)
    return result


def expand(resources, templates):
    """Recover editable section sources when regenerating from a live snapshot."""
    result = copy.deepcopy(resources)
    by_id = {(d['metadata']['project'], d['metadata']['name']): d for d in templates['dashboards']}
    expanded = []
    for document in result['dashboards']:
        project, column = document['metadata']['project'], document['metadata']['name']
        if project not in PROJECTS or column not in COLUMNS:
            expanded.append(document)
            continue
        spec = document['spec']
        accounted = set()
        for name, label in sections(project, column):
            assert not any(d['metadata']['project'] == project and d['metadata']['name'] == name for d in result['dashboards']), 'Mixed column/source state'
            d = copy.deepcopy(by_id[(project, name)])
            d['metadata'] = {'project': project, 'name': name}
            d['spec'].update({k: copy.deepcopy(v) for k, v in spec.items() if k not in CONTENT})
            prefix = name + SEPARATOR
            keys = {key for key in spec['panels'] if key.startswith(prefix)}
            accounted.update(keys)
            d['spec']['panels'] = {key[len(prefix):]: copy.deepcopy(spec['panels'][key]) for key in spec['panels'] if key in keys}
            d['spec']['layouts'] = []
            for layout in spec['layouts']:
                refs = {item['content']['$ref'].rsplit('/', 1)[1] for item in layout['spec']['items']}
                if not refs & keys:
                    continue
                assert refs <= keys, 'A layout spans multiple section sources'
                row = copy.deepcopy(layout)
                display = row['spec'].get('display', {})
                title = display.get('title', '')
                if title == label:
                    display.pop('title')
                elif title.startswith(label + ' · '):
                    display['title'] = title[len(label + ' · '):]
                if not display:
                    row['spec'].pop('display', None)
                for item in row['spec']['items']:
                    key = item['content']['$ref'].rsplit('/', 1)[1]
                    item['content']['$ref'] = '#/spec/panels/' + key[len(prefix):]
                d['spec']['layouts'].append(row)
            used = set(re.findall(r'\$([A-Za-z_]\w*)', str(d['spec']['panels']))) - {'__interval'}
            used |= {v['spec']['name'] for v in d['spec'].get('variables', [])}
            d['spec']['variables'] = [copy.deepcopy(v) for v in spec.get('variables', []) if v['spec']['name'] in used]
            expanded.append(d)
        assert accounted == set(spec['panels']), 'Unknown section panel'
    result['dashboards'] = expanded
    return result
