"""Retire percentage panels that repeat an absolute-value panel on the same dashboard.

Operators read the value (used GiB, errors/s, Token capacity); the ratio twin is
removed. Kept panels, their queries and raw collection are unchanged. Cache hit
rates have no same-scope value twin and stay.
"""
import copy

HOSTS = {'dcu-monitoring': 'hosts-dcu', 'a3-monitoring': 'a3-hosts', 'xpu-monitoring': 'hosts-xpu'}

# (project, dashboard) -> {retired ratio panel: value panel moved into its slot, or None}
RETIRED = {}
for _project, _hosts in HOSTS.items():
    RETIRED[(_project, _hosts)] = {'core-extra-memory-ratio': None, 'core-p2': 'core-extra-fs-free'}
    RETIRED[(_project, 'accelerator-resources')] = {'core-memory-ratio': None}
    RETIRED[(_project, 'gateway-requests')] = {'generation-2': None}
for _role in ('prefill', 'decode'):
    # DCU core-kv and bn-*-kv-usage both plot sglang:token_usage; the capacity panel holds the Token values.
    RETIRED[('dcu-monitoring', 'backend-' + _role)] = {
        'core-kv': 'bn-' + _role + '-kv-capacity', 'bn-' + _role + '-kv-usage': None, 'bn-' + _role + '-swa-usage': None}
RETIRED[('dcu-monitoring', 'cache-store')] = {'p2': None, 'extra-segment-ratio': None}


def retire(document):
    retired = RETIRED.get((document['metadata'].get('project'), document['metadata']['name']), {})
    if not retired.keys() & document['spec']['panels'].keys():
        return document
    d = copy.deepcopy(document)
    spec = d['spec']
    groups = [[item['content']['$ref'].rsplit('/', 1)[1]
               for item in sorted(layout['spec']['items'], key=lambda x: (x['y'], x['x']))]
              for layout in spec['layouts']]
    for key, keeper in retired.items():
        if key not in spec['panels']:
            continue
        del spec['panels'][key]
        if keeper in spec['panels']:
            for keys in groups:
                if keeper in keys:
                    keys.remove(keeper)
        for keys in groups:
            if key in keys:
                index = keys.index(key)
                keys[index:index + 1] = [keeper] if keeper in spec['panels'] else []
    layouts = []
    for layout, keys in zip(spec['layouts'], groups):
        if not keys:
            continue
        top = min(item['y'] for item in layout['spec']['items'])
        layout['spec']['items'] = [{'x': i % 2 * 12, 'y': top + i // 2 * 8, 'width': 12, 'height': 8,
                                    'content': {'$ref': '#/spec/panels/' + key}} for i, key in enumerate(keys)]
        layouts.append(layout)
    spec['layouts'] = layouts
    return d


def apply(resources):
    result = dict(resources)
    result['dashboards'] = [retire(d) for d in resources['dashboards']]
    return result
