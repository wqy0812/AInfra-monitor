"""Fault injection for deployment tools; no Docker or service calls are made."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cv = load('container_validation', 'deploy/container_validation.py')
replacement = load('review_container_replace', 'deploy/replace.py')


def container(name='monitoring-api'):
    return {'Id': 'original', 'Image': 'previous', 'Name': '/' + name, 'RestartCount': 0,
            'State': {'Running': True, 'StartedAt': 'before'},
            'Config': {'Labels': {'monitoring.owner': 'independent'},
                       'Cmd': ['-m', 'uvicorn', 'monitoring.api:app', '--host', '0.0.0.0', '--port', '18430']},
            'HostConfig': {'NetworkMode': 'host', 'Binds': []}}


class Docker:
    def __init__(self, failure=None):
        self.containers = {'monitoring-api': container()}
        self.calls = []
        self.failure = failure

    def fail(self, stage):
        if self.failure == stage:
            self.failure = None
            self.at_failure = copy.deepcopy(self.containers)
            self.calls_at_failure = list(self.calls)
            raise RuntimeError('injected ' + stage)

    def locate(self, value):
        return next(key for key, c in self.containers.items() if key == value or c['Id'] == value)

    def inspect(self, value, **kwargs):
        return copy.deepcopy(self.containers[self.locate(value)])

    def find(self, name):
        return self.inspect(name) if name in self.containers else None

    def command(self, *args, **kwargs):
        self.calls.append(args)
        if args[:3] == ('docker', 'image', 'inspect'):
            return json.dumps([{'Id': 'candidate'}])
        action = args[1]
        self.fail(action + '-before')
        key = self.locate(args[-2] if action == 'rename' else args[-1])
        if action in ('stop', 'start'):
            self.containers[key]['State']['Running'] = action == 'start'
        elif action == 'rename':
            self.containers[args[-1]] = self.containers.pop(key)
        elif action == 'rm':
            del self.containers[key]
        else:
            raise AssertionError(args)
        self.fail(action + '-after')
        return ''

    def create(self, name, config):
        self.calls.append(('create', name))
        self.fail('create-before')
        self.containers[name] = {'Id': 'new', 'Image': config['Image'], 'Config': config,
                                 'State': {'Running': False}}
        self.fail('create-after')
        return 'new'

    def ready(self, inspect, identity, image, probe):
        assert self.inspect(identity)['State']['Running']
        assert any(c['Id'] == 'original' for c in self.containers.values())
        self.fail('acceptance')
        self.calls.append(('accepted',))

    def patch(self, monkeypatch):
        for name, function in [('inspect', self.inspect), ('find', self.find), ('cmd', self.command),
                               ('create', self.create), ('wait_ready', self.ready)]:
            monkeypatch.setattr(replacement, name, function)


@pytest.mark.parametrize('failure', ['stop-before', 'stop-after', 'rename-before', 'rename-after',
                                     'create-before', 'create-after', 'start-before', 'start-after', 'acceptance'])
def test_replacement_preserves_failure_state_without_more_docker_calls(monkeypatch, failure):
    docker = Docker(failure)
    docker.patch(monkeypatch)
    with pytest.raises(RuntimeError, match='injected'):
        replacement.replace('monitoring-api', 'candidate-tag')
    assert docker.containers == docker.at_failure
    assert docker.calls == docker.calls_at_failure
    assert any(c['Id'] == 'original' for c in docker.containers.values())


def test_replacement_removes_backup_only_after_acceptance(monkeypatch):
    docker = Docker()
    docker.patch(monkeypatch)
    result = replacement.replace('monitoring-api', 'candidate-tag')
    assert result['acceptance'] == 'passed'
    assert docker.calls.index(('accepted',)) < docker.calls.index(('docker', 'rm', 'original'))
    assert list(docker.containers) == ['monitoring-api']


@pytest.mark.parametrize('failure', [None, 'create-after', 'acceptance', 'rm-before', 'rm-after'])
def test_replacement_evidence_survives_success_or_uncertain_mutation(tmp_path, monkeypatch, failure):
    docker = Docker(failure)
    docker.containers['monitoring-api']['Config']['Env'] = ['SECRET=do-not-persist']
    docker.patch(monkeypatch)
    if failure:
        with pytest.raises(RuntimeError, match='injected'):
            replacement.replace('monitoring-api', 'candidate-tag', evidence=tmp_path)
        assert docker.containers == docker.at_failure and docker.calls == docker.calls_at_failure
        report = json.loads((tmp_path / 'container-failure.json').read_text())
        assert report['recovery'] == 'fix_forward' and report['automatic_rollback'] is False
        assert not (tmp_path / 'container-publication.json').exists()
    else:
        result = replacement.replace('monitoring-api', 'candidate-tag', evidence=tmp_path)
        assert json.loads((tmp_path / 'container-publication.json').read_text()) == result
        assert result['container_id'] == 'new'
    assert json.loads((tmp_path / 'container-before.json').read_text())['Id'] == 'original'
    assert 'configuration_sha256' in json.loads((tmp_path / 'container-before.json').read_text())
    assert all('do-not-persist' not in p.read_text() for p in tmp_path.iterdir())
    assert json.loads((tmp_path / 'container-transaction.json').read_text())['image'] == 'candidate'
    calls = list(docker.calls)
    with pytest.raises(RuntimeError, match='fresh evidence'):
        replacement.replace('monitoring-api', 'candidate-tag', evidence=tmp_path)
    assert docker.calls == calls


def test_api_replacement_normalizes_explicit_clients_and_preserves_other_settings(monkeypatch):
    docker = Docker()
    docker.containers['monitoring-api']['Config']['Env'] = ['ALLOWED_CLIENTS=*', 'OTHER=preserve']
    docker.patch(monkeypatch)
    replacement.replace('monitoring-api', 'candidate-tag', allowed_clients=' 127.0.0.1, ::1,127.0.0.1 ')
    config = docker.containers['monitoring-api']['Config']
    assert config['Env'] == ['OTHER=preserve', 'ALLOWED_CLIENTS=127.0.0.1,::1']
    assert config['Cmd'].count('--no-proxy-headers') == 1


@pytest.mark.parametrize('clients', ['', ' ', '*', '127.0.0.1,*', '127.0.0.1,', 'localhost', '0.0.0.0/0', '192.0.2.999'])
@pytest.mark.parametrize('explicit', [False, True])
def test_invalid_api_clients_are_rejected_before_container_changes(monkeypatch, clients, explicit):
    docker = Docker()
    docker.containers['monitoring-api']['Config']['Env'] = [
        'ALLOWED_CLIENTS=' + ('127.0.0.1' if explicit else clients), 'OTHER=preserve']
    before = copy.deepcopy(docker.containers)
    docker.patch(monkeypatch)
    with pytest.raises(ValueError):
        replacement.replace('monitoring-api', 'candidate-tag', allowed_clients=clients if explicit else None)
    assert docker.containers == before
    assert docker.calls == [('docker', 'image', 'inspect', 'candidate-tag')]


@pytest.mark.parametrize('environment,expected', [
    (['OTHER=preserve'], '127.0.0.1,::1'),
    (['OTHER=preserve', 'ALLOWED_CLIENTS= 127.0.0.1, ::1,127.0.0.1 '], '127.0.0.1,::1'),
    (['OTHER=preserve', 'ALLOWED_CLIENTS=192.0.2.1'], '192.0.2.1'),
])
def test_api_replacement_validates_inherited_clients_and_pins_default(monkeypatch, environment, expected):
    docker = Docker()
    docker.containers['monitoring-api']['Config']['Env'] = environment
    docker.patch(monkeypatch)
    replacement.replace('monitoring-api', 'candidate-tag')
    assert docker.containers['monitoring-api']['Config']['Env'] == ['OTHER=preserve', 'ALLOWED_CLIENTS=' + expected]


def test_api_client_validation_works_outside_repository_cwd(tmp_path):
    import subprocess
    result = subprocess.run([sys.executable, '-I', '-c',
        "import runpy,sys; sys.path.insert(0,sys.argv[1]); "
        "module=runpy.run_path(sys.argv[1]+'/replace.py'); "
        "print(module['api_clients']('127.0.0.1, ::1'))", str(ROOT/'deploy')],
        cwd=tmp_path, check=True, capture_output=True, text=True)
    assert result.stdout.strip() == '127.0.0.1,::1'


def test_replacement_preflight_rejects_collision_and_unknown_component(monkeypatch):
    docker = Docker()
    docker.patch(monkeypatch)
    monkeypatch.setattr(replacement, 'find', lambda name: container())
    with pytest.raises(RuntimeError, match='Backup name'):
        replacement.replace('monitoring-api', 'tag')
    assert not any(c[:2] == ('docker', 'stop') for c in docker.calls)
    with pytest.raises(RuntimeError, match='Unsupported'):
        cv.ComponentProbe('unknown', container())


class Clock:
    def __init__(self):
        self.now = 0
    def monotonic(self):
        return self.now
    def sleep(self, seconds):
        self.now += seconds


@pytest.mark.parametrize('fault', [None, 'data', 'stopped', 'restarting', 'identity', 'image', 'health'])
def test_acceptance_checks_new_version_and_data_with_bounded_startup_retry(monkeypatch, fault):
    clock = Clock()
    monkeypatch.setattr(cv, 'time', clock)
    state = container()
    state.update(Id='new', Image='candidate')
    def inspect(*args, **kwargs):
        value = copy.deepcopy(state)
        if fault == 'stopped':value['State']['Running'] = False
        if fault == 'restarting':value['State']['Restarting'] = True
        if fault == 'identity':value['Id'] = 'other'
        if fault == 'image':value['Image'] = 'wrong'
        if fault == 'health':value['State']['Health'] = {'Status': 'unhealthy'}
        return value
    def check(deadline):
        if fault == 'data':raise RuntimeError('health OK, data stale')
    if fault:
        with pytest.raises(RuntimeError, match='timed out'):
            cv.wait_ready(inspect, 'new', 'candidate', SimpleNamespace(check=check))
        assert clock.now == 90
    else:
        cv.wait_ready(inspect, 'new', 'candidate', SimpleNamespace(check=check))
        assert clock.now == 0


def test_acceptance_retries_startup_without_a_continuity_window(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(cv, 'time', clock)
    state = container();state.update(Id='new', Image='candidate', RestartCount=1)
    def check(deadline):
        if clock.now < 6:raise RuntimeError('startup data not ready')
    cv.wait_ready(lambda *a, **k: state, 'new', 'candidate', SimpleNamespace(check=check))
    assert clock.now == 6


def test_replacement_accepts_a_service_stopped_for_maintenance(monkeypatch):
    docker = Docker()
    docker.containers['monitoring-api']['State']['Running'] = False
    docker.patch(monkeypatch)
    assert replacement.replace('monitoring-api', 'candidate-tag')['acceptance'] == 'passed'


def make_probe(name):
    state = container(name)
    args = {'monitoring-vm': ['-httpListenAddr=127.0.0.1:18428'],
            'monitoring-vmagent': ['-httpListenAddr=127.0.0.1:18429'],
            'monitoring-node': ['--web.listen-address=127.0.0.1:19100'], 'monitoring-dcu': []}
    if name in args:state['Config']['Cmd'] = args[name]
    if name == 'monitoring-dcu':state['Config']['Env'] = ['BIND=192.0.2.8']
    return cv.ComponentProbe(name, state)


@pytest.mark.parametrize('name', ['monitoring-api', 'monitoring-vm', 'monitoring-vmagent', 'monitoring-node', 'monitoring-dcu'])
def test_component_probes_check_real_payloads_and_actual_listener(monkeypatch, name):
    probe = make_probe(name)
    now = cv.time.time()
    count = [10]
    bad = [False]
    def fetch(url, **kwargs):
        if '/health' in url:return io.BytesIO(b'{"status":"ok"}')
        if '/latest?' in url:
            env = url.split('environment=')[1]
            payload = {'environment': env, 'ts': now - (100 if bad[0] else 1),
                       'nodes': {role: {'metrics': {'status': 'ok'}} for role in ('prefill', 'decode')}}
        elif '/query?' in url:
            payload = {'status': 'success', 'data': {'result': [] if bad[0] else [{'value': [now, '1']}]}}
        else:
            if name == 'monitoring-vmagent':
                text = 'vm_promscrape_scrapes_total{status_code="200"} ' + str(count[0] if not bad[0] else -1)
            elif name == 'monitoring-node':
                text = f'node_cpu_seconds_total{{cpu="0",mode="idle"}} 1\nnode_memory_MemTotal_bytes 100\nnode_memory_MemAvailable_bytes {101 if bad[0] else 30}\nnode_boot_time_seconds 1\nnode_time_seconds {now}'
            else:
                assert url.startswith('http://192.0.2.8:19500/')
                text = f'dcu_sample_success 1\ndcu_sample_timestamp_seconds {now - (100 if bad[0] else 1)}\n'
                for i in range(8):
                    for metric in ('utilization_percent', 'memory_used_bytes', 'memory_total_bytes', 'temperature_celsius', 'power_watts'):
                        text += f'dcu_{metric}{{device="card{i}"}} 1\n'
            return io.BytesIO(text.encode())
        return io.BytesIO(json.dumps(payload).encode())
    monkeypatch.setattr(cv.urllib.request, 'build_opener', lambda *a: SimpleNamespace(open=fetch))
    if name == 'monitoring-vmagent':
        with pytest.raises(RuntimeError, match='progressing'):probe.check(cv.time.monotonic() + 20)
        with pytest.raises(RuntimeError, match='progressing'):probe.check(cv.time.monotonic() + 20)
        count[0] += 1
    probe.check(cv.time.monotonic() + 20)
    bad[0] = True
    with pytest.raises(RuntimeError):probe.check(cv.time.monotonic() + 20)


image_release = load('review_image_release', 'perses/performance/image_release.py')
local_candidate = load('review_local_candidate', 'perses/performance/local_candidate.py')


class PersesDocker(Docker):
    def __init__(self, failure=None):
        super().__init__(failure)
        self.containers = {'monitoring-perses': container('monitoring-perses')}

    def inspect(self, value, **kwargs):
        if value == 'candidate':
            return {'Id': value, 'Architecture': 'amd64', 'Os': 'linux',
                    'Config': {'Labels': {'monitoring.patch': 'perses-0.54.0-perf.2'}}}
        return super().inspect(value, **kwargs)

    def command(self, *args):
        if args[0] == 'systemctl':
            self.calls.append(args)
            action = args[1]
            self.fail(action + '-before')
            if 'monitoring-perses' in self.containers:
                self.containers['monitoring-perses']['State']['Running'] = action == 'start'
            self.fail(action + '-after')
            return ''
        return super().command(*args)

    def create_perses(self, old, name, image, binds, listen):
        return self.create(name, {'Image': image, 'Labels': {'monitoring.transaction': old['Id']}})

    def health(self, base):
        self.fail('acceptance')
        return {'version': '0.54.0-perf.2'}

    def patch_perses(self, monkeypatch):
        monkeypatch.setattr(image_release, 'BACKUP', 'monitoring-perses-before-perf2')
        for name, fn in [('inspect', self.inspect), ('find_container', self.find), ('run', self.command),
                         ('create', self.create_perses), ('health', self.health), ('resources', lambda: {}),
                         ('save', lambda *a: None)]:
            monkeypatch.setattr(image_release, name, fn)


@pytest.mark.parametrize('failure', ['stop-before', 'stop-after', 'rename-before', 'rename-after',
                                     'create-before', 'create-after', 'start-before', 'start-after', 'acceptance'])
def test_perses_preserves_failure_state_even_after_uncertain_mutation(monkeypatch, tmp_path, failure):
    docker = PersesDocker(failure)
    docker.patch_perses(monkeypatch)
    with pytest.raises(RuntimeError, match='injected'):
        image_release.apply_image(tmp_path, 'candidate', '0.54.0-perf.2', 'previous', {})
    assert docker.containers == docker.at_failure
    assert docker.calls == docker.calls_at_failure
    assert any(c['Id'] == 'original' for c in docker.containers.values())


def test_perses_backup_collision_rejected_before_stopping(monkeypatch, tmp_path):
    docker = PersesDocker()
    docker.containers['monitoring-perses-before-perf2'] = container()
    docker.patch_perses(monkeypatch)
    with pytest.raises(AssertionError, match='Backup name'):
        image_release.apply_image(tmp_path, 'candidate', '0.54.0-perf.2', 'previous', {})
    assert not docker.calls


def test_perses_success_preserves_versioned_backup(monkeypatch, tmp_path):
    docker = PersesDocker()
    docker.patch_perses(monkeypatch)
    image_release.apply_image(tmp_path, 'candidate', '0.54.0-perf.2', 'previous', {})
    assert docker.containers['monitoring-perses']['Id'] == 'new'
    assert docker.containers['monitoring-perses-before-perf2']['Id'] == 'original'
    assert not docker.containers['monitoring-perses-before-perf2']['State']['Running']


@pytest.mark.parametrize('fault', [None, 'digest', 'architecture', 'label', 'version'])
def test_local_candidate_uses_locked_images_and_checks_versions(monkeypatch, fault):
    lock_path = ROOT / 'perses/performance/release-lock.json'
    lock = json.loads(lock_path.read_text())
    baseline = json.loads((ROOT / 'perses/image-lock.json').read_text())
    calls = []
    def command(args, **kwargs):
        calls.append(args)
        if args[:3] == ['docker', 'image', 'inspect']:
            image = args[-1]
            value = {'Id': image, 'Architecture': 'amd64', 'Os': 'linux',
                     'Config': {'Labels': {'monitoring.patch': 'perses-' + lock['candidate_version']}}}
            if image == lock['candidate_config_digest']:
                if fault == 'digest':value['Id'] = 'wrong'
                if fault == 'architecture':value['Architecture'] = 'arm64'
                if fault == 'label':value['Config']['Labels']['monitoring.patch'] = 'perses-old'
            return json.dumps([value]).encode()
        version = lock['candidate_version'] if args[-2] == lock['candidate_config_digest'] else '0.54.0'
        if fault == 'version':version = '0.54.0-perf.1'
        return ('perses, version ' + version + ' (branch: HEAD)').encode()
    monkeypatch.setattr(local_candidate.subprocess, 'check_output', command)
    if fault:
        with pytest.raises(AssertionError):local_candidate.selected_images(lock_path)
    else:
        selected = local_candidate.selected_images(lock_path)
        assert selected == [('candidate', 18541, lock['candidate_config_digest']), ('baseline', 18542, baseline['image_id'])]
    assert not any('-d' in call for call in calls)


def test_perses_preflight_rejects_wrong_candidate_before_stopping(monkeypatch, tmp_path):
    docker = PersesDocker()
    docker.patch_perses(monkeypatch)
    original_inspect = docker.inspect
    def inspect(value):
        state = original_inspect(value)
        if value == 'candidate':state['Config']['Labels']['monitoring.patch'] = 'perses-old'
        return state
    monkeypatch.setattr(image_release, 'inspect', inspect)
    with pytest.raises(AssertionError, match='version mismatch'):
        image_release.apply_image(tmp_path, 'candidate', '0.54.0-perf.2', 'previous', {})
    assert not docker.calls


def test_agent_cannot_accept_only_failed_scrapes(monkeypatch):
    probe = make_probe('monitoring-vmagent')
    def fetch(url, **kwargs):
        return io.BytesIO(b'OK' if '/health' in url else b'vm_promscrape_scrapes_total{status_code="500"} 1000\n')
    monkeypatch.setattr(cv.urllib.request, 'build_opener', lambda *a: SimpleNamespace(open=fetch))
    with pytest.raises(RuntimeError, match='successful'):
        probe.check(cv.time.monotonic() + 10)
