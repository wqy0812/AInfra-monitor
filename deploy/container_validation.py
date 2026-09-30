"""Bounded, read-only acceptance for the independently owned containers."""
import json
import math
import re
import time
import urllib.parse
import urllib.request


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def option(args, name, default=None):
    for i, value in enumerate(args):
        if value.startswith(name + '='):
            return value.split('=', 1)[1]
        if value == name and i + 1 < len(args):
            return args[i + 1]
    return default


def base_url(address):
    parsed = urllib.parse.urlsplit('http://' + address)
    require(parsed.hostname and parsed.port, 'Missing component listener')
    host = parsed.hostname
    if host in ('0.0.0.0', '::'):
        host = '127.0.0.1' if host == '0.0.0.0' else '::1'
    return 'http://' + ('[' + host + ']' if ':' in host else host) + ':' + str(parsed.port)


def metrics(text):
    result = {}
    for line in text.splitlines():
        match = re.fullmatch(r'([a-zA-Z_:][a-zA-Z0-9_:]*)(\{.*\})?\s+(\S+)(?:\s+\S+)?', line)
        if not match:
            continue
        value = float(match[3])
        if not math.isfinite(value):
            continue
        labels = {k: json.loads(v) for k, v in re.findall(r'(\w+)=("(?:\\.|[^"\\])*")', match[2] or '')}
        result.setdefault(match[1], []).append((labels, value))
    return result


def scalar(rows, name):
    values = rows.get(name, [])
    require(len(values) == 1, 'Missing or ambiguous metric: ' + name)
    return values[0][1]


class ComponentProbe:
    def __init__(self, name, container):
        require(container['HostConfig']['NetworkMode'] == 'host', 'Only host-network monitoring containers are supported')
        self.name = name
        config = container['Config']
        args = (config.get('Entrypoint') or []) + (config.get('Cmd') or [])
        env = dict(x.split('=', 1) for x in config.get('Env', []) if '=' in x)
        if name == 'monitoring-api':
            host, port = option(args, '--host'), option(args, '--port')
            require(host and port, 'Missing API listener arguments')
            address = ('[' + host + ']' if ':' in host and not host.startswith('[') else host) + ':' + port
        elif name in ('monitoring-vm', 'monitoring-vmagent'):
            address = option(args, '-httpListenAddr')
        elif name == 'monitoring-node':
            address = option(args, '--web.listen-address')
        elif name == 'monitoring-dcu':
            host = env.get('BIND', '127.0.0.1')
            address = ('[' + host + ']' if ':' in host else host) + ':19500'
        else:
            raise RuntimeError('Unsupported monitoring component: ' + name)
        require(address, 'Missing component listener')
        self.base = base_url(address)
        self.last_scrapes = None
        self.activity_observed = False

    def check(self, deadline):
        def fetch(path, as_json=False):
            remaining = deadline - time.monotonic()
            require(remaining > 0, 'Component acceptance deadline exceeded')
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(self.base + path, timeout=min(4, remaining)) as response:
                data = response.read().decode()
            return json.loads(data) if as_json else data

        if self.name == 'monitoring-api':
            require(fetch('/health', True)['status'] == 'ok', 'API health degraded')
            for environment in ('dcu-pd', 'a3-vllm', 'xpu-pd'):
                data = fetch('/api/monitoring/latest?environment=' + environment, True)
                require(data['environment'] == environment and 0 <= time.time() - data['ts'] < 20, 'API data stale: ' + environment)
                require(set(data['nodes']) == {'prefill', 'decode'} and
                        all(n['metrics']['status'] == 'ok' for n in data['nodes'].values()), 'API sources incomplete: ' + environment)
        elif self.name == 'monitoring-vm':
            fetch('/health')
            data = fetch('/api/v1/query?' + urllib.parse.urlencode({'query': 'vector(1)', 'nocache': 1}), True)
            require(data['status'] == 'success' and len(data['data']['result']) == 1 and
                    float(data['data']['result'][0]['value'][1]) == 1, 'VM query failed')
        elif self.name == 'monitoring-vmagent':
            fetch('/health')
            rows = metrics(fetch('/metrics'))
            counters = [(labels, v) for labels, v in rows.get('vm_promscrape_scrapes_total', [])
                        if labels.get('status_code') == '200']
            require(counters and all(v >= 0 for _, v in counters), 'Missing successful vmagent scrape counters')
            total = sum(v for _, v in counters)
            previous = self.last_scrapes
            if previous is None or total > previous:
                self.last_scrapes = total
                self.last_activity = time.monotonic()
                if previous is not None:
                    self.activity_observed = True
            require(self.activity_observed and total >= previous and total > 0 and
                    time.monotonic() - self.last_activity < 15, 'vmagent scraping is not progressing')
        else:
            rows = metrics(fetch('/metrics'))
            if self.name == 'monitoring-node':
                require(rows.get('node_cpu_seconds_total') and all(v >= 0 for _, v in rows['node_cpu_seconds_total']), 'Missing CPU metrics')
                total = scalar(rows, 'node_memory_MemTotal_bytes')
                available = scalar(rows, 'node_memory_MemAvailable_bytes')
                require(total > 0 and 0 <= available <= total, 'Invalid memory metrics')
                require(0 < scalar(rows, 'node_boot_time_seconds') <= time.time(), 'Invalid boot time')
                require(0 <= time.time() - scalar(rows, 'node_time_seconds') < 15, 'Host observation stale')
            else:
                require(scalar(rows, 'dcu_sample_success') == 1 and
                        0 <= time.time() - scalar(rows, 'dcu_sample_timestamp_seconds') < 15, 'DCU observation unavailable or stale')
                expected = {'card' + str(i) for i in range(8)}
                for metric in ('utilization_percent', 'memory_used_bytes', 'memory_total_bytes', 'temperature_celsius', 'power_watts'):
                    samples = rows.get('dcu_' + metric, [])
                    require(len(samples) == 8 and {labels.get('device') for labels, _ in samples} == expected and
                            all(v >= 0 for _, v in samples), 'DCU devices incomplete: ' + metric)
                used = {labels['device']: v for labels, v in rows['dcu_memory_used_bytes']}
                require(all(total > 0 and used[labels['device']] <= total for labels, total in rows['dcu_memory_total_bytes']), 'Invalid DCU memory')
                require(all(v <= 100 for _, v in rows['dcu_utilization_percent']), 'Invalid DCU utilization')


def wait_ready(inspect, identity, image, probe, timeout=90):
    """Accept once the new component is ready; retries cover startup only."""
    deadline = time.monotonic() + timeout
    last_error = 'No successful observation'
    while time.monotonic() < deadline:
        try:
            current = inspect(identity, timeout=min(4, deadline - time.monotonic()))
            require(current['Id'] == identity and current['Image'] == image, 'Candidate identity changed')
            require(current['State']['Running'] and not current['State'].get('Restarting'), 'Candidate not running')
            health = current['State'].get('Health')
            require(not health or health['Status'] == 'healthy', 'Container health check not healthy')
            probe.check(deadline)
            if time.monotonic() < deadline:
                return
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            last_error = str(exc)
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(2, remaining))
    raise RuntimeError('Candidate acceptance timed out: ' + last_error)
