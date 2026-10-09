"""Verify trust and IP checks against a real local self-signed TLS server."""
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError

import pytest

import connection


def test_self_signed_ip_requires_explicit_trust_and_matching_san(tmp_path, monkeypatch):
    cert, key = tmp_path / 'server.crt', tmp_path / 'server.key'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                    '-days', '1', '-keyout', str(key), '-out', str(cert),
                    '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1'],
                   check=True, capture_output=True)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'healthy')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(connection, 'DEFAULT_CA', tmp_path / 'missing.crt')
    monkeypatch.delenv('PERSES_CA_FILE', raising=False)
    base = f'https://127.0.0.1:{server.server_port}'
    try:
        with pytest.raises(URLError) as untrusted:
            connection.opener(direct=True).open(base, timeout=3)
        assert isinstance(untrusted.value.reason, ssl.SSLCertVerificationError)
        monkeypatch.setenv('PERSES_CA_FILE', str(cert))
        with connection.opener(direct=True).open(base, timeout=3) as response:
            assert response.read() == b'healthy'
        with pytest.raises(URLError) as mismatch:
            connection.opener(direct=True).open(base.replace('127.0.0.1', 'localhost'), timeout=3)
        assert isinstance(mismatch.value.reason, ssl.SSLCertVerificationError)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
