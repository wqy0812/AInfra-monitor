"""Exercise release authentication and complete resource snapshots over HTTP."""
import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location('release_auth_under_test', Path(__file__).resolve().parents[1] / 'image_release.py')
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


@pytest.fixture
def server(tmp_path, monkeypatch):
    credentials = tmp_path / 'credentials.json'
    credentials.write_text(json.dumps({'login': 'admin', 'password': 'test-only'}))
    monkeypatch.setenv('PERSES_CREDENTIALS_FILE', str(credentials))
    state = {'logins': 0, 'token': None, 'requests': [], 'xpu_revision': 1, 'deny': False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, value):
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            assert self.path == '/api/auth/providers/native/login'
            assert json.loads(self.rfile.read(int(self.headers['Content-Length']))) == json.loads(credentials.read_text())
            state['logins'] += 1
            state['token'] = f"token-{state['logins']}"
            self.reply(200, {'access_token': state['token']})

        def do_GET(self):
            state['requests'].append(self.path)
            if state['deny'] or self.headers.get('Authorization') != 'Bearer ' + str(state['token']):
                self.reply(401, {'private': 'must not appear in errors'})
                return
            if self.path == '/api/v1/projects':
                self.reply(200, [{'metadata': {'name': p}} for p in ('xpu-monitoring', 'dcu-monitoring', 'a3-monitoring', 'custom-project')])
            else:
                self.reply(200, [{'metadata': {'name': 'resource'}, 'spec': {'revision': state['xpu_revision'] if '/xpu-monitoring/' in self.path else 1}}])

    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{httpd.server_port}'
    release.CLIENTS.clear()
    try:
        yield base, state
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()
        release.CLIENTS.clear()


def test_authenticated_snapshot_covers_all_projects_and_detects_xpu_changes(server):
    base, state = server
    before = release.resources(base)
    assert len(before) == 9
    assert 'xpu-monitoring/dashboards' in before and 'custom-project/datasources' in before
    assert state['logins'] == 1
    assert release.resources(base) == before
    state['xpu_revision'] = 2
    assert release.resources(base) != before


def test_expired_token_reauthenticates_once_without_exposing_response(server):
    base, state = server
    api = release.client(base)
    api.get('/api/v1/projects')
    state['token'] = 'expired'
    api.get('/api/v1/projects')
    assert state['logins'] == 2
    state['deny'] = True
    with pytest.raises(RuntimeError, match='HTTP 401') as failure:
        api.get('/api/v1/projects')
    assert 'private' not in str(failure.value)
    assert state['logins'] == 3


def test_tokens_are_scoped_to_each_server(server):
    base, state = server
    first = release.client(base)
    second = release.client(base.replace('127.0.0.1', 'localhost'))
    assert first is not second
    first.get('/api/v1/projects')
    assert second.token is None
    second.get('/api/v1/projects')
    assert state['logins'] == 2


def test_perf3_requires_and_pins_perf2_upgrade_baseline():
    root = Path(__file__).resolve().parents[1]
    lock = json.loads((root / 'release-lock-perf3.json').read_text())
    previous = json.loads((root / 'release-lock.json').read_text())
    assert release.previous_release(lock) == (previous['candidate_config_digest'], previous['candidate_version'])
    for missing in ('previous_image_digest', 'previous_version'):
        broken = dict(lock)
        del broken[missing]
        with pytest.raises(AssertionError, match='must pin'):
            release.previous_release(broken)
