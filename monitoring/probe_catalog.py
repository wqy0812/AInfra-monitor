"""Stable probe identities and bounded, local dispatch permissions.

The platform owns registration; this module never infers an old gateway's
identity from its current Router binding or an editable display name.
"""
import hashlib
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit

PERMIT_SECONDS = 30
LEGACY_OFFSETS = {'a3-vllm': 0, 'xpu-pd': 20, 'dcu-pd': 40}
ADAPTERS = {'dcu-sglang-v1', 'a3-vllm-v1', 'xpu-sglang-v1'}


def state_file(kind):
    variable = 'MONITORING_PROBE_CONTROL_FILE' if kind == 'control' else 'MONITORING_PROBE_STATUS_FILE'
    filename = 'probes-control.json' if kind == 'control' else 'probes-runtime.json'
    return Path(os.environ.get(variable, str(Path(os.environ.get('STATE_DIR', '/state')) / filename)))


def read_json(path, default=None):
    try:
        value = json.loads(Path(path).read_text())
        if not isinstance(value, dict):
            raise ValueError('Invalid probe state')
        return value
    except (OSError, ValueError):
        return {} if default is None else default


def catalog():
    path = os.environ.get('MONITORING_PROBE_CATALOG_FILE') or os.environ.get('MONITORING_CATALOG_FILE')
    if not path:
        return None
    # A missing or malformed managed catalog is not permission to use legacy URLs.
    value = read_json(path)
    if value.get('schema_version') != 1 or not isinstance(value.get('foundations'), list) or not isinstance(value.get('gateways'), list):
        raise ValueError('Managed probe catalog unavailable')
    return value


def generation_url(base):
    if not isinstance(base, str) or not base:
        return ''
    parsed = urlsplit(base)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        return ''
    base = base.rstrip('/')
    if base.endswith('/v1/chat/completions'):
        return base
    return base + ('/chat/completions' if base.endswith('/v1') else '/v1/chat/completions')


def offset(identity, legacy=''):
    if identity.startswith('gateway:') and legacy in LEGACY_OFFSETS:
        return LEGACY_OFFSETS[legacy]
    # New foundation schedules are odd; new gateway schedules are even.
    number = int.from_bytes(hashlib.sha256(identity.encode()).digest()[:4], 'big')
    return (number % 30) * 2 + int(identity.startswith('foundation:'))


def targets(value=None, control=None, now=None):
    value = catalog() if value is None else value
    if value is None:
        return None
    now = time.time() if now is None else now
    control = read_json(state_file('control')) if control is None else control
    heartbeat = control.get('heartbeat_at', 0)
    fresh = isinstance(heartbeat, (float, int)) and 0 <= now - heartbeat <= PERMIT_SECONDS
    pauses = control.get('paused_routers') or {}
    result = []
    owners = {}
    for foundation in value['foundations']:
        router = foundation.get('router') or {}
        if router.get('id'):
            owners[router['id']] = foundation
        fid = foundation['id']
        identity = 'foundation:' + fid
        url = generation_url(router.get('proxy_url'))
        reason = ('disabled' if not foundation.get('enabled', True) else
                  'awaiting_adapter' if foundation.get('monitoring_adapter') not in ADAPTERS else
                  'awaiting_router' if not url or not router.get('id') else
                  'maintenance' if router.get('maintenance') or router.get('id') in pauses else
                  'permit_expired' if not fresh else '')
        result.append({'probe_id': identity, 'probe_kind': 'foundation', 'foundation_id': fid,
                       'environment': foundation.get('monitoring_source') or '', 'name': foundation['name'],
                       'router_id': router.get('id'), 'url': url, 'offset_seconds': offset(identity),
                       'dispatch_status': 'paused' if reason else 'active', 'pause_reason': reason,
                       'expected_router': router.get('id', ''), 'legacy_probe_environment': ''})
    for gateway in value['gateways']:
        owner = owners.get(gateway.get('router_id'))
        # An unrelated registered gateway (for example a retired environment)
        # must not silently regain real model traffic through monitoring.
        if gateway.get('router_id') and owner is None:
            continue
        identity = 'gateway:' + gateway['id']
        url = generation_url(gateway.get('business_url'))
        legacy = gateway.get('legacy_probe_environment', '')
        migrated = gateway.get('probe_migrated_at')
        if legacy and (legacy not in LEGACY_OFFSETS or not isinstance(migrated, (int, float)) or migrated <= 0):
            raise ValueError('Gateway probe history needs an explicit migration boundary')
        reason = ('disabled' if not gateway.get('enabled', True) or (owner and not owner.get('enabled', True)) else
                  'awaiting_router' if not owner else
                  'awaiting_adapter' if owner.get('monitoring_adapter') not in ADAPTERS else
                  'awaiting_endpoint' if not url else '')
        result.append({'probe_id': identity, 'probe_kind': 'gateway', 'gateway_id': gateway['id'],
                       'environment': legacy, 'name': gateway['name'], 'router_id': gateway.get('router_id'),
                       'url': url, 'offset_seconds': offset(identity, legacy),
                       'dispatch_status': 'paused' if reason else 'active', 'pause_reason': reason,
                       'legacy_probe_environment': legacy, 'probe_migrated_at': migrated})
    if len({row['probe_id'] for row in result}) != len(result):
        raise ValueError('Duplicate probe identity')
    mapped = [row['legacy_probe_environment'] for row in result if row['legacy_probe_environment']]
    if len(set(mapped)) != len(mapped):
        raise ValueError('Duplicate legacy probe history mapping')
    return result


def public(target):
    return {key: value for key, value in target.items() if key not in ('url', 'expected_router')}


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w') as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, separators=(',', ':'))
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
