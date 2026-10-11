"""Issue #4 experiment: generate native vmalert rules, without publishing them.

JSON is valid YAML and is accepted by vmalert's rule loader. No production
datasource, dashboard, acceleration state, or deployment file is changed.
"""
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / 'monitoring/perses_acceleration_catalog.json'


def expression(panel, step):
    value = panel['expression'].replace('$__interval', f'{step}s')
    for variable in panel['variables']:
        value = value.replace('$' + variable, '.*')
    if '$' in value:
        raise ValueError('Unresolved template variable')
    return value


def record_name(panel, step):
    identity = panel['id'] + ':' + panel['revision']
    return 'monitoring_rr_trial_' + hashlib.sha256(identity.encode()).hexdigest()[:24] + f'_{step}s'


def rules(catalog):
    return {'groups': [{
        'name': f'issue4_{step}s', 'interval': f'{step}s',
        'eval_delay': '60s', 'eval_alignment': True, 'concurrency': 1,
        # The default instant-query step is 5m, independently of interval.
        # Explicitly retain the original query's step and freshness semantics.
        'params': {'step': [f'{step}s'], 'nocache': ['1'], 'latency_offset': ['1ms']},
        'rules': [{'record': record_name(panel, step), 'expr': expression(panel, step)}
                  for panel in catalog['panels']],
    } for step in catalog['steps']]}


def selector(panel, step, *, exact=False, bindings=None):
    selected = record_name(panel, step)
    if bindings:
        selected += '{' + ','.join('node=~' + json.dumps(value) for value in bindings.values()) + '}'
    if exact:
        selected += ' and (timestamp(' + selected + ') == time())'
    return 'label_del((' + selected + '), "__name__")'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--catalog', type=Path, default=CATALOG)
    args = parser.parse_args()
    args.output.write_text(json.dumps(rules(json.loads(args.catalog.read_text())), indent=2) + '\n')
