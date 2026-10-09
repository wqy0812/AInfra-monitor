"""Verified TLS/HTTP2 transport for the read-only query diagnostic benchmark.

Importing this module does not require pycurl. Each pool owns a persistent
CurlMulti connection cache and must be used by only one caller at a time.
Different threads must use different pools. No request or response contents are
included in transfer metadata or errors.
"""
import importlib
import io
import json
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit


class H2TransportError(RuntimeError):
    """An incomplete or unverified measurement; never retried automatically."""


class H2Pool:
    """One TLS connection, at most three in-flight HTTP/2 requests.

    ``run([(path, body), ...], token)`` accepts root-relative paths and either
    form-encoded bytes for POST or None for GET. It returns decoded ``results``
    and ``transfers`` in input order, plus complete batch wall-clock ``elapsed``.
    A warm-up GET on this same pool allows subsequent runs to reuse its TLS
    connection. The server can close that connection; NUM_CONNECTS records this.
    """

    def __init__(self, base, ca_file, concurrency=3):
        parsed = urlsplit(base)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
                or parsed.password or parsed.path not in ('', '/')
                or parsed.query or parsed.fragment or '\\' in base
                or any(c.isspace() for c in base)):
            raise ValueError('base must be an HTTPS origin without credentials')
        # Force malformed ports to fail before initializing any native handles.
        parsed.port
        if isinstance(concurrency, bool) or not isinstance(concurrency, int) or not 1 <= concurrency <= 3:
            raise ValueError('concurrency must be an integer from 1 through 3')
        if not Path(ca_file).is_file():
            raise ValueError('An explicit, existing TLS CA file is required')
        self.base = base.rstrip('/')
        self.ca_file = str(Path(ca_file).resolve())
        self.concurrency = concurrency
        self._lock = threading.Lock()
        self._closed = False
        self._curl = importlib.import_module('pycurl')
        self.metadata = {'client_version': self._curl.version,
                         'compression': 'identity', 'max_connections': 1,
                         'concurrency': concurrency, 'tls_peer_verify': True,
                         'tls_hostname_verify': True, 'proxy': False}
        self._multi = self._curl.CurlMulti()
        try:
            self._multi.setopt(self._curl.M_PIPELINING, self._curl.PIPE_MULTIPLEX)
            self._multi.setopt(self._curl.M_MAX_HOST_CONNECTIONS, 1)
            self._multi.setopt(self._curl.M_MAX_TOTAL_CONNECTIONS, 1)
            self._multi.setopt(self._curl.M_MAXCONNECTS, 1)
        except Exception:
            self._multi.close()
            self._closed = True
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if not self._lock.acquire(False):
            raise H2TransportError('Cannot close a pool during a measurement')
        try:
            if not self._closed:
                self._multi.close()
                self._closed = True
        finally:
            self._lock.release()

    def _validate_requests(self, requests, token):
        if not isinstance(token, str) or not token or '\r' in token or '\n' in token:
            raise ValueError('A nonempty single-line bearer token is required')
        values = list(requests)
        for item in values:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise ValueError('Each request must be a (path, body) pair')
            path, body = item
            if not isinstance(path, str):
                raise ValueError('Request paths must be strings')
            parsed = urlsplit(path)
            if (not path.startswith('/') or path.startswith('//') or parsed.scheme
                    or parsed.netloc or parsed.fragment or '\\' in path
                    or any(c.isspace() for c in path)):
                raise ValueError('Request paths must stay within the HTTPS origin')
            if body is not None and not isinstance(body, bytes):
                raise ValueError('POST bodies must be form-encoded bytes')
        return values

    def _handle(self, path, body, token, buffer):
        curl, handle = self._curl, self._curl.Curl()
        try:
            options = {
                curl.URL: self.base + path,
                curl.HTTP_VERSION: curl.CURL_HTTP_VERSION_2_0,
                curl.PIPEWAIT: 1,
                curl.SSL_VERIFYPEER: 1,
                curl.SSL_VERIFYHOST: 2,
                curl.CAINFO: self.ca_file,
                curl.PROXY: '',
                curl.FOLLOWLOCATION: 0,
                curl.NOSIGNAL: 1,
                curl.CONNECTTIMEOUT: 20,
                curl.TIMEOUT: 180,
                curl.WRITEFUNCTION: buffer.write,
                curl.HTTPHEADER: ['Authorization: Bearer ' + token,
                                  'Accept: application/json', 'Accept-Encoding: identity'],
            }
            if body is None:
                options[curl.HTTPGET] = 1
            else:
                options[curl.POST] = 1
                options[curl.POSTFIELDS] = body
                options[curl.HTTPHEADER] += ['Content-Type: application/x-www-form-urlencoded', 'Expect:']
            for key, value in options.items():
                handle.setopt(key, value)
            return handle
        except Exception:
            handle.close()
            raise

    def _finished(self, handle, index, body, buffer):
        curl = self._curl
        metadata = {'index': index, 'method': 'GET' if body is None else 'POST'}
        fields = (
            ('http_version', curl.INFO_HTTP_VERSION),
            ('ssl_verify_result', curl.SSL_VERIFYRESULT),
            ('new_connections', curl.NUM_CONNECTS),
            ('local_port', curl.LOCAL_PORT),
            ('total_time', curl.TOTAL_TIME),
            ('response_code', curl.RESPONSE_CODE),
            ('size_download', curl.SIZE_DOWNLOAD),
            ('size_upload', curl.SIZE_UPLOAD),
            ('header_size', curl.HEADER_SIZE),
            ('request_size', curl.REQUEST_SIZE),
        )
        for key, info in fields:
            metadata[key] = handle.getinfo(info)
        if metadata['http_version'] != curl.CURL_HTTP_VERSION_2_0:
            raise H2TransportError('Request {} did not negotiate HTTP/2'.format(index))
        if metadata['ssl_verify_result'] != 0:
            raise H2TransportError('Request {} failed TLS verification'.format(index))
        if metadata['response_code'] != 200:
            raise H2TransportError('Request {} returned HTTP {}'.format(index, metadata['response_code']))
        try:
            result = json.loads(buffer.getvalue())
        except (ValueError, UnicodeError):
            raise H2TransportError('Request {} returned invalid JSON'.format(index)) from None
        if isinstance(result, dict) and (result.get('warnings') or result.get('isPartial')):
            raise H2TransportError('Request {} returned warnings or partial results'.format(index))
        if body is not None and (not isinstance(result, dict) or result.get('status') != 'success'):
            raise H2TransportError('Request {} returned unsuccessful query status'.format(index))
        return result, metadata

    def run(self, requests, token):
        values = self._validate_requests(requests, token)
        if not self._lock.acquire(False):
            raise H2TransportError('Use a separate pool for each concurrent caller')
        active = {}
        try:
            if self._closed:
                raise H2TransportError('Pool is closed')
            started = time.monotonic()
            results, transfers = [None] * len(values), [None] * len(values)
            following = 0
            while following < len(values) or active:
                while following < len(values) and len(active) < self.concurrency:
                    path, body = values[following]
                    buffer = io.BytesIO()
                    try:
                        handle = self._handle(path, body, token, buffer)
                    except Exception:
                        buffer.close()
                        raise
                    try:
                        self._multi.add_handle(handle)
                    except Exception:
                        handle.close()
                        buffer.close()
                        raise
                    active[handle] = (following, body, buffer)
                    following += 1
                while True:
                    status, running = self._multi.perform()
                    if status != self._curl.E_CALL_MULTI_PERFORM:
                        break
                if status != self._curl.E_OK:
                    raise H2TransportError('HTTP/2 multi transport failed (code {})'.format(status))
                while True:
                    remaining, completed, failed = self._multi.info_read()
                    if failed:
                        handle, error_code, ignored_message = failed[0]
                        raise H2TransportError('Request {} transport failed (code {})'.format(active[handle][0], error_code))
                    for handle in completed:
                        index, body, buffer = active[handle]
                        results[index], transfers[index] = self._finished(handle, index, body, buffer)
                        self._multi.remove_handle(handle)
                        del active[handle]
                        handle.close()
                        buffer.close()
                    if not remaining:
                        break
                if active and (len(active) == self.concurrency or following == len(values)):
                    # select(-1) means no selectable descriptors; avoid a spin
                    # while curl waits on timers or connection establishment.
                    if self._multi.select(0.1) == -1:
                        time.sleep(0.001)
            elapsed = time.monotonic() - started
            return {'results': results, 'elapsed': elapsed, 'transfers': transfers}
        except self._curl.error:
            # libcurl error messages can include URLs. Suppress raw errors.
            raise H2TransportError('HTTP/2 transport setup or inspection failed') from None
        finally:
            cleanup_failed = False
            try:
                for handle, (index, body, buffer) in list(active.items()):
                    try:
                        self._multi.remove_handle(handle)
                    except Exception:
                        cleanup_failed = True
                    finally:
                        try:
                            handle.close()
                        except Exception:
                            cleanup_failed = True
                        finally:
                            buffer.close()
            finally:
                self._lock.release()
            if cleanup_failed:
                raise H2TransportError('HTTP/2 transport resource cleanup failed') from None
