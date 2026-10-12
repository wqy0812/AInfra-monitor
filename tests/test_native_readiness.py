"""Native publication requires writes from the new process and fresh VM samples."""
import copy
import io
import json
import os
from pathlib import Path
import sys
import time
import urllib.parse

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy'))
import native_recording as release
from container_validation import ComponentProbe
from monitoring.native_schema import NODES


def records(stamp):
    return [{'metric': {'environment': env, 'node': node, 'role': role}, 'value': [1000, str(stamp)]}
            for env, nodes in NODES.items() for role, node in nodes.items()]


@pytest.fixture
def writer(monkeypatch):
    state = {'sent': 100, 'errors': 0, 'dropped': 0, 'started': 970,
             'rows': records(985), 'status': 'success', 'partial': False, 'reads': []}
    monkeypatch.setattr(release.time, 'time', lambda: 1000)

    class Opener:
        def open(self, url, **kwargs):
            state['reads'].append(url)
            if url == 'http://writer/metrics':
                return io.BytesIO(('process_start_time_seconds {started}\n'
                    'vmalert_remotewrite_sent_rows_total {sent}\n'
                    'vmalert_remotewrite_errors_total {errors}\n'
                    'vmalert_remotewrite_dropped_rows_total {dropped}\n').format(**state).encode())
            if url == 'http://writer/api/v1/rules':
                body = {'status': 'success', 'data': {'groups': [{'rules': [
                    {'type': 'recording', 'health': 'ok', 'lastError': ''}]}]}}
            else:
                assert url.startswith('http://storage/api/v1/query?')
                params = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
                assert params['nocache'] == ['1']
                assert params['query'] == ['tlast_over_time(monitoring_native_backend_expected_instances[30s])']
                body = {'status': state['status'], 'isPartial': state['partial'],
                        'data': {'result': state['rows']}}
            return io.BytesIO(json.dumps(body).encode())

    monkeypatch.setattr(release.urllib.request, 'build_opener', lambda *a: Opener())
    return state


@pytest.mark.parametrize('errors', [0, 12])
def test_no_successful_send_cannot_pass_even_with_old_fresh_records(writer, errors):
    writer.update(sent=0, errors=errors)
    with pytest.raises(RuntimeError, match='has not sent any rows'):
        release.check('http://writer', expected=1, vm='http://storage')
    assert not any(url.startswith('http://storage') for url in writer['reads'])


def test_recovered_errors_pass_only_with_new_fresh_record_readback(writer):
    writer['errors'] = 12
    result = release.check('http://writer', expected=1, vm='http://storage')
    assert result['remote_write']['errors_total'] == 12
    assert result['stored_records'] == {'series': 6, 'oldest_sample_at': 985, 'process_started_at': 970}


@pytest.mark.parametrize('fault', ['missing', 'predates-process', 'stale', 'future', 'nan',
                                 'duplicate', 'wrong-source', 'partial', 'query-error', 'dropped'])
def test_failed_readback_blocks_publication(writer, fault):
    if fault == 'missing': writer['rows'].pop()
    elif fault == 'predates-process': writer['started'] = 990
    elif fault in ('stale', 'future', 'nan'):
        writer['rows'][0]['value'][1] = {'stale': '960', 'future': '1001', 'nan': 'NaN'}[fault]
    elif fault == 'duplicate': writer['rows'].append(copy.deepcopy(writer['rows'][0]))
    elif fault == 'wrong-source': writer['rows'][0]['metric']['node'] = 'wrong'
    elif fault == 'partial': writer['partial'] = True
    elif fault == 'query-error': writer['status'] = 'error'
    elif fault == 'dropped': writer['dropped'] = 1
    with pytest.raises(RuntimeError):
        release.check('http://writer', expected=1, vm='http://storage')


@pytest.mark.parametrize('args,env,expected', [
    ([], [], 'http://127.0.0.1:18428'),
    ([], ['VM_URL=http://env-storage'], 'http://env-storage'),
    (['--vm=http://arg-storage'], ['VM_URL=http://env-storage'], 'http://arg-storage')])
def test_container_probe_reads_the_configured_vm(monkeypatch, args, env, expected):
    calls = []
    monkeypatch.setattr(release, 'check', lambda *a, **k: calls.append((a, k)))
    probe = ComponentProbe('monitoring-vmalert', {'HostConfig': {'NetworkMode': 'host'},
        'Config': {'Entrypoint': ['python', '-m', 'monitoring.recording_runtime'], 'Cmd': args, 'Env': env}})
    probe.check(time.monotonic() + 30)
    assert calls == [(('http://127.0.0.1:18432',), {'vm': expected})]


@pytest.mark.skipif(not os.environ.get('NATIVE_RECORDING_TEST_VM'), reason='Disposable VM required')
def test_actual_vm_readback_uses_sample_time_and_rejects_old_process():
    url = os.environ['NATIVE_RECORDING_TEST_VM']
    assert url == 'http://127.0.0.1:18543'
    now = int(time.time() // 5) * 5 - 1000
    stamp = now - 10
    lines = []
    for row in records(stamp):
        tags = ','.join(k + '=' + json.dumps(v) for k, v in row['metric'].items())
        lines.append(f'monitoring_native_backend_expected_instances{{{tags}}} 1 {stamp * 1000}')
    with httpx.Client(base_url=url, trust_env=False) as client:
        client.post('/api/v1/import/prometheus', content='\n'.join(lines) + '\n').raise_for_status()
        client.get('/internal/force_flush').raise_for_status()
    assert release.fresh_records(url, now - 20, now)['oldest_sample_at'] == stamp
    with pytest.raises(RuntimeError, match='predate this process'):
        release.fresh_records(url, now - 5, now)
    with pytest.raises(RuntimeError, match='missing from VM'):
        release.fresh_records(url, now - 20, now + 30)
