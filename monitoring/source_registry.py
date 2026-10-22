"""Versioned platform source catalog shared by collection, queries and dashboards.

The catalog is an applied, public configuration snapshot, not a second registry.
An unset path keeps the existing installation usable during the coordinated
upgrade. A configured empty catalog deliberately contains no sources.
"""
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

ADAPTERS = {
    'dcu-sglang-v1': {'baseline': 'dcu-pd', 'semantics': 'prefill-effective-v1',
                      'tiers': ['device', 'host', 'storage']},
    'a3-vllm-v1': {'baseline': 'a3-vllm', 'semantics': 'vllm-prefill-source-v1',
                   'tiers': ['device', 'storage']},
    'xpu-sglang-v1': {'baseline': 'xpu-pd', 'semantics': 'xpu-native-prefix-v1', 'tiers': []},
}
LEGACY_NODES = {'dcu-pd': {'prefill': 'dcu1', 'decode': 'dcu2'},
                'a3-vllm': {'prefill': 'a3-1', 'decode': 'a3-2'},
                'xpu-pd': {'prefill': 'xpu-2', 'decode': 'xpu-1'}}
_override = ContextVar('monitoring_catalog', default=None)
_last_good = {}
_errors = {}


def configured():
    return _override.get() is not None or bool(os.environ.get('MONITORING_CATALOG_FILE'))


def legacy_catalog():
    foundations = []
    for adapter, definition in ADAPTERS.items():
        env = definition['baseline']
        foundations.append({'id': env, 'name': env, 'monitoring_source': env,
            'monitoring_adapter': adapter, 'enabled': True, 'legacy': True,
            'resource_nodes': [{'name': node, 'role': role} for role, node in LEGACY_NODES[env].items()],
            'monitoring_endpoints': []})
    return {'schema_version': 1, 'revision': 0, 'foundations': foundations, 'gateways': []}


@lru_cache(maxsize=4)
def _read(path, stamp, size):
    if size > 16 * 1024 * 1024:
        raise ValueError('monitoring_catalog_too_large')
    return json.loads(Path(path).read_text())


def catalog():
    override = _override.get()
    if override is not None:
        return override
    path = os.environ.get('MONITORING_CATALOG_FILE')
    if not path:
        return legacy_catalog()
    try:
        stat = Path(path).stat()
        value = _read(path, stat.st_mtime_ns, stat.st_size)
        sources(value)
    except (OSError, ValueError, KeyError, TypeError):
        _errors[path] = 'monitoring_catalog_unavailable'
        if path not in _last_good:
            raise
        return _last_good[path]
    _last_good[path] = value
    _errors.pop(path, None)
    return value


def load_error():
    return _errors.get(os.environ.get('MONITORING_CATALOG_FILE'))


@contextmanager
def use_catalog(value):
    token = _override.set(value)
    try:
        yield
    finally:
        _override.reset(token)


def sources(value=None, *, include_disabled=False):
    data = catalog() if value is None else value
    if data.get('schema_version') != 1 or not isinstance(data.get('foundations'), list):
        raise ValueError('invalid_monitoring_catalog')
    result = {}
    for foundation in data['foundations']:
        if not include_disabled and not foundation.get('enabled', True):
            continue
        env = foundation.get('monitoring_source')
        adapter = foundation.get('monitoring_adapter')
        # Incomplete registrations remain visible to the controller, but do not
        # invent metric semantics or start a query worker.
        if not env or not adapter:
            continue
        if adapter not in ADAPTERS or not isinstance(env, str) or not re.fullmatch(r'[A-Za-z0-9_:-]{1,128}', env):
            raise ValueError('invalid_monitoring_source')
        if env in result:
            raise ValueError('duplicate_monitoring_source')
        nodes = {}
        owners = {}
        for node in foundation.get('resource_nodes', []):
            name, role = node.get('name'), node.get('role', '')
            if not isinstance(name, str) or not name or role not in ('prefill', 'decode', 'shared', 'other', ''):
                raise ValueError('invalid_monitoring_node')
            if name in owners and owners[name] != role:
                raise ValueError('conflicting_monitoring_node_role')
            owners[name] = role
            if role in ('prefill', 'decode') and name not in nodes.setdefault(role, []):
                nodes[role].append(name)
        endpoints = []
        for endpoint in foundation.get('monitoring_endpoints', []):
            if not endpoint.get('enabled', True):
                continue
            name, role = endpoint.get('node', ''), endpoint.get('role', '')
            if name and role in ('prefill', 'decode'):
                if name in owners and owners[name] != role:
                    raise ValueError('conflicting_monitoring_node_role')
                owners[name] = role
                if name not in nodes.setdefault(role, []):
                    nodes[role].append(name)
            endpoints.append({k: v for k, v in endpoint.items() if k != 'auth'})
        result[env] = {**ADAPTERS[adapter], 'id': env, 'environment': env,
            'foundation_id': foundation['id'], 'name': foundation.get('name', env),
            'adapter': adapter, 'nodes': nodes, 'all_nodes': owners, 'endpoints': endpoints,
            'enabled': foundation.get('enabled', True), 'legacy': foundation.get('legacy', False),
            'revision': data.get('revision', 0)}
    return result


def source(environment, value=None):
    try:
        return sources(value)[environment]
    except KeyError:
        raise ValueError('unknown_monitoring_source') from None


def adapter_environment(environment):
    return source(environment)['baseline']


def nodes(environment):
    return source(environment)['nodes']


def role_of(environment, node):
    return next((role for role, names in nodes(environment).items() if node in names), None)


def endpoint_job(adapter, endpoint):
    baseline = ADAPTERS[adapter]['baseline']
    kind, role = endpoint['type'], endpoint.get('role', '')
    if kind == 'host':
        return {'a3-vllm': 'node-a3', 'xpu-pd': 'node-xpu'}.get(baseline, 'node-' + role)
    if kind == 'accelerator':
        return {'a3-vllm': 'npu-a3', 'xpu-pd': 'xpu-hardware'}.get(baseline, 'dcu-' + role)
    if kind == 'inference':
        return 'vllm-a3' if baseline == 'a3-vllm' else 'sglang-' + role
    if kind == 'mooncake':
        return 'mooncake-a3' if baseline == 'a3-vllm' else 'mooncake'
    if kind == 'gateway':
        return 'aigate'
    raise ValueError('unsupported_monitoring_endpoint')


def endpoint_instance(endpoint):
    """Full custom paths distinguish targets; imported identities stay intact."""
    parsed=urlsplit(endpoint['url'])
    if endpoint.get('scrape_labels') is not None:
        return endpoint['scrape_labels'].get('instance') or parsed.netloc
    path=parsed.path or '/'
    return parsed.netloc + (path if path != '/metrics' else '')


def expected_instances(environment, role, node=None):
    definition = source(environment)
    if definition['legacy']:
        return (4 if role == 'prefill' else 16) if definition['baseline'] == 'a3-vllm' else 1
    return len({endpoint['id'] for endpoint in definition['endpoints']
        if endpoint['type'] == 'inference' and endpoint.get('role') == role
        and (node is None or endpoint.get('node') == node)})


def node_matcher(environment, role):
    names = nodes(environment).get(role, [])
    if len(names) == 1:
        return 'node=' + json.dumps(names[0])
    return 'node=~' + json.dumps('|'.join(re.escape(name) for name in names) if names else 'a^')


def rewrite_query(expression, environment):
    """Bind a reviewed adapter expression to catalog identities, never plain text names."""
    baseline = adapter_environment(environment)
    if configured():
        expression = _instance_scopes(expression, environment, baseline)
        if baseline == 'a3-vllm' and 'count by(environment,node,instance)' in expression:
            # These two legacy template guards express expected vLLM replica
            # counts. Compare to registered per-node coverage, not constants.
            for count, role in ((4, 'prefill'), (16, 'decode')):
                expected = ('last_over_time(monitoring_native_backend_expected_instances{environment='
                            + json.dumps(environment) + ',role=' + json.dumps(role)
                            + ',monitoring_scope=""}[5s])')
                expression = re.sub(r'== ' + str(count) + r'(?=\))',
                    lambda _: '== on(environment,node) ' + expected, expression)
    expression = expression.replace('environment=' + json.dumps(baseline), 'environment=' + json.dumps(environment))
    for role, old_node in LEGACY_NODES[baseline].items():
        expression = expression.replace('node=' + json.dumps(old_node), node_matcher(environment, role))
        names = nodes(environment).get(role, [])
        pattern = '|'.join(re.escape(name) for name in names) if names else 'a^'
        if not configured():
            pattern = old_node
        expression = expression.replace('node=~' + json.dumps(old_node), 'node=~' + json.dumps(pattern))
        expression = expression.replace(', "node", ' + json.dumps(old_node) + ')',
                                        ', "node", ' + json.dumps(pattern) + ')')
    if configured():
        # Existing dashboards show per-node records. Optional pooled P/D
        # records share metric families but must not double their totals.
        def detail(found):
            labels = found[2]
            if 'monitoring_scope' not in labels:
                labels += ',monitoring_scope=""'
            return found[1] + '{' + labels + '}'
        expression = re.sub(r'(monitoring_native_[A-Za-z0-9_]+)\{([^{}]*)\}', detail, expression)
    return expression


def _instance_scopes(expression, environment, baseline):
    """Bind template instance inventories while preserving dashboard variables."""
    definition = source(environment)
    matcher = r'\binstance(?:=~|=)("(?:\\.|[^"\\])*")'
    def replace(found):
        labels = found[1]
        if 'environment=' + json.dumps(baseline) not in labels:
            return found[0]
        identity = re.search(matcher, labels)
        if not identity or '$' in identity[1]:
            return found[0]
        node = re.search(r'\bnode(?:=~|=)("(?:\\.|[^"\\])*")', labels)
        job = re.search(r'\bjob=("(?:\\.|[^"\\])*")', labels)
        if not job:
            return found[0]
        role = next((role for role, name in LEGACY_NODES[baseline].items()
                     if node and json.loads(node[1]) == name), None)
        instances = []
        for endpoint in definition['endpoints']:
            if role and endpoint.get('role') != role:
                continue
            if (endpoint.get('scrape_job') or endpoint_job(definition['adapter'], endpoint)) != json.loads(job[1]):
                continue
            instance = endpoint_instance(endpoint)
            if instance not in instances:
                instances.append(instance)
        pattern = '|'.join(re.escape(value) for value in instances) or 'a^'
        labels = re.sub(matcher, lambda _: 'instance=~' + json.dumps(pattern), labels)
        return '{' + labels + '}'
    return re.sub(r'\{([^{}]*)\}', replace, expression)


class NodeDirectory(Mapping):
    """Compatibility view; multi-node roles expose their names without selecting one."""
    def __iter__(self):
        return iter(sources())

    def __len__(self):
        return len(sources())

    def __getitem__(self, environment):
        try:
            return {role: names[0] if len(names) == 1 else ', '.join(names)
                    for role, names in nodes(environment).items()}
        except ValueError:
            raise KeyError(environment) from None


NODES = NodeDirectory()
