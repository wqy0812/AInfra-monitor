import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'deploy/perses_acceleration'))
import h2_transport as transport


@pytest.fixture
def fake_curl(tmp_path, monkeypatch):
    names = '''M_PIPELINING PIPE_MULTIPLEX M_MAX_HOST_CONNECTIONS M_MAX_TOTAL_CONNECTIONS
    M_MAXCONNECTS URL HTTP_VERSION CURL_HTTP_VERSION_2_0 PIPEWAIT SSL_VERIFYPEER SSL_VERIFYHOST
    CAINFO PROXY FOLLOWLOCATION NOSIGNAL CONNECTTIMEOUT TIMEOUT WRITEFUNCTION HTTPHEADER HTTPGET
    POST POSTFIELDS INFO_HTTP_VERSION SSL_VERIFYRESULT NUM_CONNECTS LOCAL_PORT TOTAL_TIME RESPONSE_CODE
    SIZE_DOWNLOAD SIZE_UPLOAD HEADER_SIZE REQUEST_SIZE E_CALL_MULTI_PERFORM E_OK'''.split()
    module = types.SimpleNamespace(**{name: number for number, name in enumerate(names, 100)})
    module.version = 'test-pycurl/libcurl'
    module.error = type('PycurlError', (Exception,), {})
    module.handles = []
    module.overrides = {}
    module.payload = {'status': 'success', 'data': {'result': []}}
    module.failure = None
    class Handle:
        def __init__(self):
            self.options = {}; self.closed = False; self.info = {}; self.completed = False
            module.handles.append(self)
        def setopt(self, option, value): self.options[option] = value
        def getinfo(self, key): return self.info[key]
        def close(self): self.closed = True
    class Multi:
        def __init__(self):
            self.options = {}; self.handles = []; self.closed = False
            self.peak = 0; self.connected = False
        def setopt(self, option, value): self.options[option] = value
        def add_handle(self, handle):
            self.handles.append(handle); self.peak = max(self.peak, len(self.handles))
        def remove_handle(self, handle): self.handles.remove(handle)
        def perform(self):
            for handle in self.handles:
                if handle.completed: continue
                payload = json.dumps(module.payload).encode()
                handle.options[module.WRITEFUNCTION](payload)
                handle.info = {
                    module.INFO_HTTP_VERSION: module.CURL_HTTP_VERSION_2_0,
                    module.SSL_VERIFYRESULT: 0, module.NUM_CONNECTS: int(not self.connected),
                    module.LOCAL_PORT: 40000, module.TOTAL_TIME: .01, module.RESPONSE_CODE: 200,
                    module.SIZE_DOWNLOAD: len(payload), module.SIZE_UPLOAD: len(handle.options.get(module.POSTFIELDS, b'')),
                    module.HEADER_SIZE: 32, module.REQUEST_SIZE: 50,
                }
                handle.info.update(module.overrides)
                handle.completed = True; self.connected = True
            return module.E_OK, 0
        def info_read(self):
            if module.failure:
                return 0, [], [(self.handles[0], 42, module.failure)]
            return 0, list(reversed(self.handles)), []
        def select(self, timeout): return 0
        def close(self): self.closed = True
    module.Curl = Handle
    module.CurlMulti = Multi
    original = transport.importlib.import_module
    monkeypatch.setattr(transport.importlib, 'import_module', lambda name: module if name == 'pycurl' else original(name))
    ca = tmp_path / 'ca.pem'; ca.write_text('test fixture')
    module.ca = str(ca)
    return module


def test_bounded_multiplexing_tls_validation_and_cross_run_reuse(fake_curl):
    curl = fake_curl
    with transport.H2Pool('https://example.test:8443', curl.ca) as pool:
        warm = pool.run([('/api/v1/projects', None)], 'secret')
        run = pool.run([('/query', b'query=secret_expression') for _ in range(8)], 'secret')
        assert pool._multi.peak == 3
        assert pool._multi.options[curl.M_MAX_HOST_CONNECTIONS] == 1
        assert pool._multi.options[curl.M_MAX_TOTAL_CONNECTIONS] == 1
        assert pool._multi.options[curl.M_PIPELINING] == curl.PIPE_MULTIPLEX
        assert sum(row['new_connections'] for row in warm['transfers']) == 1
        assert sum(row['new_connections'] for row in run['transfers']) == 0
        assert [row['index'] for row in run['transfers']] == list(range(8))
        assert all(row['local_port'] == 40000 for row in run['transfers'])
        assert run['elapsed'] > 0 and len(run['results']) == 8
        assert pool.metadata['client_version'] == curl.version
        assert pool.metadata['compression'] == 'identity'
        assert 'secret' not in json.dumps(run['transfers'])
        for handle in curl.handles:
            assert handle.options[curl.SSL_VERIFYPEER] == 1
            assert handle.options[curl.SSL_VERIFYHOST] == 2
            assert handle.options[curl.CAINFO] == curl.ca
            assert handle.options[curl.PROXY] == ''
            assert handle.options[curl.PIPEWAIT] == 1
            assert handle.options[curl.FOLLOWLOCATION] == 0
            assert handle.closed
        assert not pool._multi.handles
    assert pool._multi.closed
    pool.close()
    with pytest.raises(transport.H2TransportError, match='closed'):
        pool.run([], 'secret')


@pytest.mark.parametrize('key,value,reason', [
    ('INFO_HTTP_VERSION', 999, 'HTTP/2'), ('SSL_VERIFYRESULT', 1, 'TLS'),
    ('RESPONSE_CODE', 503, 'HTTP 503'),
])
def test_failed_transport_measurements_are_rejected_and_cleaned(fake_curl, key, value, reason):
    fake_curl.overrides[getattr(fake_curl, key)] = value
    with transport.H2Pool('https://example.test', fake_curl.ca) as pool:
        with pytest.raises(transport.H2TransportError, match=reason):
            pool.run([('/query', b'query=hidden') for _ in range(8)], 'secret')
        assert len(fake_curl.handles) == 3  # no retry and no further queued work
        assert not pool._multi.handles
        assert all(handle.closed for handle in fake_curl.handles)


@pytest.mark.parametrize('payload', [
    {'status': 'error'}, {'status': 'success', 'warnings': ['bad']},
    {'status': 'success', 'isPartial': True}, [],
])
def test_failed_query_status_or_incomplete_results_rejected(fake_curl, payload):
    fake_curl.payload = payload
    with transport.H2Pool('https://example.test', fake_curl.ca) as pool:
        with pytest.raises(transport.H2TransportError):
            pool.run([('/query', b'query=hidden')], 'secret')


def test_raw_libcurl_failure_text_is_not_exposed(fake_curl):
    fake_curl.failure = 'secret url and token'
    with transport.H2Pool('https://example.test', fake_curl.ca) as pool:
        with pytest.raises(transport.H2TransportError) as error:
            pool.run([('/query', b'query=hidden')], 'secret')
        assert 'secret' not in str(error.value)
        assert '42' in str(error.value)
        assert all(handle.closed for handle in fake_curl.handles)


@pytest.mark.parametrize('path', ['https://evil.test/query', '//evil.test/query', '/\\evil.test', '/query#fragment', '/query\n'])
def test_paths_cannot_escape_verified_origin(fake_curl, path):
    with transport.H2Pool('https://example.test', fake_curl.ca) as pool:
        with pytest.raises(ValueError): pool.run([(path, None)], 'secret')
        assert not fake_curl.handles


@pytest.mark.parametrize('base', ['http://example.test', 'https://user:pass@example.test', 'https://example.test/path'])
def test_base_requires_bare_tls_origin(fake_curl, base):
    with pytest.raises(ValueError): transport.H2Pool(base, fake_curl.ca)


def test_pool_rejects_overlapping_use(fake_curl):
    with transport.H2Pool('https://example.test', fake_curl.ca) as pool:
        pool._lock.acquire()
        try:
            with pytest.raises(transport.H2TransportError, match='separate pool'):
                pool.run([('/query', None)], 'secret')
            with pytest.raises(transport.H2TransportError, match='during'):
                pool.close()
        finally:
            pool._lock.release()
