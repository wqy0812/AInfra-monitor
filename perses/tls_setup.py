"""Enable self-signed IP TLS on the existing Perses container on test4.

Run through SSH MCP in an approved maintenance window. Failures retain current
state for repair; never roll back, reset resources, or regenerate an existing key.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent / 'performance'))
import image_release
from connection import opener
from release_support import save

ROOT = Path('/data2/monitoring/perses')
IP = '122.247.53.162'
ADDRESS = IP + ':18431'
NAME = 'monitoring-perses'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def apply(args):
    evidence = args.evidence.resolve()
    assert evidence.is_dir() and not (evidence / 'tls-started.json').exists()
    assert image_release.run('hostname') == 'dkfhc8287nap002'
    original = image_release.inspect(NAME)
    config = ROOT / 'config.yaml'
    assert original['Id'] == args.expected_container
    assert original['Image'] == args.expected_image
    assert sha(config) == args.expected_config_sha256
    assert original['Config']['Labels']['monitoring.owner'] == 'perses'
    assert original['State']['Running']
    assert not any('web.tls-' in x for x in original['Config']['Cmd'])
    text = config.read_text()
    assert text.count('security:\n') == 1 and '  cookie:' not in text
    updated = text.replace('security:\n', 'security:\n  cookie:\n    secure: true\n', 1)
    before = image_release.resources('http://' + ADDRESS)
    save(evidence, 'resources-before.json', before)
    tls = ROOT / 'tls'
    assert not tls.exists(), 'Existing TLS material requires explicit reconciliation'
    tls.mkdir(mode=0o750)
    tls.chmod(0o750)  # Override the private evidence umask for container traversal.
    os.chown(tls, 0, 65532)
    cert, key = tls / 'server.crt', tls / 'server.key'
    openssl_config = evidence / 'openssl.cnf'
    openssl_config.write_text('[req]\ndistinguished_name=dn\nx509_extensions=server\n'
        'prompt=no\n[dn]\nCN=' + IP + '\n[server]\nsubjectAltName=IP:' + IP + '\n'
        'basicConstraints=critical,CA:FALSE\n'
        'keyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n')
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:3072', '-sha256',
                    '-nodes', '-days', '365', '-keyout', str(key), '-out', str(cert),
                    '-config', str(openssl_config)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    os.chown(key, 0, 65532)
    key.chmod(0o640)
    cert.chmod(0o644)
    certificate = image_release.run('openssl', 'x509', '-in', str(cert), '-noout',
                                   '-dates', '-fingerprint', '-sha256', '-ext', 'subjectAltName')
    candidate = copy.deepcopy(original)
    candidate['Config']['Cmd'] += [
        '--web.tls-cert-file=/etc/perses/tls/server.crt',
        '--web.tls-key-file=/etc/perses/tls/server.key',
        '--web.tls-min-version=1.2']
    binds = original['HostConfig']['Binds'] + [str(tls) + ':/etc/perses/tls:ro']
    assert image_release.resources('http://' + ADDRESS) == before, 'Concurrent resource edit'
    assert image_release.inspect(NAME)['Id'] == original['Id']
    assert sha(config) == args.expected_config_sha256, 'Concurrent config edit'
    save(evidence, 'tls-started.json', {
        'at': time.time(), 'container': original['Id'], 'image': original['Image'],
        'config_sha256': sha(config), 'certificate': certificate,
        'new_directories': [str(evidence), str(tls)]})
    started = time.time()
    image_release.run('systemctl', 'stop', 'monitoring-perses.service')
    # Recheck after shutdown, before removing the stopped container.
    assert image_release.inspect(NAME)['Id'] == original['Id']
    assert sha(config) == args.expected_config_sha256
    stat = config.stat()
    temporary = config.with_suffix('.tls.tmp')
    assert not temporary.exists()
    temporary.write_text(updated)
    os.chmod(temporary, stat.st_mode & 0o777)
    os.chown(temporary, stat.st_uid, stat.st_gid)
    temporary.replace(config)
    image_release.run('docker', 'rm', original['Id'])
    image_release.create(candidate, NAME, original['Image'], binds, ADDRESS)
    image_release.run('systemctl', 'start', 'monitoring-perses.service')
    verify(evidence, before, original['Image'], started)


def verify(evidence, before, expected_image, started):
    """Read-only acceptance, also usable after a fix-forward repair."""
    cert = ROOT / 'tls/server.crt'
    certificate = image_release.run('openssl', 'x509', '-in', str(cert), '-noout',
                                   '-dates', '-fingerprint', '-sha256', '-ext', 'subjectAltName')
    base = 'https://' + ADDRESS
    health = image_release.health(base)
    current = image_release.inspect(NAME)
    assert current['Image'] == expected_image and current['State']['Running']
    assert health['version'] == '0.54.0-perf.3', health
    after = image_release.resources(base)
    save(evidence, 'resources-after.json', after)
    assert after == before, 'Resources changed across TLS cutover'
    protocol = image_release.run('curl', '--noproxy', '*', '--cacert', str(cert),
                                '--http2', '--silent', '--show-error', '--fail',
                                '--max-time', '15', '-o', '/dev/null',
                                '-w', '%{http_version}', base + '/api/v1/health')
    assert protocol == '2', protocol
    client = opener(direct=True)
    try:
        client.open(base + '/api/v1/projects', timeout=10)
    except urllib.error.HTTPError as error:
        assert error.code == 401
    else:
        raise AssertionError('Unauthenticated resource access must be denied')
    request = urllib.request.Request(base + '/api/auth/providers/native/login',
        data=(ROOT / 'admin-credentials.json').read_bytes(),
        headers={'Content-Type': 'application/json'})
    with client.open(request, timeout=20) as response:
        cookies = response.headers.get_all('Set-Cookie') or []
        assert cookies and all('; secure' in c.lower() for c in cookies)
        json.load(response)
    proxies = []
    api = image_release.client(base)
    for route, documents in after.items():
        if not route.endswith('/datasources'):
            continue
        project = route.split('/')[0]
        for document in documents:
            if document['spec']['plugin']['kind'] != 'PrometheusDatasource':
                continue
            name = document['metadata']['name']
            # Actual datasource proxy transport, with a bounded read-only query.
            query = ('up' if name == 'victoriametrics' else
                     'monitoring_perses_complete')
            path = '/proxy/projects/' + project + '/datasources/' + name
            result = api.get(path + '/api/v1/query?' + urllib.parse.urlencode({'query': query}))
            assert result['status'] == 'success' and result['data']['result']
            proxies.append({'project': project, 'datasource': name,
                            'series': len(result['data']['result'])})
    report = {'passed': True, 'at': time.time(), 'https_url': base, 'http_version': protocol,
              'image': current['Image'], 'version': health['version'],
              'container': current['Id'], 'resources_unchanged': True,
              'unauthenticated_status': 401, 'secure_cookies': True,
              'datasource_queries': proxies, 'certificate': certificate,
              'stop_to_acceptance_seconds': round(time.time() - started, 2),
              'new_directories': [str(evidence), str(ROOT / 'tls')]}
    save(evidence, 'tls-publication.json', report)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', required=True, type=Path)
    parser.add_argument('--expected-container', required=True)
    parser.add_argument('--expected-image', required=True)
    parser.add_argument('--expected-config-sha256', required=True)
    args = parser.parse_args()
    try:
        apply(args)
    except BaseException as error:
        save(args.evidence, 'tls-failure.json', {
            'passed': False, 'at': time.time(), 'error': str(error)[:500],
            'recovery': 'fix_forward', 'automatic_rollback': False})
        raise
