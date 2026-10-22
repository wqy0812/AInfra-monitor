"""Production reconcile path: durable revisions, native validation, and ownership."""
import asyncio
import base64
import copy
import json
from pathlib import Path
import time

import httpx
import pytest

from monitoring.controller import Controller, Conflict, Settings, create_app
from monitoring.provisioning import atomic_json, digest, perses_resources, scrape_config, store_credentials

ROOT = Path(__file__).resolve().parents[1]


def catalog(revision=1):
    value = {'schema_version': 1, 'revision': revision, 'foundations': [{
        'id': 'new-foundation', 'name': '新的底座', 'monitoring_source': 'foundation-new',
        'monitoring_adapter': 'dcu-sglang-v1', 'enabled': True,
        'resource_nodes': [{'name': 'new-prefill', 'role': 'prefill'}, {'name': 'new-decode', 'role': 'decode'}],
        'monitoring_endpoints': [{'id': 'host-1', 'name': '主机', 'type': 'host',
            'url': 'https://exporter.example.invalid:9100/custom/metrics', 'node': 'new-prefill',
            'role': 'prefill', 'enabled': True, 'auth': {'type': 'basic', 'username': 'reader', 'password': 'private-secret'}},
            {'id': 'inference-1', 'name': '推理', 'type': 'inference', 'url': 'http://model.example.invalid:8000/metrics',
             'node': 'new-prefill', 'role': 'prefill', 'auth': {'type': 'none'}}],
        'router': {'id': 'router-new', 'proxy_url': 'http://proxy.example.invalid:18081',
                   'profile_url': 'http://proxy.example.invalid:18082', 'maintenance': False}}],
        'gateways': [{'id': 'entry-1', 'name': '入口', 'business_url': 'http://gateway.example.invalid',
                      'router_id': 'router-new', 'enabled': True, 'admission': True}]}
    for kind, role, port in [('inference', 'decode', 8001), ('accelerator', 'prefill', 9500), ('mooncake', 'prefill', 9000)]:
        value['foundations'][0]['monitoring_endpoints'].append({'id': kind + '-' + role, 'name': kind, 'type': kind,
            'url': 'http://extra.example.invalid:' + str(port) + '/metrics', 'node': 'new-' + role, 'role': role,
            'component': 'master' if kind == 'mooncake' else None, 'auth': {'type': 'none'}})
    return value


def settings(tmp_path):
    return Settings(state=tmp_path, catalog=tmp_path / 'catalog.json', control=tmp_path / 'probes-control.json',
                    probe_status=tmp_path / 'probes-runtime.json', token='a' * 40, allowed_clients={'127.0.0.1'},
                    templates=ROOT / 'perses', require_tls=False)


class Publisher:
    def __init__(self):
        self.calls = []
        self.resources = {}
        self.fail = None
        self.unknown_once = False

    async def close(self):
        pass

    async def scrape_status(self):
        return {}

    async def check_endpoint(self, endpoint, adapter):
        return {'status': 'passed', 'checked_at': time.time(), 'format_valid': True, 'type_matches': True, 'origin': 'monitoring-controller'}

    async def dry_run(self, component, path):
        self.calls.append(('validate', component))
        assert json.loads(path.read_text())
        if self.fail == component:
            raise RuntimeError(component + '_validation_failed')

    async def readback(self, component, value):
        pass

    async def reload(self, component, value):
        self.calls.append(('reload', component))

    async def perses_request(self, method, path, body=None):
        self.calls.append((method, path))
        if method == 'GET':
            return copy.deepcopy(self.resources.get(path))
        key = path + '/' + body['metadata']['name'] if method == 'POST' else path
        self.resources[key] = copy.deepcopy(body)
        if self.unknown_once:
            self.unknown_once = False
            raise httpx.ReadTimeout('unknown')
        return copy.deepcopy(body)


@pytest.mark.asyncio
async def test_secret_is_private_and_revisions_are_idempotent(tmp_path):
    publisher = Publisher()
    c = Controller(settings(tmp_path), publisher)
    body = catalog()
    await c.accept(body)
    assert 'private-secret' not in (tmp_path / 'desired.json').read_text()
    assert 'private-secret' not in json.dumps(c.status())
    secret = next((tmp_path / 'secrets').iterdir())
    assert base64.b64decode(secret.read_bytes()).decode() == 'reader:private-secret'
    assert secret.stat().st_mode & 0o777 == 0o600
    await c.accept(copy.deepcopy(body))
    assert c.state['accepted_revision'] == 1
    with pytest.raises(Conflict, match='revision_conflict'):
        changed = copy.deepcopy(body)
        changed['foundations'][0]['name'] = 'changed'
        await c.accept(changed)
    await c.accept(catalog(2))
    with pytest.raises(Conflict, match='stale'):
        await c.accept(body)
    restored = Controller(settings(tmp_path), Publisher())
    assert restored.state['accepted_revision'] == 2
    assert restored.desired['catalog']['revision'] == 2


@pytest.mark.asyncio
async def test_full_reconcile_generates_new_source_and_seven_dashboards(tmp_path):
    pub = Publisher()
    c = Controller(settings(tmp_path), pub)
    await c.accept(catalog())
    atomic_json(c.settings.probe_status, {'observed_at': time.time(), 'targets': 2, 'catalog_revision': 1, 'target_ids': ['foundation:new-foundation', 'gateway:entry-1']})
    await c.reconcile()
    assert c.status()['applied_revision'] == 1
    assert c.status()['foundations'][0]['status'] == 'ready'
    public = (tmp_path / 'catalog.json').read_text()
    assert 'private-secret' not in public and 'password_file' not in public
    scrape = json.loads((tmp_path / 'scrape.json').read_text())
    host = next(j for j in scrape['scrape_configs'] if j['job_name'].endswith('host-1'))
    assert host['scheme'] == 'https' and host['metrics_path'] == '/custom/metrics'
    assert host['static_configs'][0]['labels']['environment'] == 'foundation-new'
    dashboards = [v for v in pub.resources.values() if v['kind'] == 'Dashboard']
    assert len(dashboards) == 7
    queries = [q['spec']['plugin']['spec']['query'] for d in dashboards for p in d['spec']['panels'].values()
               for q in p['spec'].get('queries', [])]
    assert any('environment="foundation-new"' in q for q in queries)
    assert not any('environment="dcu-pd"' in q and 'sglang' in q for q in queries)
    calls = len([x for x in pub.calls if x[0] in ('POST', 'PUT', 'reload')])
    await c.reconcile()
    assert len([x for x in pub.calls if x[0] in ('POST', 'PUT', 'reload')]) == calls


@pytest.mark.asyncio
async def test_invalid_candidate_keeps_active_file_and_recovers_forward(tmp_path):
    pub = Publisher()
    c = Controller(settings(tmp_path), pub)
    await c.accept(catalog())
    atomic_json(c.settings.probe_status, {'observed_at': time.time(), 'catalog_revision': 1, 'target_ids': ['foundation:new-foundation', 'gateway:entry-1']})
    await c.reconcile()
    old = (tmp_path / 'scrape.json').read_bytes()
    body = catalog(2)
    body['foundations'][0]['monitoring_endpoints'][0]['url'] = 'http://different.invalid:9100/metrics'
    await c.accept(body)
    pub.fail = 'scrape'
    await c.reconcile()
    assert (tmp_path / 'scrape.json').read_bytes() == old
    assert c.status()['foundations'][0]['components']['scrape']['status'] == 'error'
    pub.fail = None
    atomic_json(c.settings.probe_status, {'observed_at': time.time(), 'catalog_revision': 2, 'target_ids': ['foundation:new-foundation', 'gateway:entry-1']})
    await c.reconcile()
    assert c.status()['applied_revision'] == 2
    assert (tmp_path / 'scrape.json').read_bytes() != old


@pytest.mark.asyncio
async def test_perses_conflict_never_overwrites_new_edit(tmp_path):
    pub = Publisher()
    c = Controller(settings(tmp_path), pub)
    await c.accept(catalog())
    await c.reconcile()
    path = next(k for k, v in pub.resources.items() if v['kind'] == 'Dashboard')
    pub.resources[path]['spec']['display']['description'] = 'manual edit'
    await c.reconcile()
    assert c.status()['foundations'][0]['components']['perses']['status'] == 'conflict'
    c.forced.add('new-foundation')
    pub.resources[path]['spec']['display']['description'] = 'newer manual edit'
    await c.reconcile()
    assert pub.resources[path]['spec']['display']['description'] == 'newer manual edit'
    assert c.status()['foundations'][0]['components']['perses']['status'] == 'conflict'
    await c.reconcile()
    assert pub.resources[path]['spec']['display']['description'] == 'newer manual edit'
    c.forced.add('new-foundation')
    await c.reconcile()
    assert c.status()['foundations'][0]['components']['perses']['status'] == 'ready'


@pytest.mark.asyncio
async def test_unknown_perses_write_is_reconciled_by_readback(tmp_path):
    pub = Publisher()
    pub.unknown_once = True
    c = Controller(settings(tmp_path), pub)
    await c.accept(catalog())
    await c.reconcile()
    assert c.status()['foundations'][0]['components']['perses']['status'] == 'error'
    await c.reconcile()
    assert c.status()['foundations'][0]['components']['perses']['status'] == 'ready'
    project_posts = [call for call in pub.calls if call == ('POST', '/api/v1/projects')]
    assert len(project_posts) == 1


@pytest.mark.asyncio
async def test_pause_requires_fresh_matching_worker_ack_and_resume_is_explicit(tmp_path):
    c = Controller(settings(tmp_path), Publisher())
    await c.accept(catalog())
    atomic_json(c.settings.probe_status, {'observed_at': time.time(), 'paused_routers': ['router-new'],
                                        'pause_operations': {'router-new': 'old-op'}})
    async def ack():
        await asyncio.sleep(.15)
        atomic_json(c.settings.probe_status, {'observed_at': time.time(), 'paused_routers': ['router-new'],
                                            'pause_operations': {'router-new': 'new-op'}, 'inflight': {'foundation:new-foundation': 1}})
    task = asyncio.create_task(ack())
    value = await c.pause('router-new', 'new-op')
    await task
    assert value['dispatch_stopped'] and value['inflight']
    await c.accept(catalog())
    assert c.control()['paused_routers'] == {'router-new': 'new-op'}
    await c.resume('router-new', 'restore-op')
    assert c.control()['paused_routers'] == {}


def test_scrape_import_preserves_existing_labels(tmp_path):
    body = catalog()
    endpoint = body['foundations'][0]['monitoring_endpoints'][0]
    endpoint['scrape_labels'] = {'environment': 'foundation-new', 'node': 'new-prefill', 'service': 'host'}
    endpoint['scrape_job'] = 'node-a3'
    value = scrape_config(store_credentials(body, tmp_path))
    row = value['scrape_configs'][0]
    assert 'role' not in row['static_configs'][0]['labels']
    assert row['relabel_configs'][0]['replacement'] == 'node-a3'


@pytest.mark.asyncio
async def test_repeated_resume_does_not_clear_a_new_maintenance_pause(tmp_path):
    c = Controller(settings(tmp_path), Publisher())
    await c.accept(catalog())
    await c.resume('router-new', 'restore-old')
    control = c.control()
    control['paused_routers']['router-new'] = 'maintenance-new'
    atomic_json(c.settings.control, control)
    assert (await c.resume('router-new', 'restore-old'))['resumed']
    assert c.control()['paused_routers']['router-new'] == 'maintenance-new'
    with pytest.raises(Conflict):
        await c.resume('different-router', 'restore-old')


@pytest.mark.asyncio
async def test_deleted_foundation_stops_collection_and_retains_disabled_perses(tmp_path):
    pub = Publisher()
    c = Controller(settings(tmp_path), pub)
    await c.accept(catalog())
    await c.reconcile()
    await c.accept({'schema_version': 1, 'revision': 2, 'foundations': [], 'gateways': []})
    await c.reconcile()
    active = json.loads((tmp_path / 'catalog.json').read_text())
    assert len(active['foundations']) == 1 and active['foundations'][0]['archived']
    scrape = json.loads((tmp_path / 'scrape.json').read_text())
    assert all(not j['job_name'].startswith('managed-') for j in scrape['scrape_configs'])
    project = next(v for v in pub.resources.values() if v['kind'] == 'Project')
    assert project['spec']['display']['name'].endswith('（已停用）')
    assert len([v for v in pub.resources.values() if v['kind'] == 'Dashboard']) == 7


@pytest.mark.asyncio
async def test_retired_pause_cannot_pause_again_after_resume(tmp_path):
    c = Controller(settings(tmp_path), Publisher())
    await c.accept(catalog())
    control = c.control()
    control['paused_routers']['router-new'] = 'pause-old'
    atomic_json(c.settings.control, control)
    await c.resume('router-new', 'restore-new')
    with pytest.raises(Conflict, match='pause_retired'):
        await c.pause('router-new', 'pause-old')
    assert c.control()['paused_routers'] == {}


@pytest.mark.asyncio
async def test_credential_rotation_keeps_only_applied_and_desired_secrets(tmp_path):
    c = Controller(settings(tmp_path), Publisher())
    first = catalog()
    first['foundations'][0]['monitoring_endpoints'][0]['auth']['password'] = 'space\r\nsecret '
    await c.accept(first)
    original = next((tmp_path / 'secrets').iterdir())
    assert base64.b64decode(original.read_bytes()) == b'reader:space\r\nsecret '
    await c.reconcile()
    changed = catalog(2)
    await c.accept(changed)
    assert len(list((tmp_path / 'secrets').iterdir())) == 2
    await c.reconcile()
    assert len(list((tmp_path / 'secrets').iterdir())) == 1
    assert not original.exists()


@pytest.mark.asyncio
async def test_monitoring_origin_check_validates_family_and_hides_auth(tmp_path):
    from monitoring.controller import Publisher as HTTPPublisher
    body = store_credentials(catalog(), tmp_path)
    endpoint = body['foundations'][0]['monitoring_endpoints'][0]
    seen = []
    async def handler(request):
        seen.append(request.headers.get('authorization'))
        return httpx.Response(200, text='sglang:num_requests_total 1\n')
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    pub = HTTPPublisher(settings(tmp_path), client)
    result = await pub.check_endpoint(endpoint, 'dcu-sglang-v1')
    assert seen[0].startswith('Basic ')
    assert result['format_valid'] and result['status'] == 'mismatch'
    assert result['origin'] == 'monitoring-controller'
    assert 'private-secret' not in json.dumps(result)
    await pub.close()


@pytest.mark.asyncio
async def test_configuration_drift_is_not_reported_ready(tmp_path):
    pub = Publisher()
    c = Controller(settings(tmp_path), pub)
    await c.accept(catalog())
    await c.reconcile()
    value = json.loads((tmp_path / 'scrape.json').read_text())
    value['scrape_configs'] = []
    atomic_json(tmp_path / 'scrape.json', value)
    await c.reconcile()
    assert c.status()['foundations'][0]['components']['scrape']['error'] == 'scrape_configuration_drift'
    assert json.loads((tmp_path / 'scrape.json').read_text())['scrape_configs'] == []


def test_control_api_auth_tls_and_secret_safe_validation(tmp_path):
    from fastapi.testclient import TestClient
    cfg = settings(tmp_path)
    cfg.require_tls = True
    pub = Publisher()
    app = create_app(cfg, pub)
    with TestClient(app, base_url='https://controller.test', client=('127.0.0.1', 50000)) as client:
        assert client.get('/health').status_code == 200
        assert client.get('/internal/monitoring/v1/integrations').status_code == 401
        headers = {'Authorization': 'Bearer ' + cfg.token}
        invalid = catalog()
        invalid['foundations'][0]['monitoring_endpoints'][0]['auth']['type'] = 'unsupported'
        response = client.put('/internal/monitoring/v1/catalog', json=invalid, headers=headers)
        assert response.status_code == 422 and 'private-secret' not in response.text
        assert client.put('/internal/monitoring/v1/catalog', json=catalog(), headers=headers).status_code == 200
        response = client.get('/internal/monitoring/v1/integrations', headers=headers)
        assert response.status_code == 200 and 'private-secret' not in response.text
        assert client.post('/internal/monitoring/v1/reconcile', json={'expected_revision': 99}, headers=headers).status_code == 409
        assert client.get('http://controller.test/internal/monitoring/v1/integrations', headers=headers).status_code == 403


@pytest.mark.asyncio
async def test_crash_after_desired_commit_rebuilds_public_probe_catalog(tmp_path):
    c = Controller(settings(tmp_path), Publisher())
    await c.accept(catalog())
    state = c.state.copy()
    state.update(accepted_revision=0, foundations=[], gateways=[])
    atomic_json(c.journal_path, state)
    (tmp_path / 'probes-catalog.json').unlink()
    recovered = Controller(settings(tmp_path), Publisher())
    assert recovered.state['accepted_revision'] == 1
    assert recovered.state['gateways'][0]['gateway_id'] == 'entry-1'
    assert json.loads((tmp_path / 'probes-catalog.json').read_text())['revision'] == 1
    await recovered.accept(catalog())
    assert recovered.control()['revision'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('adapter,name', [
    ('dcu-sglang-v1', 'sglang_prefill_effective_tokens_total'),
    ('xpu-sglang-v1', 'sglang_num_requests_total'),
    ('dcu-sglang-v1', 'sglang:num_requests_total'),
    ('a3-vllm-v1', 'vllm_request_success_total'),
    ('a3-vllm-v1', 'vllm:request_success_total'),
])
async def test_inference_check_accepts_native_and_openmetrics_escaped_names(tmp_path, adapter, name):
    from monitoring.controller import Publisher as HTTPPublisher
    endpoint = {'type': 'inference', 'url': 'http://exporter.invalid/metrics', 'auth': {'type': 'none'}}
    async def handler(request):
        assert 'application/openmetrics-text' in request.headers['accept']
        return httpx.Response(200, headers={'Content-Type':
            'application/openmetrics-text; version=1.0.0; charset=utf-8; escaping=underscores'},
            text='# TYPE ' + name.removesuffix('_total') + ' counter\n' + name + '{engine_type="prefill"} 1.0\n# EOF\n')
    publisher = HTTPPublisher(settings(tmp_path), httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    result = await publisher.check_endpoint(endpoint, adapter)
    await publisher.close()
    assert result['status'] == 'passed'
    assert result['format_valid'] and result['type_matches']
    assert result['metric_names'] == [name]


@pytest.mark.asyncio
async def test_gateway_status_distinguishes_unmanaged_disabled_and_worker_pending(tmp_path):
    from monitoring.probe_catalog import targets
    c = Controller(settings(tmp_path), Publisher())
    body = catalog()
    prototype = body['gateways'][0]
    body['gateways'].extend([
        {**prototype, 'id': 'ppu', 'router_id': 'ppu-unmanaged', 'admission': False},
        {**prototype, 'id': 'disabled', 'enabled': False},
        {**prototype, 'id': 'unbound', 'router_id': None},
        {**prototype, 'id': 'awaiting-worker'},
        {**prototype, 'id': 'closed-entry', 'admission': False},
    ])
    await c.accept(body)
    atomic_json(c.settings.probe_status, {'observed_at': time.time(), 'catalog_revision': 1,
        'target_ids': ['foundation:new-foundation', 'gateway:entry-1', 'gateway:disabled', 'gateway:closed-entry']})
    await c.reconcile()
    statuses = {g['gateway_id']: g['status'] for g in c.status()['gateways']}
    assert statuses == {'entry-1': 'ready', 'ppu': 'not_configured', 'disabled': 'disabled',
                        'unbound': 'not_configured', 'awaiting-worker': 'pending', 'closed-entry': 'ready'}
    # Reporting a terminal unconfigured state never opts the gateway into probes.
    dispatch = targets(value=body, control=c.control())
    assert all(target['probe_id'] != 'gateway:ppu' for target in dispatch)
