# Project-aware CLI routing; legacy helpers below remain importable.
if __name__ == "__main__":
    raise SystemExit("此历史发布入口已退役。使用 project_release.py prepare/audit/apply --evidence DIR；资源按 project/name 定位。")
    raise SystemExit(0)

"""Migrate the two gateway dashboards to streaming scope, preserving unrelated edits."""
import copy
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from gateway_live import extend_dashboard
from metric_scope import apply_scope
from publish_gateway_live import API, SOURCES, get, save, protected

NAMES = ('gateway-generation', 'gateway')
SELECTOR = re.compile(r'(aigate_[a-zA-Z0-9_]+)\{([^{}]*)\}')


def scope_query(query):
    scoped = apply_scope(query)
    # Exported metrics carry request_scope; scrape-generated up does not.
    # Match source identity explicitly or the lifecycle gate is always empty.
    for function in ('min_over_time', 'count_over_time'):
        scoped = scoped.replace('and (' + function + '(up{job="aigate"}[1m])',
                                'and on(job,instance) (' + function + '(up{job="aigate"}[1m])')
    return scoped


def migrate(document):
    result = extend_dashboard(document) if document['metadata']['name'] == 'gateway-generation' else copy.deepcopy(document)
    for key, panel in result['spec']['panels'].items():
        changed = False
        for q in panel['spec']['queries']:
            spec = q['spec']['plugin']['spec']
            old = spec['query']
            spec['query'] = scope_query(old)
            changed |= old != spec['query']
        if changed:
            display = panel['spec'].setdefault('display', {})
            description = display.get('description', '')
            description = description.replace('全部流式生成结束请求', '全部生成结束请求')
            display['description'] = '按指标含义区分统计范围。' + description
    return result


def main():
    root = Path(sys.argv[1])
    assert root.is_dir()
    assert not (root / 'scope-before.json').exists(), 'Use a fresh evidence directory'
    for file in ('live-semantics.json', 'semantics.json'):
        assert json.loads((root / file).read_text())['passed']
    before = get(API)
    services = protected()
    old = {name: next(d for d in before if d['metadata']['name'] == name) for name in NAMES}
    proposed = {name: migrate(d) for name, d in old.items()}
    save(root, 'scope-before.json', before)
    save(root, 'scope-proposed.json', proposed)
    save(root, 'services-before.json', services)
    # Require the exact new scopes from both live exporters, never accept old metrics.
    for env in ('dcu-pd', 'a3-vllm'):
        for metric, scope in [('aigate_live_backend_groups', 'streaming'), ('aigate_nonstream_requests_total', 'nonstreaming')]:
            s = metric + '{job="aigate",environment="' + env + '",request_scope="' + scope + '"}'
            q = s + ' and (time() - timestamp(' + s + ') < 15)'
            rows = get(SOURCES[0] + '/api/v1/query?' + urllib.parse.urlencode({'query': q}))['data']['result']
            assert len(rows) == 1, (env, metric, rows)
    end = int(time.time() // 5) * 5 - 90
    checks = []
    for name, document in proposed.items():
        for key, panel in document['spec']['panels'].items():
            for i, query in enumerate(panel['spec']['queries']):
                for step in (15, 60):
                    params = {'query': query['spec']['plugin']['spec']['query'].replace('$__interval', f'{step}s'), 'start': end - 600, 'end': end, 'step': step, 'nocache': '1'}
                    answers = []
                    for base in SOURCES:
                        req = urllib.request.Request(base + '/api/v1/query_range', data=urllib.parse.urlencode(params).encode())
                        answer = json.load(urllib.request.urlopen(req, timeout=30))
                        assert answer['status'] == 'success', answer
                        answers.append(answer['data'])
                    assert answers[0] == answers[1], (name, key, i, step)
                    checks.append({'dashboard': name, 'panel': key, 'query': i, 'step': step, 'series': len(answers[0]['result'])})
    for key in ('p1', 'p5', 'p6'):
        assert any(c['dashboard'] == 'gateway' and c['panel'] == key and c['series'] > 0 for c in checks), ('Expected measured rate series', key)
    save(root, 'query-checks.json', checks)
    assert get(API) == before, 'Concurrent edit detected; refusing publication'
    for name, document in proposed.items():
        assert get(API + '/' + name) == old[name], 'Concurrent edit detected'
        if document != old[name]:
            req = urllib.request.Request(API + '/' + name, data=json.dumps(document).encode(), headers={'Content-Type': 'application/json'}, method='PUT')
            urllib.request.urlopen(req, timeout=30).close()
        actual = get(API + '/' + name)
        assert actual['spec'] == document['spec'], name
        save(root, name + '-after.json', actual)
    after = get(API)
    assert len(after) == len(before)
    for document in before:
        name = document['metadata']['name']
        if name not in NAMES:
            assert next(d for d in after if d['metadata']['name'] == name) == document
    assert protected() == services
    report = {'passed': True, 'panels': {n: len(d['spec']['panels']) for n, d in proposed.items()}, 'query_checks': len(checks), 'other_dashboards_preserved': True, 'services_preserved': True, 'time': time.time()}
    save(root, 'scope-publication.json', report)
    print(json.dumps(report))


if __name__ == '__main__':
    main()
