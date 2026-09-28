"""Scoped generator installation, with byte backups and concurrency checks."""
import base64
import copy
import hashlib
import json
from pathlib import Path

RUNTIME = Path('/data2/monitoring/perses/release')
MODULES = ('query_acceleration.py', 'acceleration_catalog.py', 'acceleration_publication.py', 'dashboard_reorg.py')
BASE_HASH = '38b62a8cc6378aecc7837bdfc144f9300d282cf35e0b22bf67bddc4e4f54aba0'


def encoded(path):
    return base64.b64encode(path.read_bytes()).decode() if path.exists() else None


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()


def save(path, content):
    temporary = path.with_suffix('.tmp'); temporary.write_bytes(content); temporary.replace(path)


def plan(evidence, changes, group=None, datasources=()):
    assert not (evidence / 'generator-journal.json').exists()
    assert RUNTIME.is_dir()
    source = Path(__file__).resolve().parents[2] / 'perses'
    state_path = RUNTIME / 'acceleration_state.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {'schema': 1, 'merges': [], 'groups': []}
    selected = [(c['project'], c['dashboard'], c['panel']) for c in changes]
    if group is None:
        assert not set(selected) & {tuple(x) for x in state['merges']}
        state['merges'] = sorted({tuple(x) for x in state['merges']} | set(selected))
    else:
        assert group in ('cpu', 'dcu', 'a3') and group not in state['groups']
        state['groups'] = sorted(state['groups'] + [group])
    writes = {}
    for name in MODULES:
        target = RUNTIME / name
        candidate = (source / name).read_bytes()
        if name == 'dashboard_reorg.py':
            assert target.read_bytes() == candidate or hashlib.sha256(target.read_bytes()).hexdigest() == BASE_HASH, 'Generator changed concurrently'
        else:
            assert not target.exists() or target.read_bytes() == candidate, 'Unexpected existing generator module: ' + name
        writes[name] = candidate
    # The generated guide no longer contains the independently maintained note.
    # Install that document beside the runtime guides and preserve their history.
    document = 'perses-query-acceleration.md'
    note = (source.parent / 'docs' / document).read_text()
    note = note.replace('../perses/', '')
    note = note.replace('../deploy/perses_acceleration/',
                        str(source.parent / 'deploy/perses_acceleration') + '/')
    writes[document] = note.encode()
    link = '查询合并、预计算和回源规则见 [查询加速与原有口径](' + document + ')。'
    for name in ('METRICS_GUIDE.md', 'README.md'):
        target = RUNTIME / name
        if target.exists():
            current = target.read_text()
            if '](' + document + ')' not in current:
                writes[name] = (current.rstrip() + '\n\n' + link + '\n').encode()
    identities = sorted({(c['project'], c['dashboard']) for c in changes})
    for project, dashboard in identities:
        relative = 'projects/' + project + '/dashboards/' + dashboard + '.json'
        document = json.loads((RUNTIME / relative).read_text())
        for change in changes:
            if (change['project'], change['dashboard']) != (project, dashboard): continue
            key = change['panel']
            assert document['spec']['panels'][key]['spec']['queries'] == change['before']['spec']['queries'], 'Runtime query changed: ' + relative
            document['spec']['panels'][key]['spec']['queries'] = copy.deepcopy(change['after']['spec']['queries'])
        writes[relative] = json_bytes(document)
    for datasource in datasources:
        relative = 'projects/' + datasource['metadata']['project'] + '/perses-accelerated-datasource.json'
        clean = copy.deepcopy(datasource)
        clean['metadata'] = {k: clean['metadata'][k] for k in ('name', 'project')}
        target = RUNTIME / relative
        if target.exists():
            assert json.loads(target.read_text())['spec'] == clean['spec'], 'Existing accelerated datasource differs'
        writes[relative] = json_bytes(clean)
    writes['acceleration_state.json'] = json_bytes(state)
    entries = [{'path': name, 'before': encoded(RUNTIME / name), 'after': base64.b64encode(content).decode()} for name, content in writes.items()]
    save(evidence / 'generator-journal.json', json_bytes({'entries': entries, 'changes': changes, 'group': group}))


def install(evidence):
    for item in json.loads((evidence / 'generator-journal.json').read_text())['entries']:
        path = RUNTIME / item['path']
        assert path.parent.is_dir(), 'No implicit server directories'
        assert encoded(path) == item['before'], 'Concurrent generator edit: ' + item['path']
        save(path, base64.b64decode(item['after']))
        assert encoded(path) == item['after']


def rollback(evidence):
    path = evidence / 'generator-journal.json'
    if not path.exists(): return
    journal = json.loads(path.read_text())
    for item in reversed(journal['entries']):
        target = RUNTIME / item['path']
        current = encoded(target)
        if current == item['before']: continue
        if item['path'].startswith('projects/'):
            if item['path'].endswith('/perses-accelerated-datasource.json'):
                # A non-default unused datasource is inert; later groups may use it.
                assert current == item['after'], 'Concurrent datasource generator edit'
                continue
            value = json.loads(target.read_text())
            identity = (value['metadata']['project'], value['metadata']['name'])
            for change in journal['changes']:
                if (change['project'], change['dashboard']) != identity: continue
                queries = value['spec']['panels'][change['panel']]['spec']['queries']
                if queries == change['before']['spec']['queries']: continue
                assert queries == change['after']['spec']['queries'], 'Concurrent target generator query edit'
                value['spec']['panels'][change['panel']]['spec']['queries'] = change['before']['spec']['queries']
            save(target, json_bytes(value))
        elif item['path'] == 'acceleration_state.json':
            value = json.loads(target.read_text())
            selected = {(c['project'], c['dashboard'], c['panel']) for c in journal['changes']}
            if journal.get('group'):
                value['groups'] = [g for g in value['groups'] if g != journal['group']]
            else:
                value['merges'] = [m for m in value['merges'] if tuple(m) not in selected]
            save(target, json_bytes(value))
        else:
            # Inert shared helpers stay installed so a later independent batch
            # keeps working. The exact old bytes remain in the rollback journal.
            assert current == item['after'], 'Concurrent generator helper edit'
