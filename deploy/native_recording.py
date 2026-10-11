"""Build, publish and inspect the native recording service in a maintenance window."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from container_validation import require, metrics, scalar
from replace import find, replace, save

ROOT = Path(__file__).resolve().parents[1]


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def image(reference):
    require(re.fullmatch(r'sha256:[0-9a-f]{64}', reference), 'Immutable local image ID required')
    return json.loads(run('docker', 'image', 'inspect', reference))[0]


def fetch(base, path):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(base + path, timeout=5) as response:
        return json.load(response)


def check(base='http://127.0.0.1:18432', expected=158):
    body = fetch(base, '/api/v1/rules')
    require(body.get('status') == 'success', 'Native rules endpoint unavailable')
    groups = body['data']['groups']
    rules = [rule for group in groups for rule in group['rules']]
    require(len(rules) == expected and all(rule.get('type') == 'recording' for rule in rules), 'Native rule catalog incomplete')
    require(all(not rule.get('lastError') and rule.get('health') == 'ok' for rule in rules), 'Native rule evaluation error')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(base + '/metrics', timeout=5) as response:
        counters = metrics(response.read().decode())
    writes = {name: scalar(counters, 'vmalert_remotewrite_' + name) for name in (
        'errors_total', 'dropped_rows_total', 'sent_rows_total')}
    require(writes['dropped_rows_total'] == 0, 'Native remote write dropped rows; inspect and repair coverage')
    return {'rule_count': len(rules), 'groups': len(groups), 'rules': rules, 'remote_write': writes}


def build(api_image, vmalert_image, evidence):
    api, native = image(api_image), image(vmalert_image)
    require(api['Architecture'] == native['Architecture'], 'Image architectures differ')
    require(api['Config'].get('Labels', {}).get('monitoring.component') == 'api', 'Use the committed API build')
    for ref, name in ((api_image, 'api'), (vmalert_image, 'vmalert')):
        run('docker', 'tag', ref, 'monitoring-native-build-' + name + ':' + ref[7:])
    dockerfile = ('FROM monitoring-native-build-vmalert:' + vmalert_image[7:] + ' AS native\n'
                  'FROM monitoring-native-build-api:' + api_image[7:] + '\n'
                  'COPY --from=native /vmalert-prod /vmalert-prod\n'
                  'LABEL monitoring.component="vmalert" monitoring.vmalert-image="' + vmalert_image + '"\n'
                  'ENTRYPOINT ["python", "-m", "monitoring.recording_runtime"]\nCMD []\n')
    path = evidence / 'Dockerfile.native'
    path.write_text(dockerfile)
    with (evidence / 'native-build.log').open('x') as log:
        subprocess.run(['docker', 'build', '--network=none', '--pull=false', '-f', str(path),
                        '--iidfile', str(evidence / 'native-image-id'), str(evidence)],
                       stdout=log, stderr=subprocess.STDOUT, check=True)
    built = image((evidence / 'native-image-id').read_text().strip())
    result = {'image': built['Id'], 'api_image': api_image, 'vmalert_image': vmalert_image,
              'architecture': built['Architecture'], 'commit': api['Config']['Labels']['org.opencontainers.image.revision']}
    save(evidence, 'native-build.json', result)
    return result


def publish(reference, state, evidence):
    pinned = image(reference)
    require(pinned['Config'].get('Labels', {}).get('monitoring.component') == 'vmalert', 'Wrong component image')
    require(state.is_dir(), 'Persistent state must exist')
    current = find('monitoring-vmalert')
    if current:
        return replace('monitoring-vmalert', reference, evidence=evidence)
    # First install is also owned by the stable entry point. No VM data/buffer mounts.
    identity = run('docker', 'run', '-d', '--name', 'monitoring-vmalert', '--restart', 'unless-stopped',
                   '--network', 'host', '--label', 'monitoring.owner=independent',
                   '--cpus', '1', '--memory', '768m', '--log-opt', 'max-size=10m', '--log-opt', 'max-file=3',
                   '-v', str(state) + ':/state', reference, '--listen=127.0.0.1:18432')
    save(evidence, 'native-started.json', {'container_id': identity, 'image': reference})
    deadline = time.monotonic() + 90
    while True:
        try:
            result = check()
            save(evidence, 'native-publication.json', dict(result, image=reference, container_id=identity))
            return result
        except (OSError, ValueError, KeyError, RuntimeError):
            if time.monotonic() >= deadline: raise
            time.sleep(2)


def audit(evidence, filters=False, coverage_only=False, require_full=False):
    # Execute the checked-out acceptance code in the installed API Python runtime.
    # This is read-only: no copied modules or changes to the running container.
    current = find('monitoring-api')
    require(current and current['Config'].get('Labels', {}).get('monitoring.owner') == 'independent',
            'Expected owned API container')
    code = (ROOT / 'monitoring/recording_acceptance.py').read_text()
    flags = [name for name, enabled in (('--filters', filters), ('--coverage-only', coverage_only),
                                      ('--require-full', require_full)) if enabled]
    result = subprocess.run(['docker', 'exec', '-i', current['Id'], 'python', '-c',
        "import sys; __package__='monitoring'; exec(compile(sys.stdin.read(), 'recording_acceptance.py', 'exec'))",
        *flags], input=code, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    (evidence / 'native-audit.json').write_text(result.stdout)
    (evidence / 'native-audit.stderr').write_text(result.stderr)
    require(result.returncode == 0, 'Native audit failed; inspect native-audit evidence')
    report = json.loads(result.stdout)
    require(report['passed'], 'Native audit did not pass')
    return {'passed': True, 'checks': len(report['checks']), 'native_jobs': report['native_jobs'],
            'total_jobs': report['total_jobs'], 'container_id': current['Id']}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('build', 'publish', 'check', 'audit'))
    parser.add_argument('--api-image'); parser.add_argument('--vmalert-image'); parser.add_argument('--image')
    parser.add_argument('--filters', action='store_true')
    parser.add_argument('--coverage-only', action='store_true')
    parser.add_argument('--require-full', action='store_true')
    parser.add_argument('--state', type=Path, default=Path('/data2/monitoring/state'))
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    require(args.evidence.is_dir(), 'Evidence directory must already exist')
    try:
        if args.action == 'build': result = build(args.api_image, args.vmalert_image, args.evidence)
        elif args.action == 'publish': result = publish(args.image, args.state, args.evidence)
        elif args.action == 'audit': result = audit(args.evidence, args.filters, args.coverage_only, args.require_full)
        else:
            result = check(); save(args.evidence, 'native-check.json', result)
        print(json.dumps(result))
    except BaseException as error:
        save(args.evidence, 'native-failure.json', {'error': str(error), 'recovery': 'fix_forward', 'automatic_rollback': False})
        raise


if __name__ == '__main__':
    main()
