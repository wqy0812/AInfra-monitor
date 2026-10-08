"""Run real CLI parsing without Docker, credentials, networking or publication."""
import ast
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('component', [
    'container', 'image', 'dashboards', 'runtime', 'acceleration', 'acceleration-ready', 'merges',
])
def test_unified_help_loads_each_implementation_without_workspace_path_assumptions(component, tmp_path):
    result = subprocess.run([sys.executable, str(ROOT / 'deploy/release.py'), component, '--help'],
                            cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert '--evidence' in result.stdout
    assert 'rollback' not in result.stdout.lower()


@pytest.mark.parametrize('component', ['image', 'dashboards', 'runtime', 'acceleration', 'merges'])
def test_removed_rollback_is_rejected_by_actual_component_parser(component, tmp_path):
    args = [sys.executable, str(ROOT / 'deploy/release.py'), component, 'rollback', '--evidence', str(tmp_path)]
    if component == 'acceleration':
        args += ['--group', 'cpu']
    result = subprocess.run(args, cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert "invalid choice: 'rollback'" in result.stderr
    assert not list(tmp_path.iterdir())


def test_no_importable_rollback_implementation_remains():
    # These are the maintained tool directories. Ignored release payloads and
    # local evidence may contain immutable copies of earlier source versions.
    for directory in ('deploy', 'deploy/perses_acceleration', 'perses', 'perses/performance'):
        for path in (ROOT / directory).glob('*.py'):
            if path.name.startswith('test') or 'tests' in path.parts:
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    assert node.name not in {'rollback', 'restore', 'restore_original'}, path


def test_current_acceleration_status_matches_versioned_resources():
    import json
    state = json.loads((ROOT / 'perses/acceleration_state.json').read_text())
    catalog = json.loads((ROOT / 'monitoring/perses_acceleration_catalog.json').read_text())
    bindings = {}
    for path in (ROOT / 'perses/projects').glob('*/dashboards/*.json'):
        document = json.loads(path.read_text())
        count = sum(any(q['spec']['plugin']['spec'].get('datasource', {}).get('name') == 'perses-accelerated'
                        for q in p['spec']['queries']) for p in document['spec']['panels'].values())
        project = document['metadata']['project']
        bindings[project] = bindings.get(project, 0) + count
    assert bindings == {'a3-monitoring': 8, 'dcu-monitoring': 8, 'xpu-monitoring': 2}
    assert len(catalog['panels']) == 18 and len(state['merges']) == 14
    assert set(state['groups']) == {'cpu', 'dcu', 'a3'}
