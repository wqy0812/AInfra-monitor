"""XPU role mapping verified from live launch arguments on 2026-09-29."""
import copy
import re

NODES = {'prefill': 'xpu-2', 'decode': 'xpu-1'}
IPS = {'prefill': '122.209.21.34', 'decode': '122.209.21.33'}


def configure(document):
    """Normalize role-dependent fields without rewriting physical gateway nodes."""
    d = copy.deepcopy(document)
    if d['metadata'].get('project') != 'xpu-monitoring':
        return d
    name = d['metadata']['name']
    for item in d['spec'].get('variables', []):
        v = item['spec']
        if v['name'] not in ('role', 'node'):
            continue
        for value in v['plugin']['spec']['values']:
            role = value['label'].split(' / ')[0].lower()
            if role in NODES:
                value['value'] = '(' + role + '|' + NODES[role] + ')' if v['name'] == 'role' else NODES[role]
                if v['name'] == 'node':
                    value['label'] = role.capitalize() + ' / ' + NODES[role]
    for panel in d['spec']['panels'].values():
        for item in panel['spec'].get('queries', []):
            q = item['spec']['plugin']['spec']
            if name in ('backend-prefill', 'backend-decode'):
                role = name[len('backend-'):]
                q['query'] = re.sub(r'node=(~?)"xpu-[12]"', lambda m: 'node=' + m[1] + '"' + NODES[role] + '"', q['query'])
            elif name == 'cache-store':
                q['query'] = re.sub(r'instance="122\.209\.21\.(33|34):8501"', 'instance="' + IPS['prefill'] + ':8501"', q['query'])
    return d


def scrape(content):
    """Change only six role-bearing XPU targets; retain all other config bytes."""
    pattern = r"(  - targets: \[')(122\.209\.21\.(?:33|34))(:\d+'\]\n    labels: \{environment: xpu-pd, role: )(prefill|decode)(, node: )(xpu-[12])(, service: ([\w-]+)\})"
    seen = []
    def replace(m):
        ip, role, node, service = m[2], m[4], m[6], m[8]
        if service == 'sglang':
            ip, node = IPS[role], NODES[role]
        else:
            role = next(r for r in NODES if NODES[r] == node)
            assert ip == IPS[role]
        seen.append((role, service))
        return m[1] + ip + m[3] + role + m[5] + node + m[7]
    updated = re.sub(pattern, replace, content)
    assert sorted(seen) == sorted((r, s) for r in NODES for s in ('sglang', 'host', 'xpu-exporter')), seen
    return updated
