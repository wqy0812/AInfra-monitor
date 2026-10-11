"""Offline failure paths: no device, syscall filter or upstream service is used."""
import errno
import io
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import httpx
import pytest

from monitoring import compat, exporter
from monitoring.query_client import QueryClient


@pytest.mark.parametrize('system,machine', [('Darwin', 'arm64'), ('Linux', 'aarch64')])
def test_compat_other_platforms_do_not_load_libc(monkeypatch, system, machine):
    monkeypatch.setattr(compat.platform, 'system', lambda: system)
    monkeypatch.setattr(compat.platform, 'machine', lambda: machine)
    libc = Mock(side_effect=AssertionError('must not install a filter'))
    monkeypatch.setattr(compat.ctypes, 'CDLL', libc)
    assert compat.apply() is False
    libc.assert_not_called()


@pytest.mark.parametrize('initial,initial_errno,prctl,final_errno,expected', [
    (0, 0, [0, 0], errno.ENOSYS, False),
    (-1, errno.ENOSYS, [0, 0], errno.ENOSYS, False),
    (-1, errno.EPERM, [1], errno.EPERM, OSError),
    (-1, errno.EPERM, [0, 1], errno.EPERM, OSError),
    (-1, errno.EPERM, [0, 0], errno.EPERM, RuntimeError),
    (-1, errno.EPERM, [0, 0], errno.ENOSYS, True),
])
def test_compat_filter_only_handles_eperm(monkeypatch, initial, initial_errno, prctl, final_errno, expected):
    monkeypatch.setattr(compat.platform, 'system', lambda: 'Linux')
    monkeypatch.setattr(compat.platform, 'machine', lambda: 'x86_64')
    errors = iter([initial_errno, final_errno])
    monkeypatch.setattr(compat.ctypes, 'get_errno', lambda: next(errors))
    libc = SimpleNamespace(syscall=Mock(return_value=initial), prctl=Mock(side_effect=prctl))
    monkeypatch.setattr(compat.ctypes, 'CDLL', lambda *a, **k: libc)
    if isinstance(expected, type):
        with pytest.raises(expected):
            compat.apply()
    else:
        assert compat.apply() is expected
    if expected is True:
        args = libc.prctl.call_args_list[1].args
        program = args[2]._obj
        assert program.len == 4
        assert program.filter[2].k == 0x00050000 | errno.ENOSYS
        assert program.filter[3].k == 0x7fff0000
    assert libc.syscall.call_args.args == (435, 0, 0)


@pytest.mark.parametrize('text,error', [
    ('warning only', StopIteration), ('device,HCU use (%)\n', ValueError),
    ('device,HCU use (%)\ncard0,1\ncard0,2\n', ValueError),
])
def test_dcu_csv_rejects_missing_or_duplicate_devices(text, error):
    with pytest.raises(error):
        exporter.parse_csv(text)


@pytest.mark.parametrize('age', [-1, 15, 100])
def test_exporter_never_publishes_future_or_stale_values(age):
    assert exporter.render({'ok': True, 'ts': 100, 'rows': [{'device': '0'}]}, 100 + age) == (
        'dcu_sample_success 0\ndcu_sample_timestamp_seconds 100\n')


def test_exporter_omits_invalid_values_and_escapes_labels():
    row = {'device': 'card"0', 'HCU use (%)': 'nan', 'vram Total Used Memory (MiB)': -1,
           'vram Total Memory (MiB)': None, 'Temperature (Sensor junction) (C)': 'bad',
           'Average Graphics Package Power (W)': '0'}
    result = exporter.render({'ok': True, 'ts': 100, 'rows': [row]}, 100)
    assert result.splitlines() == ['dcu_sample_success 1', 'dcu_sample_timestamp_seconds 100',
                                  'dcu_power_watts{device="card\\"0"} 0.0']


@pytest.mark.parametrize('path,allowed,upstream_error,status', [
    ('/metrics', False, False, 403), ('/missing', True, False, 404),
    ('/node-metrics', True, True, 503), ('/node-metrics', True, False, 200),
    ('/metrics', True, False, 200),
])
def test_exporter_http_access_and_proxy_failures(monkeypatch, path, allowed, upstream_error, status):
    handler = object.__new__(exporter.Handler)
    handler.path, handler.client_address, handler.wfile = path, ('127.0.0.1', 1), io.BytesIO()
    handler.send_error = Mock()
    handler.send_response, handler.send_header, handler.end_headers = Mock(), Mock(), Mock()
    monkeypatch.setattr(exporter, 'ALLOWED', {'127.0.0.1'} if allowed else set())
    monkeypatch.setattr(exporter, 'STATE', {'ok': False, 'ts': 0, 'rows': []})
    upstream = MagicMock()
    upstream.open.side_effect = OSError('offline') if upstream_error else None
    upstream.open.return_value.__enter__.return_value.read.return_value = b'node_up 1\n'
    monkeypatch.setattr(exporter.urllib.request, 'build_opener', lambda *a: upstream)
    handler.do_GET()
    if status != 200:
        handler.send_error.assert_called_once_with(status)
        assert handler.wfile.getvalue() == b''
    else:
        handler.send_response.assert_called_once_with(200)
        assert handler.wfile.getvalue()
        if path == '/node-metrics':
            assert handler.wfile.getvalue() == b'node_up 1\n'
    if not allowed or path != '/node-metrics':
        upstream.open.assert_not_called()


@pytest.mark.asyncio
async def test_query_client_restores_nested_scope_and_closes_on_error():
    async def respond(request):
        return httpx.Response(200, text=request.method)
    client = QueryClient(transport=httpx.MockTransport(respond))
    assert (await client.post('http://fixture', content=b'test')).text == 'POST'
    assert client.current.get() is None
    async with client.scope():
        outer = client.current.get()
        with pytest.raises(ValueError):
            async with client.scope():
                inner = client.current.get()
                assert inner is not outer
                raise ValueError('query failed')
        assert inner.is_closed and client.current.get() is outer
        assert (await client.get('http://fixture')).text == 'GET'
    assert outer.is_closed and client.current.get() is None
    await client.aclose()
    with pytest.raises(RuntimeError, match='closed'):
        await client.get('http://fixture')
    with pytest.raises(RuntimeError, match='closed'):
        async with client.scope():
            pytest.fail('closed facade must reject new pools')
