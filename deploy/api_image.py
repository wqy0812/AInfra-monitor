"""Build committed API sources on a pinned local runtime; never publish or use SSH."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = 'deploy/Dockerfile.api'


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def save(root, name, data):
    path = root / name
    with path.open('x') as output:
        json.dump(data, output, ensure_ascii=False, indent=2)
        output.write('\n')


def source_archive(repo, revision):
    commit = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', '--verify',
                                     '--end-of-options', revision + '^{commit}'], text=True).strip()
    archive = subprocess.check_output(['git', '-C', str(repo), 'archive', commit,
                                       'monitoring', 'requirements.txt', DOCKERFILE])
    sources = {}
    with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
        for member in stream:
            path = PurePosixPath(member.name)
            require(not path.is_absolute() and '..' not in path.parts, 'Invalid source path')
            if member.isdir():
                continue
            require(member.isfile(), 'Only regular source files are supported: ' + member.name)
            require(member.name in ('requirements.txt', DOCKERFILE) or
                    member.name.startswith('monitoring/'), 'Unexpected source: ' + member.name)
            sources[member.name] = stream.extractfile(member).read()
    require('monitoring/api.py' in sources and DOCKERFILE in sources, 'Incomplete committed API sources')
    for line in sources['requirements.txt'].decode().splitlines():
        if line.strip() and not line.startswith('#'):
            require(re.fullmatch(r'[A-Za-z0-9_.-]+==[A-Za-z0-9_.+-]+', line),
                    'Only exact runtime dependency pins are supported: ' + line)
    hashes = {name: sha(data) for name, data in sorted(sources.items())}
    return archive, {'commit': commit, 'source_sha256': sha(json.dumps(hashes, sort_keys=True).encode()),
                     'archive_sha256': sha(archive), 'files': hashes}


def inspect(image):
    return json.loads(subprocess.check_output(['docker', 'image', 'inspect', image]))[0]


def build(repo, revision, base_image, evidence):
    require(evidence.is_dir() and not any(evidence.iterdir()), 'Use an existing empty evidence directory')
    require(re.fullmatch(r'sha256:[0-9a-f]{64}', base_image), 'Base must be an immutable local image ID')
    try:
        archive, manifest = source_archive(repo, revision)
        base = inspect(base_image)
        require(base['Id'] == base_image and base['Os'] == 'linux', 'Unexpected base image identity or OS')
        # A declared volume would hide or discard the source replacement at build/runtime.
        for volume in base['Config'].get('Volumes') or {}:
            path = PurePosixPath(volume)
            source = PurePosixPath('/monitoring/monitoring')
            require(path != source and path not in source.parents and source not in path.parents,
                    'Base image volume overlaps API sources: ' + volume)
        manifest.update(schema=1, component='api', base_image=base_image,
                        architecture=base['Architecture'], builder_sha256=sha(Path(__file__).read_bytes()))
        # BuildKit treats FROM sha256:... as a registry tag. A content-named local
        # alias bridges that limitation; caller/evidence still pin the image ID.
        # Retain it: removing its last tag could delete an originally untagged base.
        base_reference = 'monitoring-api-build-base:sha256-' + base_image.removeprefix('sha256:')
        subprocess.run(['docker', 'image', 'tag', base_image, base_reference], check=True)
        require(inspect(base_reference)['Id'] == base_image, 'Build base reference changed')
        manifest['build_base_reference'] = base_reference
        save(evidence, 'api-build-inputs.json', manifest)
        (evidence / 'api-source.tar').write_bytes(archive)
        # Docker receives the same immutable bytes recorded above, never the dirty checkout.
        with (evidence / 'api-build.log').open('x') as log:
            subprocess.run(['docker', 'build', '--network=none', '--pull=false',
                            '--iidfile', str(evidence / 'api-image-id'),
                            '--build-arg', 'BASE=' + base_reference,
                            '--build-arg', 'BASE_IMAGE_ID=' + base_image,
                            '--build-arg', 'COMMIT=' + manifest['commit'],
                            '--build-arg', 'SOURCE_SHA256=' + manifest['source_sha256'],
                            '-f', DOCKERFILE, '-'], input=archive,
                           stdout=log, stderr=subprocess.STDOUT, check=True)
        require(inspect(base_reference)['Id'] == base_image, 'Build base reference changed during build')
        image_id = (evidence / 'api-image-id').read_text().strip()
        require(re.fullmatch(r'sha256:[0-9a-f]{64}', image_id), 'Build did not return an immutable image ID')
        image = inspect(image_id)
        labels = image['Config'].get('Labels') or {}
        require(image['Id'] == image_id and image['Os'] == base['Os'] and
                image['Architecture'] == base['Architecture'] and
                labels.get('org.opencontainers.image.revision') == manifest['commit'] and
                labels.get('monitoring.component') == 'api' and
                labels.get('monitoring.base-image') == base_image and
                labels.get('monitoring.source-sha256') == manifest['source_sha256'],
                'Built image identity does not match source inputs')
        result = {key: manifest[key] for key in ('commit', 'source_sha256', 'base_image', 'architecture')}
        result.update(image=image_id, inputs_sha256=sha((evidence / 'api-build-inputs.json').read_bytes()))
        save(evidence, 'api-build.json', result)
        return result
    except BaseException as error:
        save(evidence, 'api-build-failure.json', {'type': type(error).__name__, 'error': str(error)})
        raise


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('revision', help='Committed Git revision, including the stable Dockerfile')
    parser.add_argument('--base-image', required=True, help='Local sha256 image ID with matching requirements.txt')
    parser.add_argument('--evidence', type=Path, required=True, help='Existing empty directory for inputs, logs and image identity')
    args = parser.parse_args()
    print(json.dumps(build(ROOT, args.revision, args.base_image, args.evidence.resolve()), ensure_ascii=False))


if __name__ == '__main__':
    main()
