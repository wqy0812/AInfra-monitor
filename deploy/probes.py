"""First install of the probe worker using the same immutable image as the API."""
import argparse
import hashlib
import json
from pathlib import Path
import re

from container_validation import ComponentProbe, require, wait_ready
from replace import cmd, find, inspect, save


def install(image, config, state, evidence):
    require(re.fullmatch(r'sha256:[0-9a-f]{64}', image), 'Use an immutable local image ID')
    require(config.is_file() and state.is_dir() and evidence.is_dir(), 'Configuration, state and evidence paths must already exist')
    require(not any(evidence.iterdir()), 'Use a fresh, empty evidence directory')
    require(find('monitoring-probes') is None, 'Worker already exists; use release.py container monitoring-probes')
    # Validate configuration through the committed image, with no network or model call.
    info = json.loads(cmd('docker', 'image', 'inspect', image))[0]
    labels = info.get('Config', {}).get('Labels') or {}
    require(labels.get('org.opencontainers.image.revision') and labels.get('monitoring.component') == 'api',
            'Use a committed monitoring API image containing the probe worker')
    digest = hashlib.sha256(config.read_bytes()).hexdigest()
    cmd('docker', 'run', '--rm', '--network', 'none', '--read-only', '--entrypoint', 'python',
        '-v', str(config)+':/config/probes.json:ro', image, '-c',
        'from monitoring.probe_worker import load_targets; load_targets("/config/probes.json")')
    require(hashlib.sha256(config.read_bytes()).hexdigest() == digest, 'Probe configuration changed')
    save(evidence, 'probe-install-input.json', {'image': image, 'config': str(config), 'config_sha256': digest})
    try:
        identity = cmd('docker', 'run', '-d', '--name', 'monitoring-probes', '--restart', 'unless-stopped',
                       '--network', 'host', '--read-only', '--cpus', '0.5', '--memory', '192m', '--pids-limit', '64',
                       '--label', 'monitoring.owner=independent', '--log-opt', 'max-size=10m', '--log-opt', 'max-file=3',
                       '-v', str(config)+':/config/probes.json:ro', '-v', str(state)+':/state',
                       '-e', 'PROBE_TARGETS_FILE=/config/probes.json', '-e', 'PYTHONDONTWRITEBYTECODE=1',
                       '--entrypoint', 'python', image, '-m', 'uvicorn', 'monitoring.probe_worker:app',
                       '--host', '127.0.0.1', '--port', '18433', '--no-access-log', '--no-proxy-headers').strip()
        worker = inspect(identity)
        wait_ready(inspect, identity, image, ComponentProbe('monitoring-probes', worker))
        require(hashlib.sha256(config.read_bytes()).hexdigest() == digest, 'Probe configuration changed during installation')
        result = {'container': 'monitoring-probes', 'container_id': identity, 'image': image,
                  'config_sha256': digest, 'acceptance': 'scheduler_and_vm_ready'}
        save(evidence, 'probe-install.json', result)
        return result
    except BaseException as error:
        save(evidence, 'probe-install-failure.json', {'error': str(error), 'recovery': 'fix_forward'})
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--state', type=Path, default=Path('/data2/monitoring/state'))
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(install(args.image, args.config.resolve(), args.state.resolve(), args.evidence.resolve())))


if __name__ == '__main__':
    main()
