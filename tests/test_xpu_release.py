"""Exercise XPU release failures without network or Docker operations."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def releases(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'perses'))
    def load(name, filename):
        spec = importlib.util.spec_from_file_location(name, ROOT / 'deploy' / filename)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        return module
    hosts = load('xpu_hosts_release', 'xpu_hosts_release.py')
    hardware = load('review_xpu_hardware_release', 'xpu_hardware_release.py')
    return hosts, hardware


@pytest.mark.parametrize('fault', ['publish_response', 'readback', 'reload', 'concurrent'])
def test_publish_failure_keeps_current_config_and_dashboard(releases, monkeypatch, tmp_path, fault):
    release, _ = releases
    config = tmp_path / 'scrape.yml'
    config.write_bytes(b'old')
    old = {'metadata': {'name': 'hosts-xpu'}, 'spec': {'revision': 'old'}}
    new = {'metadata': {'name': 'hosts-xpu'}, 'spec': {'revision': 'new'}}
    current = copy.deepcopy(old)
    for name, value in [('dashboards-before.json', {'xpu-monitoring': [old]}), ('hosts-xpu.json', new)]:
        (tmp_path / name).write_text(json.dumps(value))
    (tmp_path / 'scrape-before.yml').write_bytes(b'old')
    (tmp_path / 'scrape-candidate.yml').write_bytes(b'new')
    monkeypatch.setattr(release, 'ROOT', tmp_path)
    monkeypatch.setattr(release, 'CONFIG', config)
    monkeypatch.setattr(release, 'unchanged', lambda *a, **kw: None)
    monkeypatch.setattr(release.subprocess, 'check_output', lambda *a: json.dumps([
        {'Config': {'Entrypoint': ['vmagent']}, 'Image': 'mock'}]).encode())
    monkeypatch.setattr(release.subprocess, 'check_call', lambda *a: None)
    failure = OSError('injected ' + fault)
    writes = []
    def api(path, data=None):
        if data is not None:
            writes.append(copy.deepcopy(data))
            current.clear(); current.update(copy.deepcopy(data))
            if fault == 'concurrent':
                config.write_bytes(b'concurrent')
                current['spec'] = {'revision': 'concurrent'}
            if fault != 'readback':
                raise failure
        elif writes:
            raise failure
        return copy.deepcopy(current)
    config_writes = []
    def write_config(content):
        config_writes.append(content)
        config.write_bytes(content)
        if fault == 'reload':
            raise failure
    monkeypatch.setattr(release, 'api', api)
    monkeypatch.setattr(release, 'write_config', write_config)
    with pytest.raises(OSError) as caught:
        release.publish()
    assert caught.value is failure
    assert config_writes == [b'new']
    assert config.read_bytes() == (b'concurrent' if fault == 'concurrent' else b'new')
    assert writes == ([] if fault == 'reload' else [new])
    assert current['spec']['revision'] == ('old' if fault == 'reload' else 'concurrent' if fault == 'concurrent' else 'new')
    assert not (tmp_path / 'published.json').exists()


@pytest.mark.parametrize('mismatch', [None, 'value', 'label'])
def test_hardware_health_uses_same_evaluation_time_and_detects_mismatch(releases, monkeypatch, tmp_path, mismatch):
    release, hardware = releases
    document = {'spec': {field: {} for field in ('panels', 'layouts', 'variables', 'duration', 'refreshInterval')}}
    (tmp_path / 'hosts-xpu.json').write_text(json.dumps(document))
    (tmp_path / 'dashboards-before.json').write_text('{}')
    config = tmp_path / 'scrape-candidate.yml'
    config.write_bytes(b'candidate')
    monkeypatch.setattr(hardware, 'ROOT', tmp_path)
    monkeypatch.setattr(release, 'ROOT', tmp_path)
    monkeypatch.setattr(release, 'CONFIG', config)
    monkeypatch.setattr(hardware, 'PANELS', {})
    monkeypatch.setattr(release, 'unchanged', lambda *a, **kw: None)
    calls = []
    def response(url, direct=False):
        params = parse_qs(urlsplit(url).query)
        calls.append(params)
        # Without explicit time, sequential requests have different evaluation times.
        timestamp = float(params.get('time', [1000 + len(calls)])[0])
        series = [{'metric': {'node': node}, 'value': [timestamp, '1']} for node in ('xpu-1', 'xpu-2')]
        if direct:
            series.reverse()
            if mismatch == 'value':
                series[0]['value'][1] = '0'
            if mismatch == 'label':
                series[0]['metric']['node'] = 'other'
        return {'status': 'success', 'data': {'result': series}}
    monkeypatch.setattr(release, 'api', lambda path: document if path == release.PATH else response(path))
    monkeypatch.setattr(hardware.urllib.request, 'urlopen',
                        lambda url, **kw: io.BytesIO(json.dumps(response(url, True)).encode()))
    if mismatch:
        with pytest.raises(AssertionError):
            hardware.verify()
        assert not (tmp_path / 'hardware-verification.json').exists()
    else:
        hardware.verify()
        assert (tmp_path / 'hardware-verification.json').exists()
    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert 'time' in calls[0]
