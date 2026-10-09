"""Perses endpoint and verified TLS for maintenance clients."""
import os
import ssl
import urllib.request
from pathlib import Path

BASE = os.environ.get('PERSES_URL', 'https://122.247.53.162:18431').rstrip('/')
DEFAULT_CA = Path('/data2/monitoring/perses/tls/server.crt')


def tls_context():
    context = ssl.create_default_context()
    certificate = os.environ.get('PERSES_CA_FILE')
    if certificate or DEFAULT_CA.exists():
        context.load_verify_locations(cafile=certificate or str(DEFAULT_CA))
    return context


def opener(*, direct=False):
    handlers = [urllib.request.HTTPSHandler(context=tls_context())]
    if direct:
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def urlopen(request, *, timeout):
    return opener().open(request, timeout=timeout)
