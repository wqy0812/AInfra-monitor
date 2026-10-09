"""Scoped generator installation, with byte evidence and concurrency checks."""
import base64
import copy
import hashlib
import json
from pathlib import Path

RUNTIME = Path('/data2/monitoring/perses/release')
MODULES = ('query_acceleration.py', 'query_slimming.py', 'acceleration_catalog.py', 'acceleration_publication.py', 'dashboard_reorg.py')
DOCUMENTS = ('perses-query-acceleration.md', 'query-slimming.md')
BASE_HASH = '38b62a8cc6378aecc7837bdfc144f9300d282cf35e0b22bf67bddc4e4f54aba0'


def source_paths():
    return tuple('perses/' + name for name in MODULES) + tuple('docs/' + name for name in DOCUMENTS)


def encoded(path):
    return base64.b64encode(path.read_bytes()).decode() if path.exists() else None


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()


def save(path, content):
    temporary = path.with_suffix('.tmp'); temporary.write_bytes(content); temporary.replace(path)


def plan(evidence, changes, group=None, datasources=(), *, rewrites=(), replace_revision=False):
    assert not (evidence / 'generator-journal.json').exists()
    assert RUNTIME.is_dir()
    source = Path(__file__).resolve().parents[2] / 'perses'
    audited_sources = json.loads((evidence / 'query-meta.json').read_text())['tool_sha256'] if rewrites else None

    def candidate_bytes(relative):
        content = (source.parent / relative).read_bytes()
        if audited_sources is not None:
            assert audited_sources.get(relative) == hashlib.sha256(content).hexdigest(), 'Candidate source changed since audit: ' + relative
        # Journal exactly the bytes checked here; do not reread after validation.
        return content

    state_path = RUNTIME / 'acceleration_state.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {'schema': 1, 'merges': [], 'groups': []}
    selected = [(c['project'], c['dashboard'], c['panel']) for c in changes]
    if rewrites:
        from query_slimming import kind_for
        assert all(len(e) == 4 and kind_for(e[:3]) == e[3] for e in rewrites)
        assert {tuple(e[:3]) for e in rewrites} == set(selected)
        state['rewrites'] = sorted({tuple(e) for e in state.get('rewrites', [])} | {tuple(e) for e in rewrites})
    elif group is None:
        assert not set(selected) & {tuple(x) for x in state['merges']}
        state['merges'] = sorted({tuple(x) for x in state['merges']} | set(selected))
    else:
        assert group in ('cpu', 'dcu', 'a3')
        assert (group in state['groups']) == replace_revision
        state['groups'] = sorted(set(state['groups']) | {group})
    if replace_revision:
        assert group in state['groups'], 'Cannot replace an inactive group'
    writes = {}
    frozen = json.loads((evidence / 'query-runtime-before.json').read_text()) if rewrites else None
    for name in MODULES:
        target = RUNTIME / name
        candidate = candidate_bytes('perses/' + name)
        if frozen is not None:
            assert name in frozen and encoded(target) == frozen[name], 'Concurrent generator edit: ' + name
        elif name == 'dashboard_reorg.py':
            assert target.read_bytes() == candidate or hashlib.sha256(target.read_bytes()).hexdigest() == BASE_HASH, 'Generator changed concurrently'
        else:
            assert not target.exists() or target.read_bytes() == candidate, 'Unexpected existing generator module: ' + name
        writes[name] = candidate
    # The generated guide no longer contains the independently maintained note.
    # Install that document beside the runtime guides and preserve their history.
    document = 'perses-query-acceleration.md'
    for name in DOCUMENTS:
        note = candidate_bytes('docs/' + name).decode()
        note = note.replace('../perses/', '')
        note = note.replace('../deploy/perses_acceleration/',
                            str(source.parent / 'deploy/perses_acceleration') + '/')
        note = note.replace('(maintenance-window-upgrade.md)',
                            '(' + str(source.parent / 'docs/maintenance-window-upgrade.md') + ')')
        note = note.replace('](releases/', '](' + str(source.parent / 'docs/releases') + '/')
        writes[name] = note.encode()
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
    if frozen is not None:
        for name in writes:
            assert name in frozen and encoded(RUNTIME / name) == frozen[name], 'Concurrent runtime edit: ' + name
    entries = [{'path': name, 'before': encoded(RUNTIME / name), 'after': base64.b64encode(content).decode()} for name, content in writes.items()]
    save(evidence / 'generator-journal.json', json_bytes({'entries': entries, 'changes': changes, 'group': group}))


def install(evidence):
    entries = json.loads((evidence / 'generator-journal.json').read_text())['entries']
    # Check every entry before the first write, then recheck per write to catch
    # both pre-existing conflicts and edits racing this installation.
    for item in entries:
        path = RUNTIME / item['path']
        assert path.parent.is_dir(), 'No implicit server directories'
        assert encoded(path) == item['before'], 'Concurrent generator edit: ' + item['path']
    for item in entries:
        path = RUNTIME / item['path']
        assert path.parent.is_dir(), 'No implicit server directories'
        assert encoded(path) == item['before'], 'Concurrent generator edit: ' + item['path']
        save(path, base64.b64decode(item['after']))
        assert encoded(path) == item['after']
