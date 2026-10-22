"""Managed deployment mutates only intended consumer config and validates input."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from deploy.integrations import consumer_config, prepare_container, template_hashes


def config():
    return {'Env': ['UNCHANGED=value', 'STATE_DIR=/state'], 'Cmd': ['-promscrape.config=/config/scrape.yml', '-remoteWrite.tmpDataPath=/buffer'],
            'HostConfig': {'Binds': ['/data2/monitoring/buffer:/buffer', '/data2/monitoring/vm:/storage', '/data2/monitoring/state:/state'],
                           'Memory': 123, 'NetworkMode': 'host'}}


@pytest.mark.parametrize('component', ['monitoring-vmagent', 'monitoring-vmalert', 'monitoring-api', 'monitoring-probes'])
def test_consumer_configuration_keeps_history_limits_and_identity(component, tmp_path):
    value = config()
    original = copy.deepcopy(value)
    consumer_config(component, value, tmp_path)
    assert value['HostConfig']['Binds'][:3] == original['HostConfig']['Binds']
    assert value['HostConfig']['Memory'] == original['HostConfig']['Memory']
    assert value['HostConfig']['NetworkMode'] == 'host'
    assert 'UNCHANGED=value' in value['Env']
    assert 'MONITORING_CATALOG_FILE=' + str(tmp_path / 'catalog.json') in value['Env']
    assert 'MONITORING_PROBE_CATALOG_FILE=' + str(tmp_path / 'probes-catalog.json') in value['Env']
    assert value['HostConfig']['Binds'][-1].endswith(':rw' if component == 'monitoring-probes' else ':ro')
    consumer_config(component, value, tmp_path)
    assert len(value['HostConfig']['Binds']) == 4
    if component == 'monitoring-vmagent':
        assert '-remoteWrite.tmpDataPath=/buffer' in value['Cmd']
        assert '-promscrape.config=' + str(tmp_path / 'scrape.json') in value['Cmd']
        assert '-promscrape.config=/config/scrape.yml' not in value['Cmd']


def test_conflicting_managed_mount_is_rejected(tmp_path):
    value = config()
    value['HostConfig']['Binds'].append('/different:' + str(tmp_path) + ':ro')
    with pytest.raises(RuntimeError, match='Conflicting managed mount'):
        consumer_config('monitoring-api', value, tmp_path)


def test_template_inputs_include_every_source_and_detect_change(tmp_path):
    (tmp_path / 'projects').mkdir()
    (tmp_path / 'dashboard_columns.py').write_text('VERSION = 1')
    (tmp_path / 'projects/a.json').write_text('{}')
    first = template_hashes(tmp_path)
    assert set(first) == {'dashboard_columns.py', 'projects/a.json'}
    (tmp_path / 'projects/a.json').write_text('{"new":true}')
    assert template_hashes(tmp_path) != first


def test_prepare_uses_pinned_runtime_not_host_dependencies(tmp_path, monkeypatch):
    from deploy import integrations
    catalog, templates, evidence, state = [tmp_path / name for name in ('catalog.json', 'templates', 'evidence', 'managed')]
    catalog.write_text('{}')
    templates.mkdir()
    evidence.mkdir()
    image = 'sha256:' + 'a' * 64
    monkeypatch.setattr(integrations, 'pinned_image', lambda ref: {'Config': {'Labels': {'monitoring.component': 'api'}}})
    calls = []
    def command(*args, **kwargs):
        calls.append(args)
        return json.dumps({'created_directories': [], 'status': 'prepared'})
    monkeypatch.setattr(integrations, 'cmd', command)
    result = prepare_container(SimpleNamespace(image=image, state=state, evidence=evidence, catalog=catalog, templates=templates,
        perses_credentials=None, perses_ca=None, perses_url='https://perses.invalid', adopt_perses=True))
    argv = calls[0]
    assert argv[:4] == ('docker', 'run', '--rm', '--network')
    assert image in argv and '--in-container' in argv and '--adopt-perses' in argv
    assert 'PYTHONPATH=/monitoring' in argv
    assert str(state) in result['created_directories']
    assert json.loads((evidence / 'prepare.json').read_text()) == result
