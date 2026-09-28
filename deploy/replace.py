"""Replace an owned container; discard its backup only after data acceptance."""
import argparse
import copy
import http.client
import json
import socket
import subprocess
import time

from container_validation import ComponentProbe, require, wait_ready


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


def restore(name, old, backup, image):
    current = find(name)
    if current and current['Id'] != old['Id']:
        saved = find(backup)
        require(saved and saved['Id'] == old['Id'], 'Original backup identity changed; refusing removal')
        require(current['Image'] == image and current['Config'].get('Labels', {}).get('monitoring.transaction') == backup,
                'Concurrent container change; refusing removal')
        cmd('docker', 'rm', '-f', current['Id'])
        current = None
    if current is None:
        saved = find(backup)
        require(saved and saved['Id'] == old['Id'], 'Original backup is missing')
        cmd('docker', 'rename', old['Id'], name)
    if not inspect(old['Id'])['State']['Running']:
        cmd('docker', 'start', old['Id'])
    require(inspect(name)['Id'] == old['Id'] and inspect(old['Id'])['State']['Running'], 'Original container did not recover')


def replace(name, image, driver_readonly=False, loadavg=False, allowed_clients=None):
    old = inspect(name)
    require(old['Config'].get('Labels', {}).get('monitoring.owner') == 'independent', 'Container is not independently owned')
    require(old['State']['Running'], 'Original container must be running')
    probe = ComponentProbe(name, old)
    image = json.loads(cmd('docker', 'image', 'inspect', image))[0]['Id']
    backup = name + '-rollback-' + str(time.time_ns())
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
    try:
        cmd('docker', 'stop', old['Id'])
        cmd('docker', 'rename', old['Id'], backup)
        identity = create(name, config)
        cmd('docker', 'start', identity)
        wait_ready(inspect, identity, image, probe)
        require(inspect(name)['Id'] == identity, 'Candidate name changed before acceptance completed')
    except BaseException as error:
        error.add_note('Automatic rollback is disabled; preserve current containers and fix forward. Backup: ' + backup)
        raise
    require(inspect(backup)['Id'] == old['Id'], 'Backup identity changed before cleanup')
    cmd('docker', 'rm', old['Id'])
    return {'container': name, 'temporary_container_removed': backup, 'image': image, 'acceptance': 'passed'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('name')
    parser.add_argument('image')
    parser.add_argument('--driver-readonly', action='store_true')
    parser.add_argument('--loadavg', action='store_true')
    parser.add_argument('--allowed-clients')
    args = parser.parse_args()
    print(json.dumps(replace(args.name, args.image, args.driver_readonly, args.loadavg, args.allowed_clients)))


if __name__ == '__main__':
    main()
