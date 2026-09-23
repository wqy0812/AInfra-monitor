"""Regression coverage for clean checkouts, target identity and read-only checks."""
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import runpy
import shutil
import sys
import tarfile

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


validation = load('review_live_validation', 'scripts/validate_live.py')
checker = load('review_gateway_checker', 'deploy/check_gateway_monitor_candidate.py')


@pytest.mark.parametrize('existing', [False, True])
def test_download_creates_binary_directory_and_can_repeat_without_network(tmp_path, monkeypatch, existing):
    scripts, vendor = tmp_path / 'scripts', tmp_path / 'vendor'
    scripts.mkdir();vendor.mkdir()
    if existing:(vendor / 'bin').mkdir()
    shutil.copy2(ROOT / 'scripts/download.py', scripts / 'download.py')
    content = b'fixture node exporter binary'
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w:gz') as bundle:
        member = tarfile.TarInfo('node_exporter-fixture/node_exporter')
        member.size = len(content)
        bundle.addfile(member, io.BytesIO(content))
    blob = archive.getvalue()
    spec = {'name': 'node_exporter-fixture.tar.gz', 'browser_download_url': 'https://fixture.invalid/archive'}
    (vendor / 'releases.json').write_text(json.dumps({'fixture': {'assets': [spec]}}))
    (vendor / 'sha256sums.txt').write_text(hashlib.sha256(blob).hexdigest() + ' archive\n')
    calls = []
    def fetch(url, **kwargs):
        assert url == spec['browser_download_url']
        calls.append(url)
        return io.BytesIO(blob)
    monkeypatch.setattr('urllib.request.urlopen', fetch)
    runpy.run_path(str(scripts / 'download.py'))
    output = vendor / 'bin/node_exporter'
    assert output.read_bytes() == content and output.stat().st_mode & 0o111
    runpy.run_path(str(scripts / 'download.py'))
    assert calls == [spec['browser_download_url']]
    assert json.loads((vendor / 'manifest.json').read_text())['node_exporter']['sha256'] == hashlib.sha256(content).hexdigest()
    (vendor / 'sha256sums.txt').write_text('incorrect hash\n')
    with pytest.raises(AssertionError, match='checksum mismatch'):
        runpy.run_path(str(scripts / 'download.py'))


def target_rows():
    expected = validation.expected_targets(ROOT / 'deploy/scrape.yml')
    rows = [{'metric': dict(zip(validation.IDENTITY, key)), 'value': [100, '1']} for key in sorted(expected)]
    stamps = [{'metric': row['metric'], 'value': [100, '95']} for row in rows]
    return expected, rows, stamps


def test_all_configured_targets_including_relabeled_gateways_pass():
    expected, up, stamps = target_rows()
    assert len(expected) == 30
    assert ('aigate', '122.52.5.131:18082', 'a3-vllm') in expected
    assert ('aigate', '122.209.21.33:18082', 'xpu-pd') in expected
    assert {('node-xpu', address + ':9110', 'xpu-pd') for address in ('122.209.21.33', '122.209.21.34')} <= expected
    assert {('xpu-hardware', address + ':9507', 'xpu-pd') for address in ('122.209.21.33', '122.209.21.34')} <= expected
    assert len(validation.validate_targets(up, stamps, expected, 100)) == 30


@pytest.mark.parametrize('fault', ['missing', 'extra', 'duplicate', 'environment', 'down', 'stale', 'future', 'nan', 'missing-timestamp'])
def test_target_validation_rejects_identity_or_observation_fault(fault):
    expected, up, stamps = target_rows()
    if fault == 'missing':up.pop()
    if fault == 'extra':up.append({'metric': {'job': 'unexpected'}, 'value': [100, '1']})
    if fault == 'duplicate':up.append(copy.deepcopy(up[0]))
    if fault == 'environment':up[0]['metric']['environment'] = 'wrong'
    if fault == 'down':up[0]['value'][1] = '0'
    if fault == 'stale':stamps[0]['value'][1] = '80'
    if fault == 'future':stamps[0]['value'][1] = '101'
    if fault == 'nan':stamps[0]['value'][1] = 'NaN'
    if fault == 'missing-timestamp':stamps.pop()
    with pytest.raises(ValueError):validation.validate_targets(up, stamps, expected, 100)


def test_configured_duplicates_and_unsupported_discovery_fail(tmp_path):
    import yaml
    config = yaml.safe_load((ROOT / 'deploy/scrape.yml').read_text())
    config['scrape_configs'][0]['static_configs'][0]['targets'] *= 2
    path = tmp_path / 'scrape.yml';path.write_text(yaml.safe_dump(config))
    with pytest.raises(ValueError, match='duplicate'):validation.expected_targets(path)
    config['scrape_configs'][0]['static_configs'][0]['targets'] = ['host:1']
    config['scrape_configs'][0]['file_sd_configs'] = []
    path.write_text(yaml.safe_dump(config))
    with pytest.raises(ValueError, match='Unsupported'):validation.expected_targets(path)


@pytest.mark.asyncio
@pytest.mark.parametrize('write', [False, True])
async def test_gateway_candidate_checks_all_ranges_and_rejects_vm_writes(monkeypatch, write):
    import monitoring.api as api
    original_client = httpx.AsyncClient
    calls = []
    def transport(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(200, json={})
    def client(**kwargs):
        return original_client(transport=httpx.MockTransport(transport), **kwargs)
    monkeypatch.setattr(checker.httpx, 'AsyncClient', client)
    queried = []
    class Service:
        def __init__(self, environment):
            self.environment = environment
            self.client = client()
        async def history(self, hours):
            queried.append((self.environment, hours))
            if write:await self.client.post('http://vm/api/v1/import/prometheus', content='forbidden')
            else:await self.client.get('http://vm/api/v1/query_range')
            return {'environment': self.environment, 'points': [{'gateway': {'stream_idle_max_seconds': 0, 'oldest_age_seconds': 0}}],
                    'gateway_status': {'stream_idle_max_seconds': 'ok', 'oldest_age_seconds': 'ok'}}
    monkeypatch.setattr(api, 'Service', Service)
    if write:
        with pytest.raises(RuntimeError, match='read-only'):await checker.api()
        assert calls == []
    else:
        result = await checker.api()
        assert result['passed'] and result['read_only_vm_queries']
        assert queried == [(env, hours) for env in ('dcu-pd', 'a3-vllm') for hours in (1, 6, 24, 168, 720)]
        assert calls == [('GET', '/api/v1/query_range')] * 10


def test_gateway_release_fails_before_docker_when_checker_is_not_packaged(tmp_path, monkeypatch):
    release = load('review_local_release', 'deploy/local_latest_release.py')
    monkeypatch.setitem(sys.modules, 'local_latest_release', release)
    gateway = load('review_gateway_release', 'deploy/gateway_monitor_release.py')
    monkeypatch.setattr(release, 'ROOT', tmp_path)
    monkeypatch.setattr(release, 'command', lambda *a: pytest.fail('Docker must not be called'))
    with pytest.raises(AssertionError, match='Missing API candidate checker'):
        gateway.prepare()
