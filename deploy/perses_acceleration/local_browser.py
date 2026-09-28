"""Isolated 1080p acceptance servers using the unchanged perf.3 frontend image."""
import argparse
import copy
import json
import secrets
import subprocess
import time
import sys
import urllib.request
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--accelerated', action='store_true')
    args = parser.parse_args()
    root = args.evidence.resolve()
    image = subprocess.check_output(['docker', 'image', 'inspect', 'monitoring-perses:0.54.0-perf.3',
                                     '--format', '{{.Id}}']).decode().strip()
    for version, port, filename in [('baseline', 18548, 'initial-snapshot.json'), ('candidate', 18549, 'merged-snapshot.json')]:
        folder = root / ('browser-' + version)
        folder.mkdir(exist_ok=True); folder.chmod(0o777)
        config = root / ('browser-' + version + '.yaml')
        if not config.exists():
            config.write_text('security:\n  enable_auth: false\n  readonly: false\n  encryption_key: '
                + json.dumps(secrets.token_urlsafe(24))
                + '\ndatabase:\n  file:\n    folder: /perses\n    extension: json\nephemeral_dashboard:\n  enable: false\n')
        name = 'perses-acceleration-' + version
        exists = subprocess.check_output(['docker', 'ps', '-aq', '--filter', 'name=^/' + name + '$']).strip()
        if not exists:
            subprocess.run(['docker', 'run', '-d', '--name', name, '--platform', 'linux/amd64',
                '--cpus', '1', '--memory', '1g', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
                '-p', f'127.0.0.1:{port}:8080', '-v', str(folder) + ':/perses',
                '-v', str(config) + ':/etc/perses/config.yaml:ro', image,
                '--config=/etc/perses/config.yaml', '--web.listen-address=0.0.0.0:8080'], check=True)
        base = f'http://127.0.0.1:{port}'
        for _ in range(120):
            try:
                urllib.request.urlopen(base + '/api/v1/health', timeout=1).close(); break
            except OSError:
                time.sleep(.25)
        else:
            raise RuntimeError('Local server did not start')
        resources = json.loads((root / filename).read_text())
        if version == 'candidate' and args.accelerated:
            sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'perses'))
            from acceleration_publication import published
            from acceleration_catalog import targets
            original = resources
            resources = published(resources, {'schema': 1, 'merges': [], 'groups': ['cpu', 'dcu', 'a3']})
            changes = []
            for project, dashboard, panel, group in targets():
                old = next(d for d in original['dashboards'] if (d['metadata']['project'], d['metadata']['name']) == (project, dashboard))
                new = next(d for d in resources['dashboards'] if (d['metadata']['project'], d['metadata']['name']) == (project, dashboard))
                changes.append({'project': project, 'dashboard': dashboard, 'panel': panel,
                    'before': old['spec']['panels'][panel], 'after': new['spec']['panels'][panel]})
            (root / 'materialized-changes.json').write_text(json.dumps(changes, ensure_ascii=False))
        for category in ('projects', 'datasources', 'dashboards'):
            for original in resources[category]:
                document = copy.deepcopy(original)
                name = document['metadata']['name']
                project = document['metadata'].get('project')
                document['metadata'] = {'name': name, **({'project': project} if project else {})}
                path = '/api/v1/projects' + ('/' + project + '/' + category if project else '')
                if category == 'datasources':
                    document['spec']['plugin']['spec']['proxy']['spec']['url'] = 'http://host.docker.internal:18550/internal/perses' if name == 'perses-accelerated' else 'http://host.docker.internal:18547'
                headers = {'Content-Type': 'application/json'}
                try:
                    urllib.request.urlopen(base + path + '/' + name, timeout=5).close()
                    request = urllib.request.Request(base + path + '/' + name, method='PUT', data=json.dumps(document).encode(), headers=headers)
                except urllib.error.HTTPError as error:
                    if error.code != 404: raise
                    request = urllib.request.Request(base + path, data=json.dumps(document).encode(), headers=headers)
                urllib.request.urlopen(request, timeout=15).close()
        print(version, base, image, flush=True)


if __name__ == '__main__':
    main()
