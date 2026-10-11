"""Native recording rules and a read-only adapter with exact publication checks."""
import hashlib
import json
import math
import re

from .perses_acceleration import AccelerationService, SEAL_SECONDS, metric_identity, digest

KIND = 'monitoring_record_kind'
PUBLICATION = 'monitoring_record_publication'
GENERATION = 'monitoring_record_generation'
PUBLICATION_RE = re.compile(r'[0-9a-f]{32}\Z')
GENERATION_RE = re.compile(r'[1-9][0-9]{0,18}\Z')


def record_name(panel, step):
    identity = json.dumps([panel['id'], panel['revision'], panel['expression']], separators=(',', ':'))
    return 'monitoring_perses_native_v2_' + hashlib.sha256(identity.encode()).hexdigest() + f'_{step}s'


def recording_rules(catalog, publication, generation):
    if not PUBLICATION_RE.fullmatch(publication) or not GENERATION_RE.fullmatch(str(generation)):
        raise ValueError('A new publication and positive generation are required for every process')
    if not catalog['panels']:
        raise ValueError('No recording panels')
    groups = []
    for step in catalog['steps']:
        if not isinstance(step, int) or isinstance(step, bool) or step < 5 or step % 5:
            raise ValueError('Unsupported interval')
        rules = []
        for panel in catalog['panels']:
            original = AccelerationService.expression(panel, step)
            if any(label in original for label in (KIND, PUBLICATION, GENERATION)) or '$' in original:
                raise ValueError('Reserved label or unresolved template variable')
            expr = (f'WITH (record_result = ({original})) union('
                    f'label_set(record_result, "{KIND}", "value"), '
                    f'label_set((count(record_result) or vector(0)), "{KIND}", "complete"))')
            rules.append({'record': record_name(panel, step), 'expr': expr,
                          'labels': {PUBLICATION: publication, GENERATION: str(generation)}})
        groups.append({'name': f'perses_native_v2_{step}s', 'interval': f'{step}s',
                       'eval_delay': '60s', 'eval_alignment': True, 'concurrency': 1,
                       'params': {'step': [f'{step}s'], 'nocache': ['1'],
                                  'latency_offset': ['1ms'], 'deny_partial_response': ['1']},
                       'rules': rules})
    return {'groups': groups}


def completed(rows, ticks, name, *, minimum_generation=0):
    """Latest observed generation must be complete; never merge publications."""
    seen = {t: {} for t in ticks}
    for row in rows:
        labels = dict(row['metric'])
        if labels.pop('__name__', None) != name:
            raise ValueError('Unexpected recording metric')
        kind = labels.pop(KIND, None)
        publication = labels.pop(PUBLICATION, '')
        generation = labels.pop(GENERATION, '')
        if (not PUBLICATION_RE.fullmatch(publication) or not GENERATION_RE.fullmatch(generation)
                or kind not in ('value', 'complete')):
            raise ValueError('Malformed recording identity')
        generation = int(generation)
        if kind == 'complete' and labels:
            raise ValueError('Manifest must not contain business labels')
        identity = metric_identity(labels)
        for timestamp, value in row['values']:
            if timestamp not in seen:
                raise ValueError('Unexpected recording timestamp')
            state = seen[timestamp].setdefault(generation, {}).setdefault(publication,
                {'count': None, 'values': {}, 'invalid': False})
            numeric = float(value)
            if not math.isfinite(numeric):
                state['invalid'] = True
            elif kind == 'complete':
                if state['count'] is not None or numeric < 0 or not numeric.is_integer():
                    state['invalid'] = True
                state['count'] = int(numeric)
            elif identity in state['values']:
                state['invalid'] = True
            else:
                state['values'][identity] = str(value)
    result = {}
    for timestamp, generations in seen.items():
        if not generations or max(generations) < minimum_generation:
            continue
        publications = generations[max(generations)]
        if len(publications) != 1:
            continue
        state = next(iter(publications.values()))
        if not state['invalid'] and state['count'] is not None and state['count'] == len(state['values']):
            result[timestamp] = state['values']
    return result


class RecordingService(AccelerationService):
    """All 126 catalog jobs are written by vmalert, never by the API."""
    async def stored(self, panel, step, ticks, *, nocache=True, minimum_generation=0):
        name = record_name(panel, step)
        expr = name + ' and (timestamp(' + name + ') == time())'
        rows = await self.vm_query(expr, ticks[0], ticks[-1], step, nocache=True)
        return completed(rows, ticks, name, minimum_generation=minimum_generation)

    def status(self):
        error = None
        try:
            admin = self.admin()
            path = self.state / 'perses-recording-runtime.json'
            runtime = json.loads(path.read_text()) if path.exists() else None
            if runtime and runtime.get('catalog_sha256') != digest(self.catalog):
                error = 'Native runtime catalog differs from API catalog'
        except (OSError, ValueError) as exc:
            admin, runtime, error = {}, None, type(exc).__name__
        return {'enabled': True, 'backend': 'vmalert', 'protocol': 'native-v2',
                'groups': self.catalog['groups'], 'steps': self.steps, 'seal_seconds': SEAL_SECONDS,
                'rule_count': len(self.panels) * len(self.steps), 'counters': dict(self.counters),
                'disabled_groups': admin.get('disabled_groups'), 'state_error': error,
                'runtime': runtime, 'runtime_fresh': bool(runtime and 0 <= self.now() - runtime.get('updated_at', 0) < 30),
                'coverage': 'verified on each read; replay coverage reported separately'}
