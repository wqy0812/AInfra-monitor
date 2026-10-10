"""Freeze the eighteen original panel expressions and their supported filters."""
import argparse
import hashlib
import json
import re
from pathlib import Path
from dashboard_columns import panel_index


def targets():
    result = []
    for project, dashboard in [('dcu-monitoring', 'hosts-dcu'), ('xpu-monitoring', 'hosts-xpu')]:
        for panel in ('core-p0', 'core-extra-iowait'):
            result.append((project, dashboard, panel, 'cpu'))
    for role in ('prefill', 'decode'):
        for metric in ('per_stage_req_latency_seconds', 'queue_time_seconds'):
            result.append(('dcu-monitoring', 'backend-' + role, 'bn-' + role + '-' + metric, 'dcu'))
    for metric in ('kv_transfer_bootstrap_ms', 'kv_transfer_alloc_ms'):
        result.append(('dcu-monitoring', 'backend-decode', 'bn-decode-' + metric, 'dcu'))
    for metric in ('request_inference_time_seconds', 'request_time_per_output_token_seconds'):
        result.append(('a3-monitoring', 'backend-performance', 'extra-' + metric, 'a3'))
    for role in ('prefill', 'decode'):
        for metric in ('request_queue_time_seconds', 'request_prefill_time_seconds', 'request_decode_time_seconds'):
            result.append(('a3-monitoring', 'backend-' + role, 'extra-' + metric, 'a3'))
    return result


def build(resources, steps=(5, 15, 60)):
    panels = []
    indexed = panel_index(resources)
    for project, dashboard, key, group in targets():
        document, public_key = indexed[(project, dashboard, key)]
        panel = document['spec']['panels'][public_key]['spec']
        assert len(panel['queries']) == 1
        expression = panel['queries'][0]['spec']['plugin']['spec']['query']
        variables = {}
        for name in set(re.findall(r'\$([A-Za-z_]\w*)', expression)) - {'__interval'}:
            assert name in ('role', 'node')
            # All affected aggregations retain node. Only these finite, existing
            # node filters commute with the frozen per-node computation.
            assert '$' + name not in expression.replace('node=~"$' + name + '"', '')
            variable = next(v['spec'] for v in document['spec']['variables'] if v['spec']['name'] == name)
            assert variable['plugin']['kind'] == 'StaticListVariable' and variable['defaultValue'] == '.*'
            variables[name] = [v['value'] for v in variable['plugin']['spec']['values']]
        entry = {'id': project + '/' + dashboard + '/' + key, 'group': group,
                 'project': project, 'dashboard': dashboard, 'panel': key,
                 'title': panel['display']['name'], 'expression': expression, 'variables': variables}
        entry['revision'] = hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()
        panels.append(entry)
    return {'schema': 1, 'steps': sorted(set(steps)), 'groups': ['cpu', 'dcu', 'a3'], 'panels': panels}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('snapshot', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--steps', help='Initial catalog steps (default 5,15,60)')
    parser.add_argument('--previous-catalog', type=Path, help='Replace only A3 histogram revisions, retaining existing steps and other entries')
    args = parser.parse_args()
    if args.previous_catalog:
        assert args.steps is None, 'Revision replacement must retain the existing steps'
        from query_slimming import replace_catalog
        result, _, _ = replace_catalog(json.loads(args.previous_catalog.read_text()), json.loads(args.snapshot.read_text()))
    else:
        result = build(json.loads(args.snapshot.read_text()), [int(s) for s in (args.steps or '5,15,60').split(',')])
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
