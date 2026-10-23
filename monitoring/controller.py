"""Authenticated desired-state reconciler for the one monitoring installation.

The platform owns the catalog. This process owns generated files and publication
journals. Query API processes never write configuration or decrypt credentials.
"""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
import copy
import hmac
import json
import os
import re
from pathlib import Path
import ssl
import subprocess
import time

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .access import client_allowlist
from .provisioning import (atomic_json, canonical, digest, foundation_errors, perses_resources,
                           project_name, public_catalog, scrape_config, store_credentials, validate_catalog)


class Conflict(Exception):
    pass


class Superseded(Exception):
    pass


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return copy.deepcopy(default)


@dataclass
class Settings:
    state: Path
    catalog: Path
    control: Path
    probe_status: Path
    token: str
    allowed_clients: set
    templates: Path
    vmagent: str = 'http://127.0.0.1:18429'
    vmalert: str = 'http://127.0.0.1:18432'
    vm: str = 'http://127.0.0.1:18428'
    perses: str = 'https://127.0.0.1:18431'
    vmagent_binary: str = '/vmagent-prod'
    vmalert_binary: str = '/vmalert-prod'
    credentials: Path | None = None
    ca_file: str | None = None
    require_tls: bool = True

    @classmethod
    def environment(cls):
        state = Path(os.environ.get('STATE_DIR', '/state'))
        token_path = os.environ.get('MONITOR_CONTROL_TOKEN_FILE')
        token = Path(token_path).read_text().strip() if token_path else os.environ.get('MONITOR_CONTROL_TOKEN', '')
        if not token or len(token) < 32 or any(c.isspace() for c in token):
            raise ValueError('Configure a dedicated controller token of at least 32 characters')
        credentials = os.environ.get('PERSES_CREDENTIALS_FILE')
        return cls(state=state, catalog=Path(os.environ.get('MONITORING_CATALOG_FILE', state / 'catalog.json')),
                   control=Path(os.environ.get('MONITORING_PROBE_CONTROL_FILE', state / 'probes-control.json')),
                   probe_status=Path(os.environ.get('MONITORING_PROBE_STATUS_FILE', state / 'probes-runtime.json')),
                   token=token, allowed_clients=set(client_allowlist(os.environ.get('ALLOWED_CLIENTS', '127.0.0.1,::1'))),
                   templates=Path(os.environ.get('PERSES_TEMPLATES_DIR', Path(__file__).resolve().parents[1] / 'perses')),
                   vmagent=os.environ.get('VMAGENT_URL', 'http://127.0.0.1:18429').rstrip('/'),
                   vmalert=os.environ.get('VMALERT_URL', 'http://127.0.0.1:18432').rstrip('/'),
                   vm=os.environ.get('VM_URL', 'http://127.0.0.1:18428').rstrip('/'),
                   perses=os.environ.get('PERSES_URL', 'https://127.0.0.1:18431').rstrip('/'),
                   vmagent_binary=os.environ.get('VMAGENT_BINARY', '/vmagent-prod'),
                   vmalert_binary=os.environ.get('VMALERT_BINARY', '/vmalert-prod'),
                   credentials=Path(credentials) if credentials else None, ca_file=os.environ.get('PERSES_CA_FILE'))


class Publisher:
    """Narrow side-effect boundary, injectable for full lifecycle tests."""
    def __init__(self, settings, client=None):
        self.settings = settings
        verify = ssl.create_default_context(cafile=settings.ca_file) if settings.ca_file else True
        self.client = client or httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=10, verify=verify)
        self.token = None

    async def close(self):
        await self.client.aclose()

    async def dry_run(self, component, path):
        args = ([self.settings.vmagent_binary, '-promscrape.config=' + str(path), '-promscrape.config.dryRun']
                if component == 'scrape' else [self.settings.vmalert_binary, '-rule=' + str(path), '-dryRun'])
        process = await asyncio.create_subprocess_exec(*args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            await asyncio.wait_for(process.wait(), 20)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise RuntimeError(component + '_validation_timeout') from None
        if process.returncode:
            # Native error text may include a credential path or URL. Public status
            # uses a bounded code; generated candidate remains local evidence.
            raise RuntimeError(component + '_validation_failed')

    async def reload(self, component, value):
        base = self.settings.vmagent if component == 'scrape' else self.settings.vmalert
        response = await self.client.get(base + '/-/reload')
        response.raise_for_status()
        await self.readback(component, value)

    async def readback(self, component, value):
        # A 200 reload response is not proof that the candidate was accepted.
        base = self.settings.vmagent if component == 'scrape' else self.settings.vmalert
        response = await self.client.get(base + ('/api/v1/targets' if component == 'scrape' else '/api/v1/rules'))
        response.raise_for_status()
        body = response.json()
        if body.get('status') != 'success':
            raise RuntimeError(component + '_readback_failed')
        if component == 'scrape':
            targets = body.get('data', {}).get('activeTargets', [])
            actual = {(r.get('scrapePool') or r.get('discoveredLabels', {}).get('job'), r.get('labels', {}).get('environment'), r.get('labels', {}).get('job'), r.get('scrapeUrl')) for r in targets}
            expected = set()
            for job in value['scrape_configs']:
                labels = job['static_configs'][0]['labels']
                real_job = next((r['replacement'] for r in job.get('relabel_configs', []) if r.get('target_label') == 'job'), job['job_name'])
                for target in job['static_configs'][0]['targets']:
                    expected.add((job['job_name'], labels.get('environment'), real_job,
                                  job.get('scheme', 'http') + '://' + target + job.get('metrics_path', '/metrics')))
            if expected != actual:
                raise RuntimeError('scrape_readback_pending')
        else:
            groups = body.get('data', {}).get('groups', [])
            actual = {g['name']: sorted(canonical({'name': r.get('name'), 'expr': r.get('query'), 'labels': r.get('labels') or {}}) for r in g.get('rules', [])) for g in groups}
            expected = {g['name']: sorted(canonical({'name': r['record'], 'expr': r['expr'], 'labels': {**g.get('labels', {}), **r.get('labels', {})}}) for r in g['rules']) for g in value['groups']}
            if actual != expected or any(r.get('lastError') for g in groups for r in g.get('rules', [])):
                raise RuntimeError('rules_readback_pending')

    async def scrape_status(self):
        response = await self.client.get(self.settings.vmagent + '/api/v1/targets')
        response.raise_for_status()
        body = response.json()
        if body.get('status') != 'success':
            raise RuntimeError('scrape_status_unavailable')
        result = {}
        for target in body.get('data', {}).get('activeTargets', []):
            stamp = target.get('lastScrape')
            try:
                stamp = datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp() if isinstance(stamp, str) else float(stamp)
            except (TypeError, ValueError, OverflowError):
                stamp = None
            fresh = stamp is not None and 0 <= time.time() - stamp <= 15
            result[(target.get('labels', {}).get('environment'), target.get('scrapeUrl'))] = {
                'collection_status': 'healthy' if target.get('health') == 'up' and fresh else 'stale' if target.get('health') == 'up' else 'unavailable',
                'last_scrape_at': stamp, 'scrape_error': 'exporter_scrape_failed' if target.get('lastError') else None}
        return result

    async def check_endpoint(self, endpoint, adapter):
        started = time.monotonic()
        result = {'origin': 'monitoring-controller', 'checked_at': time.time(), 'http_status': None,
                  'reachable': False, 'format_valid': False, 'type_matches': False, 'status': 'error'}
        auth = endpoint.get('auth') or {}
        headers = {'Accept': 'application/openmetrics-text;version=1.0.0, text/plain;version=0.0.4;q=0.9'}
        if auth.get('type') == 'basic':
            headers['Authorization'] = 'Basic ' + Path(auth['authorization_file']).read_bytes().decode('ascii')
        try:
            async with asyncio.timeout(8):
                async with self.client.stream('GET', endpoint['url'], timeout=8, headers=headers) as response:
                    result.update(http_status=response.status_code, reachable=True)
                    if response.status_code != 200:
                        result['error'] = 'exporter_http_' + str(response.status_code)
                        return result
                    raw = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        raw.extend(chunk)
                        if len(raw) > 4 * 1024 * 1024:
                            result['error'] = 'exporter_response_too_large'
                            return result
                    names = set()
                    # Native scraping remains the final exposition parser. This
                    # bounded check independently identifies metric families from
                    # the monitoring host, including OpenMetrics exemplars.
                    number = r'(?:[-+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][-+]?[0-9]+)?|[-+]?Inf|NaN)'
                    pattern = re.compile(r'^([A-Za-z_:][A-Za-z0-9_:]*)(?:\{.*\})?\s+' + number + r'(?:\s+' + number + r')?(?:\s+#.*)?$')
                    for line in raw.decode('utf-8-sig').splitlines():
                        line = line.strip()
                        if not line or line.startswith('#'):
                            continue
                        match = pattern.fullmatch(line)
                        if not match:
                            result['error'] = 'invalid_metrics_format'
                            return result
                        names.add(match[1])
                    if not names:
                        result['error'] = 'empty_metrics'
                        return result
                    prefixes = {'host': ('node_',),
                                'accelerator': {'dcu-sglang-v1': ('dcu_',), 'a3-vllm-v1': ('npu_', 'machine_npu_nums'), 'xpu-sglang-v1': ('node_xpu_',)}[adapter],
                                # OpenMetrics escaping=underscores can normalize namespace colons.
                                'inference': ('vllm:', 'vllm_') if adapter == 'a3-vllm-v1' else ('sglang:', 'sglang_'),
                                'mooncake': ('master_', 'segment_', 'total_get_nums_', 'valid_get_nums_', 'mem_cache_hit_nums_', 'file_cache_hit_nums_')}
                    matched = sorted(n for n in names if n.startswith(prefixes[endpoint['type']]))
                    result.update(format_valid=True, type_matches=bool(matched), metric_count=len(names), metric_names=matched[:20],
                                  status='passed' if matched else 'mismatch')
                    if not matched:
                        result['error'] = 'exporter_type_mismatch'
        except (TimeoutError, httpx.TimeoutException):
            result['error'] = 'exporter_check_timeout'
        except (httpx.HTTPError, OSError, UnicodeError, ValueError):
            result['error'] = 'exporter_check_failed'
        finally:
            result['elapsed_ms'] = round((time.monotonic() - started) * 1000)
        return result

    async def perses_request(self, method, path, body=None):
        if self.settings.credentials and self.token is None:
            credentials = read_json(self.settings.credentials)
            response = await self.client.post(self.settings.perses + '/api/auth/providers/native/login', json=credentials)
            response.raise_for_status()
            self.token = response.json()['access_token']
        headers = {'Authorization': 'Bearer ' + self.token} if self.token else {}
        response = await self.client.request(method, self.settings.perses + path, json=body, headers=headers)
        if response.status_code == 401:
            self.token = None
        if method in ('GET', 'DELETE') and response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json() if response.content else None


class Controller:
    def __init__(self, settings, publisher=None):
        self.settings = settings
        settings.state.mkdir(parents=True, exist_ok=True)
        self.desired_path = settings.state / 'desired.json'
        self.journal_path = settings.state / 'integration-state.json'
        self.desired = read_json(self.desired_path, {})
        self.state = read_json(self.journal_path, {'accepted_revision': 0, 'applied_revision': 0,
                                                'foundations': [], 'gateways': [], 'resources': {}})
        self.publisher = publisher or Publisher(settings)
        self.event = asyncio.Event()
        self.lock = asyncio.Lock()
        self.worker_lock = asyncio.Lock()
        self.forced = set()
        if self.desired and self.desired['catalog']['revision'] > self.state['accepted_revision']:
            revision = self.desired['catalog']['revision']
            self.state['accepted_revision'] = revision
            self.state['foundations'] = [self.new_foundation(f, revision) for f in self.desired['catalog']['foundations']]
            self.state['gateways'] = [{'gateway_id': g['id'], 'status': 'pending' if g.get('enabled', True) else 'disabled'} for g in self.desired['catalog']['gateways']]
            self.save()
        if self.desired:
            atomic_json(self.settings.state / 'probes-catalog.json', public_catalog(self.desired['catalog']))
            # Re-verify old disabled registrations on upgrade: earlier versions
            # deliberately retained their Perses projects.
            for foundation in self.desired['catalog']['foundations']:
                if not foundation.get('enabled', True):
                    for row in self.state['foundations']:
                        if row['foundation_id'] == foundation['id']:
                            row.update(status='pending', components={name: {'status': 'pending'}
                                       for name in ('scrape', 'rules', 'perses', 'probes')})
                    self.state['applied_revision'] = min(self.state['applied_revision'], self.state['accepted_revision'] - 1)
            self.save()
            self.event.set()

    def save(self):
        self.state['updated_at'] = time.time()
        atomic_json(self.journal_path, self.state)

    def status(self):
        return {key: copy.deepcopy(value) for key, value in self.state.items() if key not in ('resources', 'endpoint_checks', 'configurations', 'pending_configurations')} | {
            'revision': self.state['accepted_revision']}

    def control(self):
        return read_json(self.settings.control, {'revision': 0, 'heartbeat_at': 0, 'paused_routers': {}})

    async def accept(self, body):
        body = validate_catalog(body)
        signature = digest(body)
        async with self.lock:
            revision, current = body['revision'], self.state['accepted_revision']
            if revision < current:
                raise Conflict('stale_catalog_revision')
            if revision == current and signature != self.desired.get('digest'):
                raise Conflict('catalog_revision_conflict')
            if revision > current:
                safe = store_credentials(body, self.settings.state)
                live_ids = {f['id'] for f in safe['foundations']}
                for prior in self.desired.get('catalog', {}).get('foundations', []):
                    if prior['id'] not in live_ids:
                        safe['foundations'].append({**copy.deepcopy(prior), 'enabled': False, 'archived': True})
                live_gateways = {g['id'] for g in safe['gateways']}
                for prior in self.desired.get('catalog', {}).get('gateways', []):
                    if prior['id'] not in live_gateways:
                        safe['gateways'].append({**copy.deepcopy(prior), 'enabled': False, 'archived': True})
                self.desired = {'catalog': safe, 'digest': signature}
                atomic_json(self.desired_path, self.desired)
                # Desired file is the crash-recovery authority. It is committed
                # before the public directory or accepted-version status changes.
                atomic_json(self.settings.state / 'probes-catalog.json', public_catalog(safe))
                self.state['accepted_revision'] = revision
                self.state['foundations'] = [self.new_foundation(f, revision) for f in safe['foundations']]
                self.state['gateways'] = [{'gateway_id': g['id'], 'status': 'pending' if g.get('enabled', True) else 'disabled'} for g in safe['gateways']]
                self.save()
                self.event.set()
            control = self.control()
            control.update(revision=revision, heartbeat_at=time.time())
            atomic_json(self.settings.control, control)
        return {'accepted_revision': revision, 'applied_revision': self.state['applied_revision']}

    def new_foundation(self, foundation, revision):
        return {'foundation_id': foundation['id'], 'monitoring_source': foundation.get('monitoring_source'),
                'name': foundation.get('name', foundation['id']), 'status': 'pending',
                'desired_revision': revision, 'components': {name: {'status': 'pending'}
                                                         for name in ('scrape', 'rules', 'perses', 'probes')},
                'perses_url': self.settings.perses + '/projects/' + project_name(foundation) if foundation.get('monitoring_source') else None}

    def current(self, revision):
        if self.state['accepted_revision'] != revision:
            raise Superseded()

    def mark(self, revision, component, status, *, fid=None, error=None):
        self.current(revision)
        definitions = {f['id']: f for f in self.desired['catalog']['foundations']}
        for foundation in self.state['foundations']:
            if fid and foundation['foundation_id'] != fid:
                continue
            retired = not definitions[foundation['foundation_id']].get('enabled', True)
            applied_status = 'disabled' if retired and status == 'ready' and component in ('scrape', 'rules') else status
            value = {'status': applied_status, 'applied_revision': revision if applied_status in ('ready', 'disabled') else None}
            if error:
                value['error'] = error
            foundation['components'][component] = value
            states = [v['status'] for v in foundation['components'].values()]
            foundation['status'] = ('disabled' if retired and all(s == 'disabled' for s in states) else
                                    'ready' if all(s in ('ready', 'not_configured') for s in states) else
                                    'conflict' if 'conflict' in states else
                                    'error' if 'error' in states else
                                    'partial' if any(s in ('ready', 'disabled') for s in states) else 'pending')
            if retired and foundation['components']['perses']['status'] == 'disabled':
                foundation['perses_url'] = None
        self.save()

    async def apply_config(self, revision, component, value):
        candidate = self.settings.state / (component + '.candidate.json')
        target = self.settings.state / ('scrape.json' if component == 'scrape' else 'rules.json')
        signature = digest(value)
        previous = self.state.get('configurations', {}).get(component)
        pending = self.state.get('pending_configurations', {}).get(component)
        if previous and target.exists() and digest(read_json(target)) not in (previous, pending):
            raise Conflict(component + '_configuration_drift')
        if previous == signature and target.exists():
            await self.publisher.readback(component, value)
            self.mark(revision, component, 'ready')
            return
        atomic_json(candidate, value)
        await self.publisher.dry_run(component, candidate)
        self.current(revision)
        self.state.setdefault('pending_configurations', {})[component] = signature
        self.save()
        atomic_json(target, value)
        await self.publisher.reload(component, value)
        self.state.setdefault('configurations', {})[component] = signature
        self.state.get('pending_configurations', {}).pop(component, None)
        self.mark(revision, component, 'ready')
        if component == 'scrape':
            self.clean_credentials(value)

    def clean_credentials(self, active_scrape):
        # Only remove this controller's unreferenced private files after native
        # readback proves the active collector no longer depends on them.
        referenced = {j.get('authorization', {}).get('credentials_file') for j in active_scrape['scrape_configs']}
        referenced.update(j.get('basic_auth', {}).get('password_file') for j in active_scrape['scrape_configs'])
        for foundation in self.desired.get('catalog', {}).get('foundations', []):
            if foundation.get('archived'):
                continue
            referenced.update(e.get('auth', {}).get('authorization_file') for e in foundation.get('monitoring_endpoints', []))
        root = self.settings.state / 'secrets'
        if root.exists():
            for path in root.glob('*.secret'):
                if not path.is_symlink() and path.is_file() and str(path) not in referenced:
                    path.unlink()

    @staticmethod
    def resource_path(document):
        kind, metadata = document['kind'], document['metadata']
        prefix = '/api/v1'
        if kind != 'Project':
            prefix += '/projects/' + metadata['project']
        return prefix + '/' + {'Project': 'projects', 'Datasource': 'datasources', 'Dashboard': 'dashboards'}[kind]

    @staticmethod
    def resource_spec(document):
        if document is None:
            return None
        value = copy.deepcopy(document['spec'])
        if document['kind'] == 'Dashboard':
            value.setdefault('variables', [])
            for variable in value['variables']:
                spec = variable['spec']
                spec.setdefault('allowAllValue', False)
                spec.setdefault('allowMultiple', False)
                spec.setdefault('display', {}).setdefault('hidden', False)
        return value

    async def publish_resources(self, revision, foundation, resources):
        if not foundation.get('enabled', True):
            raise Conflict('retired_foundation_cannot_publish')
        owner = foundation['id']
        forced = {path: record.get('conflict_digest') for path, record in self.state['resources'].items()
                  if owner in self.forced and record.get('owner') == owner and record.get('conflict_digest')}
        self.forced.discard(owner)

        for category in ('projects', 'datasources', 'dashboards'):
            for document in resources[category]:
                self.current(revision)
                collection = self.resource_path(document)
                path = collection + '/' + document['metadata']['name']
                current = await self.publisher.perses_request('GET', path)
                desired_spec = self.resource_spec(document)
                current_digest = digest(self.resource_spec(current))
                desired_digest = digest(desired_spec)
                record = self.state['resources'].get(path)
                if current is not None and current_digest != desired_digest:
                    allowed = record and record.get('owner') == owner and current_digest in (record.get('applied_digest'), record.get('candidate_digest'))
                    authorized = forced.get(path) == current_digest
                    if not allowed and not authorized:
                        self.state['resources'][path] = {**(record or {}), 'owner': owner, 'conflict_digest': current_digest}
                        self.save()
                        raise Conflict('perses_concurrent_edit')
                # A failed/unknown PUT is reconciled through GET before another
                # write. Persist intent before mutation, preserving prior owner.
                if current_digest != desired_digest:
                    self.state['resources'][path] = {'owner': owner, 'applied_digest': current_digest,
                                                     'candidate_digest': desired_digest, 'revision': revision}
                    self.save()
                    self.current(revision)
                    again = await self.publisher.perses_request('GET', path)
                    if digest(self.resource_spec(again)) != current_digest:
                        raise Conflict('perses_concurrent_edit')
                    if current is None:
                        await self.publisher.perses_request('POST', collection, document)
                    else:
                        await self.publisher.perses_request('PUT', path, {**current, 'spec': document['spec']})
                    actual = await self.publisher.perses_request('GET', path)
                    if digest(self.resource_spec(actual)) != desired_digest:
                        raise RuntimeError('perses_readback_mismatch')
                self.state['resources'][path] = {'owner': owner, 'applied_digest': desired_digest, 'revision': revision}
                self.save()
        self.forced.discard(owner)

    async def retire_resources(self, revision, foundation):
        """Delete dedicated projects, including custom content, with durable evidence.

        Ownership conflicts are checked for the entire project before any write.
        Deleted resource records remain as tombstones for restart/readback.
        """
        owner = foundation['id']
        prefix = '/api/v1/projects/'
        projects = {path[len(prefix):].split('/')[0] for path, record in self.state['resources'].items()
                    if path.startswith(prefix) and record.get('owner') == owner}
        if foundation.get('monitoring_source'):
            projects.add(project_name(foundation))
        for project in sorted(projects):
            root = prefix + project
            if any(record.get('owner') != owner for path, record in self.state['resources'].items()
                   if path == root or path.startswith(root + '/')):
                raise Conflict('perses_project_ownership_conflict')
            if any(f['id'] != owner and f.get('monitoring_source') and project_name(f) == project
                   for f in self.desired['catalog']['foundations']):
                raise Conflict('perses_project_ownership_conflict')
        for project in sorted(projects):
            self.current(revision)
            root = prefix + project
            document = await self.publisher.perses_request('GET', root)
            if document is None:
                for path, record in self.state['resources'].items():
                    if path == root or path.startswith(root + '/'):
                        record.update(deleted=True, deleting=False, revision=revision)
                self.save()
                continue
            if (document.get('kind') != 'Project' or document.get('metadata', {}).get('name') != project
                    or self.state['resources'].get(root, {}).get('owner') != owner):
                raise Conflict('perses_project_ownership_conflict')
            resources = []
            for category, kind in (('dashboards', 'Dashboard'), ('datasources', 'Datasource')):
                items = await self.publisher.perses_request('GET', root + '/' + category)
                if not isinstance(items, list):
                    raise RuntimeError('perses_inventory_invalid')
                for item in items:
                    if item.get('kind') != kind or item.get('metadata', {}).get('project') != project:
                        raise Conflict('perses_project_ownership_conflict')
                    resources.append((root + '/' + category + '/' + item['metadata']['name'], item))
            resources.append((root, document))
            evidence = {'revision': revision, 'foundation_id': owner, 'project': project,
                        'resources': [{'path': path, 'document': item, 'digest': digest(item)} for path, item in resources]}
            # Content-addressed snapshots survive subsequent retries and are not
            # returned from the public integrations endpoint.
            atomic_json(self.settings.state / 'retirement-evidence' / (digest(evidence) + '.json'), evidence)
            for path, item in resources:
                self.current(revision)
                current = await self.publisher.perses_request('GET', path)
                if current is not None and digest(current) != digest(item):
                    raise Conflict('perses_concurrent_edit')
                if path == root:
                    # A new child created during cleanup must be inventoried on
                    # the next pass, never removed by a cascading project delete.
                    for category in ('dashboards', 'datasources'):
                        if await self.publisher.perses_request('GET', root + '/' + category):
                            raise Conflict('perses_concurrent_edit')
                self.state['resources'][path] = {**self.state['resources'].get(path, {}),
                    'owner': owner, 'delete_digest': digest(item), 'revision': revision, 'deleting': True}
                self.save()
                if current is not None:
                    self.current(revision)
                    await self.publisher.perses_request('DELETE', path)
                if await self.publisher.perses_request('GET', path) is not None:
                    raise RuntimeError('perses_delete_readback_pending')
                self.state['resources'][path].update(deleting=False, deleted=True)
                self.save()
            if await self.publisher.perses_request('GET', root) is not None:
                raise RuntimeError('perses_delete_readback_pending')

    async def observe_endpoints(self, catalog, revision):
        checks = self.state.setdefault('endpoint_checks', {})
        semaphore = asyncio.Semaphore(4)
        async def check(foundation, endpoint):
            identity = endpoint['id']
            signature = digest(endpoint)
            previous = checks.get(identity, {})
            if previous.get('signature') == signature and time.time() - previous.get('checked_at', 0) < 60:
                return identity, previous
            async with semaphore:
                result = await self.publisher.check_endpoint(endpoint, foundation['monitoring_adapter'])
            return identity, {**result, 'signature': signature}
        work = [check(f, e) for f in catalog['foundations'] if f.get('enabled', True) and f.get('monitoring_adapter')
                for e in f.get('monitoring_endpoints', []) if e.get('enabled', True)]
        for identity, value in await asyncio.gather(*work):
            checks[identity] = value
        self.current(revision)
        # Exporter checks alone do not establish collection health. Keep the
        # previous complete public observation while component reconciliation
        # and scrape readback await I/O; publish both observations together below.
        self.save()
        return {identity for identity, check in checks.items() if check.get('status') == 'mismatch'}

    async def observe_collection(self, catalog, revision):
        try:
            observations = await self.publisher.scrape_status()
        except (httpx.HTTPError, RuntimeError, ValueError, TypeError):
            observations = {}
        self.current(revision)
        checks = self.state.get('endpoint_checks', {})
        definitions = {f['id']: f for f in catalog['foundations']}
        for foundation in self.state['foundations']:
            definition = definitions[foundation['foundation_id']]
            states = [{'endpoint_id': e['id'], 'name': e.get('name', e['id']),
                       **{k: v for k, v in checks.get(e['id'], {}).items() if k != 'signature'},
                       **observations.get((definition.get('monitoring_source'), e['url']),
                                          {'collection_status': 'unavailable', 'last_scrape_at': None})}
                      for e in definition.get('monitoring_endpoints', []) if e.get('enabled', True)]
            foundation['endpoints'] = states
            healthy = sum(e.get('status') == 'passed' and e.get('collection_status') == 'healthy' for e in states)
            foundation['data_status'] = 'healthy' if states and healthy == len(states) else 'partial' if healthy else 'unavailable'
        self.save()

    async def reconcile(self):
        async with self.worker_lock:
            if not self.desired:
                return
            catalog = copy.deepcopy(self.desired['catalog'])
            revision = catalog['revision']
            self.current(revision)
            usable = copy.deepcopy(catalog)
            invalid = {f['id']: foundation_errors(f) for f in catalog['foundations'] if f.get('enabled', True)}
            usable['foundations'] = [f for f in usable['foundations'] if not any(e.startswith('missing_monitoring_') for e in invalid.get(f['id'], []))]
            for foundation in usable['foundations']:
                rejected = {e.rsplit(':', 1)[-1] for e in invalid.get(foundation['id'], [])}
                foundation['monitoring_endpoints'] = [e for e in foundation.get('monitoring_endpoints', []) if e['id'] not in rejected]
            exclude = await self.observe_endpoints(catalog, revision)
            for component in ('scrape', 'rules'):
                try:
                    if component == 'scrape':
                        value = scrape_config(usable, exclude=exclude)
                    else:
                        from .native_rules import build
                        value = build(usable)[0]
                    await self.apply_config(revision, component, value)
                except Superseded:
                    raise
                except Exception as error:
                    self.mark(revision, component, 'error', error=self.error_code(error, component))
            if all(f['components'][c]['status'] in ('ready', 'disabled') for f in self.state['foundations'] for c in ('scrape', 'rules')):
                atomic_json(self.settings.catalog, public_catalog(usable))
            for foundation in catalog['foundations']:
                self.current(revision)
                fid = foundation['id']
                if not foundation.get('enabled', True):
                    row = next(f for f in self.state['foundations'] if f['foundation_id'] == fid)
                    try:
                        if not all(row['components'][name]['status'] == 'disabled' for name in ('scrape', 'rules')):
                            self.mark(revision, 'perses', 'pending', fid=fid, error='retirement_collection_pending')
                        else:
                            await self.retire_resources(revision, foundation)
                            self.mark(revision, 'perses', 'disabled', fid=fid)
                    except Superseded:
                        raise
                    except Exception as error:
                        self.mark(revision, 'perses', 'conflict' if isinstance(error, Conflict) else 'error',
                                  fid=fid, error=self.error_code(error, 'perses'))
                    probe = read_json(self.settings.probe_status, {})
                    removed = {'foundation:' + fid} | {'gateway:' + g['id'] for g in catalog['gateways']
                              if g.get('router_id') and g['router_id'] == (foundation.get('router') or {}).get('id')}
                    stopped = (0 <= time.time() - probe.get('observed_at', 0) <= 5 and not probe.get('error')
                               and probe.get('catalog_revision') == revision
                               and isinstance(probe.get('target_ids'), list)
                               and removed.isdisjoint(probe['target_ids'])
                               and not any(probe.get('inflight', {}).get(identity) for identity in removed))
                    self.mark(revision, 'probes', 'disabled' if stopped else 'pending', fid=fid,
                              error=None if stopped else 'retirement_probe_ack_pending')
                    continue
                if any(e.startswith('missing_monitoring_') for e in invalid.get(fid, [])):
                    for component in ('scrape', 'rules', 'perses'):
                        self.mark(revision, component, 'pending', fid=fid, error=invalid[fid][0])
                    continue
                try:
                    resources = perses_resources(foundation, usable, self.settings.templates, self.settings.vm)
                    await self.publish_resources(revision, foundation, resources)
                    self.mark(revision, 'perses', 'ready' if foundation.get('enabled', True) else 'disabled', fid=fid)
                except Superseded:
                    raise
                except Exception as error:
                    self.mark(revision, 'perses', 'conflict' if isinstance(error, Conflict) else 'error', fid=fid, error=self.error_code(error, 'perses'))
                if invalid.get(fid):
                    self.mark(revision, 'scrape', 'partial', fid=fid, error=invalid[fid][0])
                router = foundation.get('router') or {}
                probe = read_json(self.settings.probe_status, {})
                fresh = (0 <= time.time() - probe.get('observed_at', 0) <= 5 and not probe.get('error')
                         and probe.get('catalog_revision') == revision
                         and 'foundation:' + fid in probe.get('target_ids', []))
                configured = bool(router.get('proxy_url')) and foundation.get('enabled', True)
                status = 'ready' if fresh and configured else 'pending' if configured else 'not_configured'
                self.mark(revision, 'probes', status, fid=fid)
            await self.observe_collection(catalog, revision)
            probe = read_json(self.settings.probe_status, {})
            definitions = {g['id']: g for g in catalog['gateways']}
            owners = {(f.get('router') or {}).get('id'): f for f in catalog['foundations'] if (f.get('router') or {}).get('id')}
            for gateway in self.state['gateways']:
                definition = definitions[gateway['gateway_id']]
                owner = owners.get(definition.get('router_id'))
                if not definition.get('enabled', True) or (owner and not owner.get('enabled', True)):
                    gateway['status'] = 'disabled'
                elif not owner or not owner.get('monitoring_adapter') or not definition.get('business_url'):
                    # Unmanaged registrations are intentionally absent from the
                    # worker. They are not jobs waiting to become active.
                    gateway['status'] = 'not_configured'
                else:
                    gateway['status'] = 'ready' if (0 <= time.time() - probe.get('observed_at', 0) <= 5 and not probe.get('error')
                        and probe.get('catalog_revision') == revision and 'gateway:' + gateway['gateway_id'] in probe.get('target_ids', [])) else 'pending'
            states = [v['status'] for f in self.state['foundations'] for v in f['components'].values()]
            if all(s in ('ready', 'disabled', 'not_configured') for s in states):
                self.state['applied_revision'] = revision
            else:
                self.state['applied_revision'] = min(self.state['applied_revision'], revision - 1)
            self.save()

    @staticmethod
    def error_code(error, component):
        if isinstance(error, (Conflict, RuntimeError, ValueError)) and str(error).replace('_', '').isalnum():
            return str(error)[:120]
        if isinstance(error, httpx.HTTPStatusError):
            return component + '_http_' + str(error.response.status_code)
        return component + '_' + type(error).__name__.lower()

    async def run(self):
        while True:
            try:
                await asyncio.wait_for(self.event.wait(), 15)
            except asyncio.TimeoutError:
                pass
            self.event.clear()
            try:
                await self.reconcile()
            except Superseded:
                self.event.set()
            except Exception as error:
                self.state['error'] = self.error_code(error, 'reconcile')
                self.save()

    async def pause(self, router_id, operation_id):
        async with self.lock:
            control = self.control()
            if operation_id in control.get('retired_pause_operations', {}):
                raise Conflict('probe_pause_retired')
            control['paused_routers'][router_id] = operation_id
            paused_at = time.time()
            atomic_json(self.settings.control, control)
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            status = read_json(self.settings.probe_status, {})
            if (status.get('observed_at', 0) >= paused_at and
                    status.get('pause_operations', {}).get(router_id) == operation_id and
                    router_id in status.get('paused_routers', [])):
                return {'router_id': router_id, 'operation_id': operation_id, 'dispatch_stopped': True,
                        'inflight': status.get('inflight', {})}
            await asyncio.sleep(.1)
        raise RuntimeError('probe_pause_pending')

    async def resume(self, router_id, operation_id):
        async with self.lock:
            control = self.control()
            completed = control.setdefault('resume_operations', {})
            if operation_id in completed:
                if completed[operation_id] != router_id:
                    raise Conflict('probe_operation_identity_conflict')
                return {'router_id': router_id, 'operation_id': operation_id, 'resumed': True}
            retired = control.setdefault('retired_pause_operations', {})
            pause = control['paused_routers'].pop(router_id, None)
            if pause:
                retired[pause] = router_id
            retired[operation_id] = router_id
            completed[operation_id] = router_id
            atomic_json(self.settings.control, control)
        return {'router_id': router_id, 'operation_id': operation_id, 'resumed': True}


def create_app(settings=None, publisher=None):
    @asynccontextmanager
    async def lifespan(app):
        value = settings or Settings.environment()
        app.state.controller = Controller(value, publisher)
        task = asyncio.create_task(app.state.controller.run())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await app.state.controller.publisher.close()
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware('http')
    async def authorize(request, call_next):
        controller = request.app.state.controller
        cfg = controller.settings
        peer = request.client.host if request.client else ''
        if peer not in cfg.allowed_clients:
            return JSONResponse({'detail': 'Forbidden'}, 403)
        if request.url.path == '/health':
            return await call_next(request)
        if cfg.require_tls and request.url.scheme != 'https':
            return JSONResponse({'detail': 'TLS required'}, 403)
        if not hmac.compare_digest(request.headers.get('authorization', ''), 'Bearer ' + cfg.token):
            return JSONResponse({'detail': 'Unauthorized'}, 401)
        return await call_next(request)

    @app.get('/health')
    async def health(request: Request):
        controller = request.app.state.controller
        return {'status': 'ok', 'accepted_revision': controller.state['accepted_revision'],
                'applied_revision': controller.state['applied_revision']}

    @app.put('/internal/monitoring/v1/catalog')
    async def catalog(request: Request):
        try:
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > 16 * 1024 * 1024:
                    raise HTTPException(413, 'Catalog too large')
            return await request.app.state.controller.accept(json.loads(raw))
        except Conflict as error:
            raise HTTPException(409, str(error)) from None
        except (ValueError, TypeError, KeyError):
            raise HTTPException(422, 'Invalid monitoring catalog') from None

    @app.get('/internal/monitoring/v1/integrations')
    async def integrations(request: Request):
        return request.app.state.controller.status()

    @app.post('/internal/monitoring/v1/reconcile')
    async def reconcile(request: Request):
        body = await request.json()
        controller = request.app.state.controller
        if body.get('expected_revision') != controller.state['accepted_revision']:
            raise HTTPException(409, 'catalog_revision_changed')
        if body.get('force_perses'):
            fid = body.get('foundation_id')
            if fid not in {f['foundation_id'] for f in controller.state['foundations']}:
                raise HTTPException(404, 'foundation_not_found')
            if any(f['id'] == fid and not f.get('enabled', True) for f in controller.desired['catalog']['foundations']):
                raise HTTPException(409, 'retired_foundation_cannot_publish')
            controller.forced.add(fid)
        controller.event.set()
        return {'accepted_revision': controller.state['accepted_revision']}

    @app.post('/internal/monitoring/v1/probes/{action}')
    async def control_probe(action: str, request: Request):
        body = await request.json()
        if action not in ('pause', 'resume') or not isinstance(body, dict) or any(
                not isinstance(body.get(key), str) or not 1 <= len(body[key]) <= 128 for key in ('router_id', 'operation_id')):
            raise HTTPException(422, 'invalid_probe_control')
        controller = request.app.state.controller
        try:
            return await getattr(controller, action)(body['router_id'], body['operation_id'])
        except Conflict as error:
            raise HTTPException(409, str(error)) from None
        except RuntimeError as error:
            raise HTTPException(503, str(error)) from None
    return app


app = create_app()
