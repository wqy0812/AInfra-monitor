"""Prepare and install catalog-driven monitoring in a maintenance window.

Runs on the monitoring host through release.py. No SSH, dependency download,
rollback, historical data deletion, or ad-hoc model requests are performed.
"""
import argparse
import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import sys
import time
import urllib.request
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from monitoring.provisioning import (atomic_json, digest, foundation_errors, perses_resources,
                                    public_catalog, scrape_config, store_credentials, validate_catalog)
from monitoring.native_rules import build as build_rules
from container_validation import require, wait_ready
from replace import cmd, find, inspect, replace, save


COMPONENTS = ('monitoring-vmagent', 'monitoring-vmalert', 'monitoring-api', 'monitoring-probes')


def template_hashes(root):
    paths = [root / 'dashboard_columns.py', *sorted((root / 'projects').rglob('*.json'))]
    require(len(paths) > 1 and all(path.is_file() and not path.is_symlink() for path in paths), 'Missing regular Perses templates')
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def cli_settings(state, templates, args):
    from monitoring.controller import Settings
    return Settings(state=state, catalog=state / 'catalog.json', control=state / 'probes-control.json',
                    probe_status=state / 'probes-runtime.json', token='offline-preparation-not-a-runtime-token',
                    allowed_clients={'127.0.0.1'}, templates=templates, perses=args.perses_url,
                    credentials=args.perses_credentials, ca_file=str(args.perses_ca) if args.perses_ca else None)


async def prepare(args):
    from monitoring.controller import Controller, Publisher
    state, evidence, templates = args.state.resolve(), args.evidence.resolve(), args.templates.resolve()
    require(evidence.is_dir() and not any(evidence.iterdir()), 'Use an existing empty evidence directory')
    require(not state.exists() or not any(state.iterdir()), 'Prepare into an empty managed state directory')
    body = validate_catalog(json.loads(args.catalog.read_text()))
    incomplete = {f['id']: foundation_errors(f) for f in body['foundations'] if f.get('enabled', True) and foundation_errors(f)}
    require(not incomplete, 'Resolve incomplete catalog before takeover: ' + json.dumps(incomplete))
    created = []
    if not state.exists():
        state.mkdir(parents=False, mode=0o700)
        created.append(str(state))
    templates_digest = template_hashes(templates)
    safe = store_credentials(body, state)
    if (state / 'secrets').exists():
        created.append(str(state / 'secrets'))
    atomic_json(state / 'scrape.json', scrape_config(safe))
    atomic_json(state / 'rules.json', build_rules(safe)[0])
    atomic_json(state / 'catalog.json', public_catalog(safe))
    atomic_json(state / 'probes-catalog.json', public_catalog(safe))
    atomic_json(state / 'probes-control.json', {'revision': body['revision'], 'heartbeat_at': 0, 'paused_routers': {}})
    publisher = Publisher(cli_settings(state, templates, args))
    resources = {}
    try:
        for foundation in safe['foundations']:
            candidate = perses_resources(foundation, safe, templates)
            if args.adopt_perses:
                for documents in candidate.values():
                    for document in documents:
                        path = Controller.resource_path(document) + '/' + document['metadata']['name']
                        actual = await publisher.perses_request('GET', path)
                        if actual is not None:
                            resources[path] = {'owner': foundation['id'], 'applied_digest': digest(Controller.resource_spec(actual))}
                            # Only resource content, never credentials or access tokens.
                            save(evidence, 'perses-' + hashlib.sha256(path.encode()).hexdigest()[:16] + '.json', actual)
    finally:
        await publisher.close()
    atomic_json(state / 'integration-state.json', {'accepted_revision': 0, 'applied_revision': 0,
                                                 'foundations': [], 'gateways': [], 'resources': resources})
    result = {'schema_version': 1, 'catalog_revision': body['revision'], 'state': str(state),
              'templates': str(templates), 'template_hashes': templates_digest,
              'files': {name: hashlib.sha256((state / name).read_bytes()).hexdigest() for name in
                        ('scrape.json', 'rules.json', 'catalog.json', 'probes-catalog.json')},
              'adopted_resources': len(resources), 'created_directories': created}
    atomic_json(state / 'prepared.json', result)
    save(evidence, 'prepare.json', result)
    return result


def consumer_config(name, config, state):
    """Change only monitoring mounts/arguments, preserving all storage and limits."""
    bindings = config['HostConfig'].setdefault('Binds', [])
    target = str(state)
    binding = target + ':' + target + (':rw' if name == 'monitoring-probes' else ':ro')
    old = [b for b in bindings if b.split(':')[1] == target]
    require(not old or old == [binding], 'Conflicting managed mount')
    if not old:
        bindings.append(binding)
    values = {'MONITORING_CATALOG_FILE': str(state / 'catalog.json'),
              'MONITORING_PROBE_CATALOG_FILE': str(state / 'probes-catalog.json'),
              'MONITORING_PROBE_CONTROL_FILE': str(state / 'probes-control.json'),
              'MONITORING_PROBE_STATUS_FILE': str(state / 'probes-runtime.json')}
    if name == 'monitoring-probes':
        values['STATE_DIR'] = target
    config['Env'] = [v for v in config.get('Env', []) if v.split('=', 1)[0] not in values]
    config['Env'].extend(key + '=' + value for key, value in values.items())
    if name == 'monitoring-vmagent':
        config['Cmd'] = [v for v in config['Cmd'] if not v.startswith('-promscrape.config=')]
        config['Cmd'].append('-promscrape.config=' + str(state / 'scrape.json'))
    elif name == 'monitoring-vmalert':
        config['Cmd'] = [v for v in config['Cmd'] if not v.startswith('--rules-file=')]
        config['Cmd'].append('--rules-file=' + str(state / 'rules.json'))


class ManagedProbe:
    def __init__(self, name, config, state):
        self.name, self.state = name, state
        self.base = {'monitoring-api': 'http://127.0.0.1:18430', 'monitoring-probes': 'http://127.0.0.1:18433',
                     'monitoring-vmagent': 'http://127.0.0.1:18429', 'monitoring-vmalert': 'http://127.0.0.1:18432'}[name]

    def check(self, deadline):
        path = '/api/v1/rules' if self.name == 'monitoring-vmalert' else '/health'
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(self.base + path, timeout=min(4, deadline - time.monotonic())) as response:
            body = response.read()
        if self.name == 'monitoring-vmalert':
            value = json.loads(body)
            require(value.get('status') == 'success', 'Native rules unavailable')
            groups = value['data']['groups']
            expected = json.loads((self.state / 'rules.json').read_text())['groups']
            require({g['name']: len(g['rules']) for g in groups} == {g['name']: len(g['rules']) for g in expected}, 'Native rule count mismatch')
            require(not any(r.get('lastError') for g in groups for r in g['rules']), 'Native rule evaluation failed')
        elif self.name == 'monitoring-probes':
            require(json.loads(body).get('status') == 'ok', 'Probe worker or VM unavailable')
        elif self.name == 'monitoring-api':
            # A newly registered target may be down. The query service must remain
            # reachable and report that truth rather than prevent forward repair.
            require(json.loads(body).get('status') in ('ok', 'degraded'), 'API failed')


def pinned_image(image):
    require(re.fullmatch(r'sha256:[0-9a-f]{64}', image), 'Use immutable local image IDs')
    return json.loads(cmd('docker', 'image', 'inspect', image))[0]


def install(args):
    state, evidence = args.state.resolve(), args.evidence.resolve()
    require(evidence.is_dir() and not any(evidence.iterdir()), 'Use an existing empty evidence directory')
    prepared = json.loads((state / 'prepared.json').read_text())
    templates = Path(prepared['templates'])
    require(template_hashes(templates) == prepared['template_hashes'], 'Perses templates changed after preparation')
    for name, signature in prepared['files'].items():
        require(hashlib.sha256((state / name).read_bytes()).hexdigest() == signature, 'Managed candidate changed: ' + name)
    api = pinned_image(args.image)
    native = pinned_image(args.native_image)
    require(api['Architecture'] == native['Architecture'], 'Image architecture mismatch')
    require(api.get('Config', {}).get('Labels', {}).get('monitoring.component') == 'api', 'Expected committed API image')
    for path in (args.token_file, args.tls_cert, args.tls_key, args.perses_credentials, args.perses_ca):
        require(path is not None and path.is_file(), 'Controller TLS, token and Perses credential files must exist')
    require(len(args.token_file.read_text().strip()) >= 32, 'Controller token too short')
    from monitoring.access import client_allowlist
    clients = ','.join(dict.fromkeys(['127.0.0.1', '::1', *client_allowlist(args.allowed_clients)]))
    require(find('monitoring-controller') is None, 'Controller already exists; inspect and update current version')
    current = {name: inspect(name) for name in COMPONENTS}
    require(all(c['Config'].get('Labels', {}).get('monitoring.owner') == 'independent' for c in current.values()), 'Consumer ownership mismatch')
    binaries = state / 'binaries'
    binaries.mkdir(mode=0o700, exist_ok=True)
    # Copy the deployed, immutable collector binaries instead of downloading tools.
    cmd('docker', 'cp', current['monitoring-vmagent']['Id'] + ':/vmagent', str(binaries / 'vmagent'))
    cmd('docker', 'cp', current['monitoring-vmalert']['Id'] + ':/vmalert-prod', str(binaries / 'vmalert'))
    result = {'created_directories': [str(binaries)], 'components': [], 'controller': None}
    save(evidence, 'inputs.json', {'image': args.image, 'native_image': args.native_image, 'prepared': prepared,
                                 'binary_hashes': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in binaries.iterdir()}})
    try:
        # Validate both candidates using exactly the binaries mounted in controller.
        for name, binary, flags in [('scrape', 'vmagent', ['-promscrape.config=' + str(state / 'scrape.json'), '-promscrape.config.dryRun']),
                                    ('rules', 'vmalert', ['-rule=' + str(state / 'rules.json'), '-dryRun'])]:
            cmd('docker', 'run', '--rm', '--network', 'none', '--read-only', '-v', str(state) + ':' + str(state) + ':ro',
                '--entrypoint', str(binaries / binary), args.image, *flags)
        # Stop the previous scheduler before changing its identity/configuration.
        cmd('docker', 'stop', current['monitoring-probes']['Id'])
        cutoff_file = state / 'probe-cutover.json'
        cutoff = json.loads(cutoff_file.read_text()) if cutoff_file.exists() else {'probe_cutover_at': time.time(), 'old_probe_container_id': current['monitoring-probes']['Id']}
        atomic_json(cutoff_file, cutoff)
        for filename in ('catalog.json', 'probes-catalog.json'):
            value = json.loads((state / filename).read_text())
            for gateway in value['gateways']:
                if gateway.get('legacy_probe_environment'):
                    gateway['probe_migrated_at'] = cutoff['probe_cutover_at']
            atomic_json(state / filename, value)
            prepared['files'][filename] = hashlib.sha256((state / filename).read_bytes()).hexdigest()
        atomic_json(state / 'prepared.json', prepared)
        result.update(cutoff)
        save(evidence, 'probe-cutover.json', cutoff)
        for name in COMPONENTS:
            component_evidence = evidence / name
            component_evidence.mkdir()
            result['created_directories'].append(str(component_evidence))
            image = args.native_image if name == 'monitoring-vmalert' else args.image if name in ('monitoring-api', 'monitoring-probes') else current[name]['Image']
            result['components'].append(replace(name, image, evidence=component_evidence,
                configure=lambda config, name=name: consumer_config(name, config, state),
                probe_factory=lambda name, config: ManagedProbe(name, config, state)))
        mounts = [(state, state, 'rw'), (templates, Path('/templates'), 'ro'),
                  (args.token_file, Path('/controller/token'), 'ro'), (args.tls_cert, Path('/controller/tls.crt'), 'ro'),
                  (args.tls_key, Path('/controller/tls.key'), 'ro'), (args.perses_credentials, Path('/controller/perses-credentials.json'), 'ro'),
                  (args.perses_ca, Path('/controller/perses-ca.crt'), 'ro')]
        command = ['docker', 'run', '-d', '--name', 'monitoring-controller', '--restart', 'unless-stopped', '--network', 'host',
                   '--read-only', '--cpus', '1', '--memory', '512m', '--pids-limit', '128',
                   '--label', 'monitoring.owner=independent', '--log-opt', 'max-size=10m', '--log-opt', 'max-file=3']
        for source, target, mode in mounts:
            command.extend(['-v', str(source.resolve()) + ':' + str(target) + ':' + mode])
        values = {'STATE_DIR': str(state), 'MONITORING_CATALOG_FILE': str(state / 'catalog.json'),
                  'MONITOR_CONTROL_TOKEN_FILE': '/controller/token', 'PERSES_TEMPLATES_DIR': '/templates',
                  'PERSES_CREDENTIALS_FILE': '/controller/perses-credentials.json', 'PERSES_CA_FILE': '/controller/perses-ca.crt',
                  'PERSES_URL': args.perses_url, 'ALLOWED_CLIENTS': clients, 'PYTHONDONTWRITEBYTECODE': '1',
                  'MONITOR_CONTROLLER_PUBLIC_URL': args.controller_url or ('https://' + urlsplit(args.perses_url).hostname + ':' + str(args.port)),
                  'VMAGENT_BINARY': str(binaries / 'vmagent'), 'VMALERT_BINARY': str(binaries / 'vmalert')}
        for key, value in values.items():
            command.extend(['-e', key + '=' + value])
        command.extend(['--entrypoint', 'python', args.image, '-m', 'uvicorn', 'monitoring.controller:app',
                        '--host', args.bind, '--port', str(args.port), '--ssl-certfile', '/controller/tls.crt',
                        '--ssl-keyfile', '/controller/tls.key', '--no-access-log', '--no-proxy-headers'])
        identity = cmd(*command).strip()
        controller_url = values['MONITOR_CONTROLLER_PUBLIC_URL']
        context = ssl.create_default_context(cafile=str(args.tls_cert))
        class ControllerHealth:
            def check(self, deadline):
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context))
                with opener.open(controller_url + '/health', timeout=min(4, deadline - time.monotonic())) as response:
                    require(json.load(response).get('status') == 'ok', 'Controller unhealthy')
        wait_ready(inspect, identity, args.image, ControllerHealth())
        result['controller'] = {'container_id': identity, 'image': args.image, 'port': args.port}
        save(evidence, 'installation.json', result)
        return result
    except BaseException as error:
        save(evidence, 'installation-failure.json', {'error': str(error), 'recovery': 'fix_forward', 'result': result})
        raise


def prepare_container(args):
    """Use pinned runtime dependencies; deploy host itself remains stdlib-only."""
    require(args.image is not None, 'prepare requires --image with a committed API image')
    image = pinned_image(args.image)
    require(image.get('Config', {}).get('Labels', {}).get('monitoring.component') == 'api', 'Expected committed API runtime')
    args.state = args.state.resolve()
    created = []
    if not args.state.exists():
        args.state.mkdir(mode=0o700)
        created.append(str(args.state))
    deploy = Path(__file__).resolve().parent
    command = ['docker', 'run', '--rm', '--network', 'host', '--read-only', '--cpus', '1', '--memory', '512m',
               '-e', 'PYTHONDONTWRITEBYTECODE=1', '-e', 'PYTHONPATH=/monitoring',
               '-v', str(deploy) + ':/integration-tool/deploy:ro']
    paths = [(args.state, 'rw'), (args.evidence.resolve(), 'rw'), (args.catalog.resolve(), 'ro'), (args.templates.resolve(), 'ro')]
    for optional in (args.perses_credentials, args.perses_ca):
        if optional:
            paths.append((optional.resolve(), 'ro'))
    for path, mode in paths:
        require(path.exists(), 'Required preparation path missing: ' + str(path))
        command.extend(['-v', str(path) + ':' + str(path) + ':' + mode])
    command.extend(['--entrypoint', 'python', args.image, '/integration-tool/deploy/integrations.py', 'prepare', '--in-container',
                    '--state', str(args.state), '--evidence', str(args.evidence.resolve()), '--catalog', str(args.catalog.resolve()),
                    '--templates', str(args.templates.resolve()), '--perses-url', args.perses_url])
    for field, option in [('perses_credentials', '--perses-credentials'), ('perses_ca', '--perses-ca')]:
        if getattr(args, field):
            command.extend([option, str(getattr(args, field).resolve())])
    if args.adopt_perses:
        command.append('--adopt-perses')
    result = json.loads(cmd(*command, timeout=180))
    result['created_directories'] = created + result['created_directories']
    save(args.evidence, 'prepare.json', result)
    return result


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    for name in ('prepare', 'install'):
        command = commands.add_parser(name)
        command.add_argument('--state', type=Path, required=True)
        command.add_argument('--evidence', type=Path, required=True)
        command.add_argument('--perses-url', default='https://122.247.53.162:18431')
        command.add_argument('--perses-credentials', type=Path)
        command.add_argument('--perses-ca', type=Path)
        if name == 'prepare':
            command.add_argument('--image', help='Immutable API runtime used for preparation; host needs only stdlib')
            command.add_argument('--in-container', action='store_true', help=argparse.SUPPRESS)
            command.add_argument('--catalog', type=Path, required=True)
            command.add_argument('--templates', type=Path, required=True)
            command.add_argument('--adopt-perses', action='store_true', help='Record read-only current resource hashes as takeover baselines')
        else:
            command.add_argument('--image', required=True)
            command.add_argument('--native-image', required=True)
            command.add_argument('--token-file', type=Path, required=True)
            command.add_argument('--tls-cert', type=Path, required=True)
            command.add_argument('--tls-key', type=Path, required=True)
            command.add_argument('--allowed-clients', required=True)
            command.add_argument('--bind', default='0.0.0.0')
            command.add_argument('--port', type=int, default=18434)
            command.add_argument('--controller-url', help='Reachable HTTPS URL matching the controller certificate for health checks')
    args = parser.parse_args()
    value = (asyncio.run(prepare(args)) if args.in_container else prepare_container(args)) if args.action == 'prepare' else install(args)
    print(json.dumps(value, ensure_ascii=False))


if __name__ == '__main__':
    main()
