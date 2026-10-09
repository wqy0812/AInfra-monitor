"""Replace an owned container; preserve evidence and fix the current version."""
import argparse
import copy
import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

from container_validation import ComponentProbe, require, wait_ready


def api_clients(value):
    # Direct execution from deploy/ must find the shared, stdlib-only validator.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from monitoring.access import client_allowlist
    return ','.join(client_allowlist(value))


def cmd(*args, timeout=30):
    return subprocess.check_output(args, timeout=timeout).decode()


def inspect(name, timeout=30):
    return json.loads(cmd('docker', 'container', 'inspect', name, timeout=timeout))[0]


def find(name):
    ids = cmd('docker', 'container', 'ls', '-aq', '--filter', 'name=^/' + name + '$').split()
    require(len(ids) <= 1, 'Ambiguous container name: ' + name)
    return inspect(ids[0]) if ids else None


class Connection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect('/var/run/docker.sock')


def create(name, config):
    connection = Connection('localhost', timeout=30)
    try:
        connection.request('POST', '/v1.39/containers/create?name=' + name,
                           json.dumps(config), {'Content-Type': 'application/json'})
        response = connection.getresponse()
        body = response.read()
        require(response.status == 201, 'Docker container creation failed: ' + str(response.status))
        return json.loads(body)['Id']
    finally:
        connection.close()


def save(root, name, data):
    if root is None:
        return
    path = root / name
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    temporary.chmod(0o600)
    temporary.replace(path)


def replace(name, image, driver_readonly=False, loadavg=False, allowed_clients=None, evidence=None):
    if evidence is not None:
        require(evidence.is_dir(), 'Evidence directory must already exist')
        require(not (evidence / 'container-before.json').exists(), 'Use a fresh evidence directory')
    old = inspect(name)
    require(old['Config'].get('Labels', {}).get('monitoring.owner') == 'independent', 'Container is not independently owned')
    probe = ComponentProbe(name, old)
    image = json.loads(cmd('docker', 'image', 'inspect', image))[0]['Id']
    backup = name + '-before-upgrade-' + str(time.time_ns())
    require(find(backup) is None, 'Backup name already exists')
    fields = ('User', 'ExposedPorts', 'Env', 'Cmd', 'Healthcheck', 'Volumes', 'WorkingDir',
              'Entrypoint', 'Labels', 'StopSignal', 'StopTimeout')
    config = copy.deepcopy({k: old['Config'][k] for k in fields if k in old['Config']})
    config.update(Image=image, HostConfig=copy.deepcopy(old['HostConfig']))
    config.setdefault('Labels', {})['monitoring.transaction'] = backup
    # The API allowlist checks transport peers, including the local Perses proxy.
    if name == 'monitoring-api' and '--no-proxy-headers' not in config['Cmd']:
        config['Cmd'].append('--no-proxy-headers')
    if allowed_clients is not None:
        require(name == 'monitoring-api', '--allowed-clients is only for monitoring-api')
        config['Env'] = [v for v in config.get('Env', []) if not v.startswith('ALLOWED_CLIENTS=')]
        config['Env'].append('ALLOWED_CLIENTS=' + allowed_clients)
    if name == 'monitoring-api':
        environment = dict(v.split('=', 1) for v in config.get('Env', []) if '=' in v)
        clients = api_clients(environment.get('ALLOWED_CLIENTS', '127.0.0.1,::1'))
        config['Env'] = [v for v in config.get('Env', []) if not v.startswith('ALLOWED_CLIENTS=')]
        config['Env'].append('ALLOWED_CLIENTS=' + clients)
    if driver_readonly:
        require(name == 'monitoring-dcu', '--driver-readonly is only for monitoring-dcu')
        binds = config['HostConfig'].setdefault('Binds', [])
        if '/opt/hyhal:/opt/hyhal:ro' not in binds:
            binds.append('/opt/hyhal:/opt/hyhal:ro')
    if loadavg:
        require(name == 'monitoring-node', '--loadavg is only for monitoring-node')
        if '--collector.loadavg' not in config['Cmd']:
            config['Cmd'].append('--collector.loadavg')
    require(inspect(name)['Id'] == old['Id'], 'Original container changed before replacement')
    before = {key: old[key] for key in ('Id', 'Image', 'Name', 'State')}
    before['configuration_sha256'] = hashlib.sha256(json.dumps(
        {'Config': old['Config'], 'HostConfig': old['HostConfig']}, sort_keys=True).encode()).hexdigest()
    save(evidence, 'container-before.json', before)
    save(evidence, 'container-transaction.json', {'container': name, 'old_id': old['Id'],
         'image': image, 'temporary_name': backup, 'started_at': time.time()})
    try:
        cmd('docker', 'stop', old['Id'])
        cmd('docker', 'rename', old['Id'], backup)
        identity = create(name, config)
        cmd('docker', 'start', identity)
        wait_ready(inspect, identity, image, probe)
        require(inspect(name)['Id'] == identity, 'Candidate name changed before acceptance completed')
        require(inspect(backup)['Id'] == old['Id'], 'Backup identity changed before cleanup')
        cmd('docker', 'rm', old['Id'])
        result = {'container': name, 'container_id': identity, 'temporary_container_removed': backup,
                  'image': image, 'acceptance': 'passed'}
        save(evidence, 'container-publication.json', result)
    except BaseException as error:
        if hasattr(error, 'add_note'):
            error.add_note('Rollback is disabled; preserve current containers and fix forward. Temporary container: ' + backup)
        try:
            save(evidence, 'container-failure.json', {'error': str(error), 'type': type(error).__name__,
                 'recovery': 'fix_forward', 'automatic_rollback': False, 'at': time.time()})
        except OSError as reporting_error:
            if hasattr(error, 'add_note'):
                error.add_note('Failure report could not be saved: ' + str(reporting_error))
        raise
    return result


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('name')
    parser.add_argument('image')
    parser.add_argument('--driver-readonly', action='store_true')
    parser.add_argument('--loadavg', action='store_true')
    parser.add_argument('--allowed-clients')
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(replace(args.name, args.image, args.driver_readonly, args.loadavg, args.allowed_clients, args.evidence)))


if __name__ == '__main__':
    main()
