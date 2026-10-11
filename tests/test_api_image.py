"""Committed build inputs, immutable identities, and retained failure evidence."""
import io
import json
from pathlib import Path
import subprocess
import tarfile

import pytest

from deploy import api_image

BASE = 'sha256:' + '1' * 64
IMAGE = 'sha256:' + '2' * 64


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / 'repo'
    (repo / 'monitoring').mkdir(parents=True)
    (repo / 'deploy').mkdir()
    (repo / 'monitoring/api.py').write_text('VERSION = "committed"\n')
    (repo / 'requirements.txt').write_text('fastapi==0.141.1\n')
    (repo / api_image.DOCKERFILE).write_bytes((api_image.ROOT / api_image.DOCKERFILE).read_bytes())
    for args in (['init', '-q'], ['add', '.'],
                 ['-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'fixture']):
        subprocess.run(['git', '-C', str(repo), *args], check=True)
    return repo


def test_build_inputs_exclude_dirty_and_untracked_files(repository):
    (repository / 'monitoring/api.py').write_text('VERSION = "dirty"\n')
    (repository / 'monitoring/secret.env').write_text('untracked secret')
    archive, manifest = api_image.source_archive(repository, 'HEAD')
    with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
        assert stream.extractfile('monitoring/api.py').read() == b'VERSION = "committed"\n'
        assert 'monitoring/secret.env' not in stream.getnames()
    assert manifest['archive_sha256'] == api_image.sha(archive)
    assert manifest['files']['deploy/Dockerfile.api'] == api_image.sha((repository / api_image.DOCKERFILE).read_bytes())


@pytest.mark.parametrize('invalid', ['symlink', 'dependency'])
def test_unsafe_or_unpinned_inputs_are_rejected(repository, invalid):
    if invalid == 'symlink':
        (repository / 'monitoring/link.py').symlink_to('api.py')
    else:
        (repository / 'requirements.txt').write_text('fastapi>=0.141\n')
    subprocess.run(['git', '-C', str(repository), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repository), '-c', 'user.name=Test', '-c',
                    'user.email=test@example.invalid', 'commit', '-qm', 'invalid fixture'], check=True)
    with pytest.raises(RuntimeError, match='regular source files|exact runtime dependency pins'):
        api_image.source_archive(repository, 'HEAD')


def fake_docker(monkeypatch, repository, evidence, *, failure=False, bad_label=False, volume=None):
    archive, manifest = api_image.source_archive(repository, 'HEAD')
    monkeypatch.setattr(api_image, 'source_archive', lambda *args: (archive, manifest.copy()))
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[:3] == ['docker', 'image', 'tag']:
            return
        assert command[:4] == ['docker', 'build', '--network=none', '--pull=false']
        assert kwargs['input'] == (evidence / 'api-source.tar').read_bytes() == archive
        kwargs['stdout'].write('Dependency mismatch' if failure else 'built')
        if failure:
            raise subprocess.CalledProcessError(1, command)
        Path(command[command.index('--iidfile') + 1]).write_text(IMAGE)

    def inspect(image):
        if image.startswith('monitoring-api-build-base:'):
            image = BASE
        assert image in (BASE, IMAGE)
        return {'Id': image, 'Os': 'linux', 'Architecture': 'amd64', 'Config': {
            'Volumes': {volume: {}} if volume else {}, 'Labels': {
                'org.opencontainers.image.revision': 'wrong' if bad_label else manifest['commit'],
                'monitoring.base-image': BASE,
                'monitoring.component': 'api', 'monitoring.source-sha256': manifest['source_sha256']}}}

    monkeypatch.setattr(api_image.subprocess, 'run', run)
    monkeypatch.setattr(api_image, 'inspect', inspect)
    return calls


def test_build_records_source_base_and_final_identity(repository, tmp_path, monkeypatch):
    evidence = tmp_path / 'evidence'
    evidence.mkdir()
    calls = fake_docker(monkeypatch, repository, evidence)
    result = api_image.build(repository, 'HEAD', BASE, evidence)
    assert result['base_image'] == BASE and result['image'] == IMAGE
    assert json.loads((evidence / 'api-build.json').read_text()) == result
    assert result['inputs_sha256'] == api_image.sha((evidence / 'api-build-inputs.json').read_bytes())
    assert [command[:3] for command in calls] == [
        ['docker', 'image', 'tag'], ['docker', 'build', '--network=none']]
    with pytest.raises(RuntimeError, match='existing empty evidence directory'):
        api_image.build(repository, 'HEAD', BASE, evidence)


@pytest.mark.parametrize('failure,bad_label', [(True, False), (False, True)])
def test_failed_build_keeps_inputs_log_and_failure_without_success(repository, tmp_path, monkeypatch, failure, bad_label):
    evidence = tmp_path / 'evidence'
    evidence.mkdir()
    fake_docker(monkeypatch, repository, evidence, failure=failure, bad_label=bad_label)
    with pytest.raises((RuntimeError, subprocess.CalledProcessError)):
        api_image.build(repository, 'HEAD', BASE, evidence)
    assert (evidence / 'api-build-inputs.json').is_file()
    assert (evidence / 'api-build.log').is_file()
    assert (evidence / 'api-build-failure.json').is_file()
    assert not (evidence / 'api-build.json').exists()


def test_source_volume_in_base_is_rejected_before_build(repository, tmp_path, monkeypatch):
    evidence = tmp_path / 'evidence'
    evidence.mkdir()
    calls = fake_docker(monkeypatch, repository, evidence, volume='/monitoring')
    with pytest.raises(RuntimeError, match='volume overlaps API sources'):
        api_image.build(repository, 'HEAD', BASE, evidence)
    assert calls == []


def test_mutable_base_tag_is_rejected_before_docker(tmp_path):
    with pytest.raises(RuntimeError, match='immutable local image ID'):
        api_image.build(tmp_path, 'HEAD', 'monitoring-api:latest', tmp_path)
