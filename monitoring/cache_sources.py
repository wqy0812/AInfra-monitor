"""Explicit business endpoints mapped to the existing monitoring environments.

The gateway's deployment environment and editable model text are never inputs.
Bindings have evidence and time bounds; loopback addresses require proxy context.
"""
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import posixpath
from urllib.parse import unquote, urlsplit, urlunsplit

from .native_schema import NODES
from . import source_registry

SCHEMA = 'service-cache-v1'
TIERS = {'dcu-pd': ['device', 'host', 'storage'], 'a3-vllm': ['device', 'storage'], 'xpu-pd': []}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def binding_revision(binding):
    # An operator may close an open validity interval without invalidating
    # snapshots for the already verified earlier part of that same binding.
    return digest({k:v for k,v in binding.items() if k != 'valid_until'})


def canonical_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError('invalid_endpoint')
    u = urlsplit(value)
    if u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise ValueError('invalid_endpoint')
    port = u.port
    path = posixpath.normpath(unquote(u.path) or '/').rstrip('/')
    for suffix in ('/chat/completions', '/models', '/generate'):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break
    if path.endswith('/v1'):
        path = path[:-3]
    host = u.hostname.lower().rstrip('.')
    if ':' in host:
        host = '[' + host + ']'
    if port and port != (443 if u.scheme == 'https' else 80):
        host += ':' + str(port)
    return urlunsplit((u.scheme, host, path, '', ''))


def local_endpoint(value):
    host = urlsplit(value).hostname
    try:
        address = ipaddress.ip_address(host)
        return address.is_loopback or address.is_private or address.is_link_local or address.is_unspecified
    except ValueError:
        return host == 'localhost' or '.' not in host or host.endswith(('.localhost', '.local', '.internal'))


def source(environment):
    if environment not in NODES:
        raise ValueError('unknown_cache_source')
    entry=source_registry.source(environment)
    definition = {'environment': environment, 'prefill_node': NODES[environment].get('prefill'),
                  'semantics': entry['semantics'],
                  'method': 'time_average' if entry['baseline'] == 'xpu-pd' else 'token_weighted',
                  'tiers': entry['tiers'], 'adapter_version': 1}
    return {**definition, 'id': environment, 'revision': digest(definition)}


def bindings():
    path = Path(os.environ.get('CACHE_SOURCE_CONFIG', Path(__file__).with_name('cache_sources.json')))
    data = json.loads(path.read_text())
    if data.get('version') != 1 or not isinstance(data.get('bindings'), list):
        raise ValueError('invalid_cache_source_config')
    result = []
    items=data['bindings']
    if source_registry.configured():
        current=[]
        for foundation in source_registry.catalog()['foundations']:
            current.extend((foundation.get('router') or {}).get('bindings', []))
        # Legacy evidence is retained up to the first recorded catalog binding;
        # a mutable latest backend never rewrites an earlier report window.
        cutoffs={}
        for item in current:
            env=item.get('environment')
            if isinstance(item.get('valid_from'), (float,int)):
                cutoffs[env]=min(cutoffs.get(env, math.inf), item['valid_from'])
        retained=[]
        for item in items:
            if item.get('environment') not in NODES:
                continue
            cutoff=cutoffs.get(item['environment'], math.inf)
            if item.get('valid_from', math.inf) >= cutoff:
                continue
            retained.append({**item, **({'valid_until':min(item.get('valid_until', math.inf),cutoff)} if cutoff < math.inf else {})})
        items=retained+current
    for item in items:
        item = dict(item)
        source(item['environment'])
        item['backend_url'] = canonical_url(item['backend_url'])
        if item.get('proxy_url'):
            item['proxy_url'] = canonical_url(item['proxy_url'])
        if local_endpoint(item['backend_url']) and not item.get('proxy_url'):
            raise ValueError('private_endpoint_requires_proxy_context')
        if not item.get('evidence') or not isinstance(item.get('valid_from'), (float, int)):
            raise ValueError('cache_binding_requires_evidence_and_time')
        until = item.get('valid_until', math.inf)
        if not math.isfinite(item['valid_from']) or not item['valid_from'] < until:
            raise ValueError('invalid_cache_binding_time')
        result.append(item)
    return result


def resolve(request, catalog=None):
    backend = canonical_url(request['backend_url'])
    proxy = canonical_url(request['proxy_url'])
    at = request['at']
    if not isinstance(at, (int, float)) or not math.isfinite(at):
        raise ValueError('invalid_cache_binding_time')
    matches = [b for b in (bindings() if catalog is None else catalog)
               if canonical_url(b['backend_url']) == backend
               and (not b.get('proxy_url') or canonical_url(b['proxy_url']) == proxy)
               and (not local_endpoint(backend) or b.get('proxy_url'))
               and b['valid_from'] <= at < b.get('valid_until', math.inf)]
    if len(matches) != 1:
        return {'status': 'unavailable', 'reason': 'ambiguous_cache_source' if matches else 'cache_source_not_registered'}
    binding = matches[0]
    return {'status': 'resolved', 'schema': SCHEMA, 'source': source(binding['environment']),
            'backend_url': backend, 'proxy_url': proxy, 'binding_revision': binding_revision(binding),
            'binding': binding, 'resolved_at': at}


def validate_snapshot(snapshot, start, end):
    definition = source(snapshot['source']['id'])
    if snapshot.get('schema') != SCHEMA or snapshot['source'] != definition:
        raise ValueError('cache_source_version_changed')
    binding = snapshot['binding']
    if binding_revision(binding) != snapshot['binding_revision'] or binding.get('environment') != definition['environment']:
        raise ValueError('cache_binding_mismatch')
    if not binding['valid_from'] <= start < end <= binding.get('valid_until', math.inf):
        raise ValueError('cache_binding_outside_validity')
    # Persisted bindings are checked against the operator-owned catalog, never
    # accepted as arbitrary selectors supplied by an API caller.
    registered = [item for item in bindings() if binding_revision(item) == snapshot['binding_revision']]
    if len(registered) != 1:
        raise ValueError('cache_binding_version_unavailable')
    if not registered[0]['valid_from'] <= start < end <= registered[0].get('valid_until', math.inf):
        raise ValueError('cache_binding_outside_validity')
    return definition
