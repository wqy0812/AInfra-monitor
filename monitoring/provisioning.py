"""Compile platform-owned catalogs into deterministic monitoring resources.

Only this module writes generated configuration. It never connects to an exporter
or invokes a model. The controller owns validation, application and readback.
"""
import copy
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

ADAPTERS = {'dcu-sglang-v1': 'dcu-pd', 'a3-vllm-v1': 'a3-vllm', 'xpu-sglang-v1': 'xpu-pd'}
PROJECTS = {'dcu-pd': 'dcu-monitoring', 'a3-vllm': 'a3-monitoring', 'xpu-pd': 'xpu-monitoring'}
IDENTITY = re.compile(r'^[A-Za-z0-9_:-]{1,128}$')
KINDS = {'host', 'accelerator', 'inference', 'mooncake'}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def atomic_json(path, value, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(canonical(value) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def endpoint_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError('invalid_endpoint_url')
    parsed = urlsplit(value)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or any(c.isspace() or ord(c) < 32 for c in value) or parsed.port == 0):
        raise ValueError('invalid_endpoint_url')
    return parsed


def validate_catalog(body):
    """Bound the trusted internal protocol and reject identity collisions early."""
    if not isinstance(body, dict) or body.get('schema_version') != 1 or type(body.get('revision')) is not int or body['revision'] < 1:
        raise ValueError('invalid_catalog_version')
    if not isinstance(body.get('foundations'), list) or not isinstance(body.get('gateways'), list):
        raise ValueError('invalid_catalog_directory')
    if len(body['foundations']) > 1000 or len(body['gateways']) > 1000:
        raise ValueError('catalog_too_large')
    ids, sources, routers, endpoints = set(), set(), set(), set()
    for foundation in body['foundations']:
        if not isinstance(foundation, dict):
            raise ValueError('invalid_foundation')
        fid, source = foundation.get('id'), foundation.get('monitoring_source')
        if not isinstance(fid, str) or not IDENTITY.fullmatch(fid) or fid in ids:
            raise ValueError('duplicate_or_invalid_foundation')
        ids.add(fid)
        if source:
            if not isinstance(source, str) or not IDENTITY.fullmatch(source) or source in sources:
                raise ValueError('duplicate_or_invalid_source')
            sources.add(source)
        adapter = foundation.get('monitoring_adapter')
        if adapter and adapter not in ADAPTERS:
            raise ValueError('unsupported_adapter')
        entries = foundation.get('monitoring_endpoints', [])
        nodes = foundation.get('resource_nodes', [])
        if not isinstance(entries, list) or len(entries) > 100 or not isinstance(nodes, list) or len(nodes) > 1000:
            raise ValueError('invalid_foundation_topology')
        known_nodes = {n.get('name'): n.get('role', '') for n in nodes if isinstance(n, dict)}
        if len(known_nodes) != len(nodes) or any(not isinstance(n, str) or not n or len(n) > 200 for n in known_nodes):
            raise ValueError('invalid_resource_nodes')
        for endpoint in entries:
            if not isinstance(endpoint, dict) or endpoint.get('type') not in KINDS:
                raise ValueError('invalid_endpoint_type')
            eid = endpoint.get('id')
            if not isinstance(eid, str) or not IDENTITY.fullmatch(eid) or eid in endpoints:
                raise ValueError('duplicate_or_invalid_endpoint')
            endpoints.add(eid)
            endpoint_url(endpoint.get('url'))
            auth = endpoint.get('auth') or {'type': 'none'}
            if auth.get('type') not in ('none', 'basic'):
                raise ValueError('unsupported_endpoint_auth')
            if auth.get('type') == 'basic':
                username, password = auth.get('username'), auth.get('password')
                if not isinstance(username, str) or not username or ':' in username or len(username) > 256:
                    raise ValueError('invalid_endpoint_auth')
                if not isinstance(password, str) or not 1 <= len(password) <= 8192:
                    raise ValueError('missing_endpoint_credential')
        router = foundation.get('router')
        if router:
            if not isinstance(router, dict) or not isinstance(router.get('id'), str) or not IDENTITY.fullmatch(router['id']) or router['id'] in routers:
                raise ValueError('duplicate_or_invalid_router')
            routers.add(router['id'])
            for field in ('proxy_url', 'profile_url'):
                if router.get(field):
                    endpoint_url(router[field])
    gids = set()
    for gateway in body['gateways']:
        gid = gateway.get('id') if isinstance(gateway, dict) else None
        if not isinstance(gid, str) or not IDENTITY.fullmatch(gid) or gid in gids:
            raise ValueError('duplicate_or_invalid_gateway')
        gids.add(gid)
        if gateway.get('business_url'):
            endpoint_url(gateway['business_url'])
    return copy.deepcopy(body)


def store_credentials(catalog, root):
    """Replace password values with immutable, private files before persistence."""
    result = copy.deepcopy(catalog)
    for foundation in result['foundations']:
        for endpoint in foundation.get('monitoring_endpoints', []):
            auth = endpoint.get('auth') or {'type': 'none'}
            password = auth.pop('password', None)
            auth.pop('password_file', None)
            auth.pop('authorization_file', None)
            if auth.get('type') == 'basic':
                filename = digest([foundation['id'], endpoint['id'], catalog['revision']]) + '.secret'
                path = root / 'secrets' / filename
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                # Base64 preserves whitespace-sensitive Basic credentials despite native file trimming.
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, 'wb') as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(base64.b64encode((auth['username'] + ':' + password).encode('utf-8')))
                auth['authorization_file'] = str(path)
            endpoint['auth'] = auth
    return result


def public_catalog(catalog):
    result = copy.deepcopy(catalog)
    for foundation in result['foundations']:
        for endpoint in foundation.get('monitoring_endpoints', []):
            auth = endpoint.get('auth') or {'type': 'none'}
            endpoint['auth'] = {'type': auth.get('type', 'none'), 'username': auth.get('username', ''),
                                'has_password': bool(auth.get('authorization_file') or auth.get('password_file'))}
    return result


def foundation_errors(foundation):
    errors = []
    if not foundation.get('monitoring_source'):
        errors.append('missing_monitoring_source')
    if not foundation.get('monitoring_adapter'):
        errors.append('missing_monitoring_adapter')
    nodes = {n['name']: n.get('role', '') for n in foundation.get('resource_nodes', [])}
    for endpoint in foundation.get('monitoring_endpoints', []):
        if not endpoint.get('enabled', True):
            continue
        if endpoint.get('node') not in nodes:
            errors.append('endpoint_node_not_registered:' + endpoint['id'])
        elif endpoint['type'] != 'mooncake' and endpoint.get('role') and nodes[endpoint['node']] and endpoint['role'] != nodes[endpoint['node']]:
            errors.append('endpoint_role_conflict:' + endpoint['id'])
        if endpoint['type'] in ('inference', 'accelerator', 'host') and endpoint.get('role') not in ('prefill', 'decode', 'shared', 'other'):
            errors.append('missing_endpoint_role:' + endpoint['id'])
    return errors


def project_name(foundation):
    return PROJECTS.get(foundation['monitoring_source'], 'foundation-' + foundation['id'].replace(':', '-').lower())


def scrape_config(catalog, exclude=None):
    from .source_registry import endpoint_job, endpoint_instance
    jobs, seen = [], set()
    metric_identities = set()
    exclude = exclude or set()
    for foundation in catalog['foundations']:
        if not foundation.get('enabled', True) or foundation_errors(foundation):
            continue
        adapter, source = foundation['monitoring_adapter'], foundation['monitoring_source']
        for endpoint in foundation.get('monitoring_endpoints', []):
            if not endpoint.get('enabled', True) or endpoint['id'] in exclude:
                continue
            parsed = endpoint_url(endpoint['url'])
            target = parsed.netloc
            identity = (parsed.scheme, target, parsed.path or '/')
            if identity in seen:
                raise ValueError('duplicate_scrape_endpoint')
            seen.add(identity)
            job = endpoint.get('scrape_job') or endpoint_job(adapter, endpoint)
            kind = endpoint['type']
            service = {'host': 'host', 'inference': 'vllm' if adapter == 'a3-vllm-v1' else 'sglang',
                       'accelerator': {'dcu-sglang-v1': 'dcu', 'a3-vllm-v1': 'npu', 'xpu-sglang-v1': 'xpu-exporter'}[adapter],
                       'mooncake': 'mooncake'}[kind]
            labels = {'environment': source, 'node': endpoint['node'], 'service': service}
            if endpoint.get('role'):
                labels['role'] = endpoint['role']
            if endpoint.get('scrape_labels') is not None:
                labels = copy.deepcopy(endpoint['scrape_labels'])
                if labels.get('environment') != source or labels.get('node') != endpoint['node']:
                    raise ValueError('legacy_scrape_labels_conflict')
            if endpoint.get('scrape_labels') is None:
                labels['instance'] = endpoint_instance(endpoint)
            metric_identity = (source, job, labels.get('instance', target))
            if metric_identity in metric_identities:
                raise ValueError('duplicate_scrape_series_identity')
            metric_identities.add(metric_identity)
            # Existing label identities are retained. New IDs live in the catalog,
            # not time series labels, so takeover does not reset every counter.
            config = {'job_name': 'managed-' + foundation['id'] + '-' + endpoint['id'],
                      'scheme': parsed.scheme, 'metrics_path': parsed.path or '/',
                      'static_configs': [{'targets': [target], 'labels': labels}],
                      'relabel_configs': [{'target_label': 'job', 'replacement': job}]}
            prefixes = {'host': 'node_.*', 'inference': 'vllm:.*' if adapter == 'a3-vllm-v1' else 'sglang:.*',
                        'accelerator': {'dcu-sglang-v1': 'dcu_.*', 'a3-vllm-v1': '(npu_.*|machine_npu_nums)', 'xpu-sglang-v1': 'node_xpu_.*'}[adapter],
                        'mooncake': '(master_.*|segment_.*|.*get_nums_|mem_cache_hit_nums_|file_cache_hit_nums_|ha_.*)'}
            config['metric_relabel_configs'] = copy.deepcopy(endpoint.get('scrape_metric_relabel_configs', [{'source_labels': ['__name__'], 'regex': prefixes[kind], 'action': 'keep'}]))
            auth = endpoint.get('auth') or {}
            if auth.get('type') == 'basic':
                config['authorization'] = {'type': 'Basic', 'credentials_file': auth['authorization_file']}
            config['job_name'] = 'managed-' + digest({k: v for k, v in config.items() if k != 'job_name'})[:12] + '-' + foundation['id'] + '-' + endpoint['id']
            jobs.append(config)
        router = foundation.get('router') or {}
        if router.get('profile_url'):
            parsed = endpoint_url(router['profile_url'].rstrip('/') + '/metrics')
            identity = (parsed.scheme, parsed.netloc, parsed.path)
            if identity in seen:
                raise ValueError('duplicate_proxy_endpoint')
            seen.add(identity)
            labels = {'environment': source, 'service': 'aigate'}
            # Preserve current proxy node identity for the three imported sources.
            nodes = [n['name'] for n in foundation.get('resource_nodes', []) if n.get('role') == ('decode' if adapter == 'xpu-sglang-v1' else 'prefill')]
            if nodes:
                labels['node'] = nodes[0]
            if router.get('scrape_labels') is not None:
                labels = copy.deepcopy(router['scrape_labels'])
                if labels.get('environment') != source:
                    raise ValueError('legacy_proxy_labels_conflict')
            jobs.append({'job_name': 'managed-proxy-' + foundation['id'], 'scheme': parsed.scheme,
                         'metrics_path': parsed.path, 'static_configs': [{'targets': [parsed.netloc], 'labels': labels}],
                         'relabel_configs': [{'target_label': 'job', 'replacement': 'aigate'}],
                         'metric_relabel_configs': copy.deepcopy(router.get('scrape_metric_relabel_configs', [{'source_labels': ['__name__'], 'regex': 'aigate_.*', 'action': 'keep'}]))})
    for config in jobs:
        if config['job_name'].startswith('managed-proxy-'):
            config['job_name'] = 'managed-proxy-' + digest({k: v for k, v in config.items() if k != 'job_name'})[:12] + '-' + config['job_name'].removeprefix('managed-proxy-')
    # Central monitoring remains one shared source, not one scrape per foundation.
    jobs.extend([{'job_name': name, 'static_configs': [{'targets': [target], 'labels': {'environment': 'dcu-pd'}}]}
                 for name, target in [('vmagent', '127.0.0.1:18429'), ('victoriametrics', '127.0.0.1:18428')]])
    return {'global': {'scrape_interval': '5s', 'scrape_timeout': '4s'}, 'scrape_configs': jobs}


def perses_resources(foundation, catalog, templates, vm_url='http://127.0.0.1:18428'):
    """Generate a new project from versioned source templates, never live copies."""
    from .source_registry import rewrite_query, use_catalog
    baseline = ADAPTERS[foundation['monitoring_adapter']]
    old_project, project = PROJECTS[baseline], project_name(foundation)
    root = Path(templates)
    spec = importlib.util.spec_from_file_location('managed_dashboard_columns', root / 'dashboard_columns.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    resources = {'projects': [], 'datasources': [], 'dashboards': []}
    for path in sorted((root / 'projects' / old_project).rglob('*.json')):
        document = json.loads(path.read_text())
        category = {'Project': 'projects', 'Datasource': 'datasources', 'Dashboard': 'dashboards'}.get(document.get('kind'))
        if category and document.get('metadata', {}).get('name') != 'perses-accelerated':
            resources[category].append(document)
    resources = module.apply(resources)
    def transform(value):
        if isinstance(value, list):
            return [transform(v) for v in value]
        if not isinstance(value, dict):
            return value
        result = {}
        for key, item in value.items():
            if key == 'query' and isinstance(item, str):
                result[key] = rewrite_query(item, foundation['monitoring_source'])
            else:
                result[key] = transform(item)
        return result
    binding_catalog = copy.deepcopy(catalog)
    for entry in binding_catalog['foundations']:
        if entry['id'] == foundation['id']:
            entry['enabled'] = True
    with use_catalog(binding_catalog):
        resources = transform(resources)
    capabilities = {e['type'] for e in foundation.get('monitoring_endpoints', []) if e.get('enabled', True)}
    proxy = bool((foundation.get('router') or {}).get('profile_url'))
    for group in resources.values():
        for document in group:
            metadata = document['metadata']
            document['metadata'] = {'name': project if document['kind'] == 'Project' else metadata['name']}
            if document['kind'] != 'Project':
                document['metadata']['project'] = project
            if document['kind'] == 'Project':
                document['spec']['display']['name'] = foundation['name'] + ' 监控' + ('（已停用）' if not foundation.get('enabled', True) else '')
            elif document['kind'] == 'Datasource':
                document['spec']['plugin']['spec']['proxy']['spec']['url'] = vm_url
            else:
                # Node/role variables are generated from topology, including multiple
                # nodes in one role. Display text is editable; identity is stable.
                for variable in document['spec'].get('variables', []):
                    data = variable.get('spec', {})
                    plugin = data.get('plugin', {})
                    if data.get('name') == 'role' and plugin.get('kind') == 'StaticListVariable':
                        values = [{'value': '.*', 'label': '全部'}]
                        for role in ('prefill', 'decode'):
                            names = [re.escape(n['name']) for n in foundation.get('resource_nodes', []) if n.get('role') == role]
                            values.append({'value': '(' + '|'.join([role, *names]) + ')', 'label': role.title()})
                        plugin['spec']['values'] = values
                        data['defaultValue'] = '.*'
                document['spec']['display']['description'] = document['spec']['display'].get('description', '') + '\n监控来源：' + foundation['monitoring_source']
    # Existing projects retain every stable panel for historical access. New
    # projects materialize supported sections only; later capability removal
    # never deletes already-published historical resources.
    if foundation['monitoring_source'] not in PROJECTS:
        patterns = [(proxy, r'aigate_'), ('host' in capabilities, r'node_(?:cpu|memory|load|filesystem|network|disk)|monitoring_(?:native|raw)_host_'),
                    ('accelerator' in capabilities, r'dcu_|npu_|node_xpu_|monitoring_(?:native|raw)_device_'),
                    ('inference' in capabilities, r'sglang:|vllm:|monitoring_(?:native|raw)_(?:backend_|prefill_|prefix_|scheduler_|hicache_)'),
                    ('mooncake' in capabilities, r'master_|segment_|get_nums_|monitoring_(?:native|raw)_store_')]
        dashboards = []
        for document in resources['dashboards']:
            panels = document['spec']['panels']
            for key, panel in list(panels.items()):
                expressions = ' '.join(q['spec']['plugin']['spec'].get('query', '') for q in panel['spec'].get('queries', []))
                if any(not enabled and re.search(pattern, expressions) for enabled, pattern in patterns):
                    del panels[key]
            layouts = document['spec']['layouts']
            for layout in layouts:
                layout['spec']['items'] = [item for item in layout['spec']['items'] if item['content']['$ref'].rsplit('/', 1)[-1] in panels]
            document['spec']['layouts'] = [layout for layout in layouts if layout['spec']['items']]
            if panels:
                dashboards.append(document)
        resources['dashboards'] = dashboards
    return resources
