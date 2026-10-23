"""Opt-in deletion contract against the pinned real Perses version, local only."""
import asyncio
import copy
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

import httpx
import pytest

from monitoring.controller import Controller, Publisher, Settings
from monitoring.provisioning import atomic_json

pytestmark = pytest.mark.skipif(os.environ.get('MONITORING_PERSES_E2E') != '1',
                               reason='Opt-in disposable real Perses required')
ROOT = Path(__file__).resolve().parents[1]


class PersesOnlyPublisher(Publisher):
    # Native collection is exercised with real VM/vmagent/vmalert separately.
    async def dry_run(self, component, path):
        pass

    async def reload(self, component, value):
        pass

    async def readback(self, component, value):
        pass

    async def scrape_status(self):
        return {}


@pytest.mark.asyncio
async def test_real_perses_retirement_deletes_manual_content_and_keeps_other_project(tmp_path):
    name = 'monitoring-perses-retirement-test-' + uuid.uuid4().hex[:10]
    lock = json.loads((ROOT / 'perses/image-lock.json').read_text())
    image = 'persesdev/perses@' + lock['platform_manifest_digest']
    config = tmp_path / 'config.yaml'
    config.write_text('security:\n  enable_auth: false\n  readonly: false\n'
                      '  encryption_key: 0123456789abcdef0123456789abcdef\n'
                      'database:\n  file:\n    folder: /perses\n    extension: json\n')
    config.chmod(0o644)
    def docker(*args):
        return subprocess.check_output(['docker', *args], text=True, stderr=subprocess.STDOUT).strip()
    docker('image', 'inspect', image)
    created = False
    publisher = None
    try:
        docker('run', '-d', '--name', name, '--platform', lock['platform'], '--cpus', '1', '--memory', '512m',
               '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
               '--tmpfs', '/perses:rw,nosuid,nodev,mode=0777,size=67108864', '-p', '127.0.0.1::8080',
               '-v', str(config) + ':/etc/perses/config.yaml:ro', image,
               '--config=/etc/perses/config.yaml', '--web.listen-address=:8080')
        created = True
        identity = json.loads(docker('inspect', name))[0]
        port = identity['NetworkSettings']['Ports']['8080/tcp'][0]['HostPort']
        base = 'http://127.0.0.1:' + port
        publisher = PersesOnlyPublisher(Settings(state=tmp_path, catalog=tmp_path/'catalog.json',
            control=tmp_path/'control.json', probe_status=tmp_path/'probes-runtime.json', token='test-only-'*5,
            allowed_clients={'127.0.0.1'}, templates=ROOT/'perses', perses=base, require_tls=False))
        for _ in range(100):
            try:
                await publisher.perses_request('GET', '/api/v1/projects')
                break
            except httpx.HTTPError:
                await asyncio.sleep(.2)
        else:
            pytest.fail('Real Perses did not become ready')
        c = Controller(publisher.settings, publisher)
        body = {'schema_version': 1, 'revision': 1, 'gateways': [], 'foundations': [{
            'id': 'fixture', 'name': 'Retirement fixture', 'enabled': True, 'monitoring_source': 'fixture-source',
            'monitoring_adapter': 'a3-vllm-v1', 'resource_nodes': [], 'monitoring_endpoints': [], 'router': None}]}
        await c.accept(body)
        await c.reconcile()
        assert c.status()['foundations'][0]['components']['perses']['status'] == 'ready', c.status()
        root = '/api/v1/projects/foundation-fixture'
        dashboards = await publisher.perses_request('GET', root + '/dashboards')
        assert dashboards
        manual = copy.deepcopy(dashboards[0])
        manual['metadata'] = {'name': 'manual-dashboard', 'project': 'foundation-fixture'}
        await publisher.perses_request('POST', root + '/dashboards', manual)
        datasource = copy.deepcopy((await publisher.perses_request('GET', root + '/datasources'))[0])
        datasource['metadata'] = {'name': 'manual-datasource', 'project': 'foundation-fixture'}
        datasource['spec']['default'] = False
        await publisher.perses_request('POST', root + '/datasources', datasource)
        other = {'kind': 'Project', 'metadata': {'name': 'other-project'}, 'spec': {'display': {'name': 'Other'}}}
        await publisher.perses_request('POST', '/api/v1/projects', other)
        datasource['metadata'] = {'name': 'other-datasource', 'project': 'other-project'}
        await publisher.perses_request('POST', '/api/v1/projects/other-project/datasources', datasource)
        body['revision'] = 2
        body['foundations'][0]['enabled'] = False
        await c.accept(body)
        atomic_json(c.settings.probe_status, {'observed_at': time.time(), 'catalog_revision': 2, 'target_ids': [], 'inflight': {}})
        await c.reconcile()
        assert c.status()['foundations'][0]['status'] == 'disabled', c.status()
        assert await publisher.perses_request('GET', root) is None
        assert await publisher.perses_request('GET', '/api/v1/projects/other-project') is not None
        assert await publisher.perses_request('GET', '/api/v1/projects/other-project/datasources/other-datasource') is not None
        await c.reconcile()
        assert await publisher.perses_request('GET', root) is None
        atomic_json(tmp_path / 'result.json', {'passed': True, 'image': identity['Image'], 'reference': image,
            'manual_dashboard_removed': True, 'other_project_preserved': True, 'status': c.status()})
    finally:
        if publisher:
            await publisher.close()
        if created:
            (tmp_path / 'perses.log').write_text(docker('logs', name))
            docker('rm', '-f', name)
