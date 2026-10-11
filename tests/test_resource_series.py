import json
from pathlib import Path
import pytest
from monitoring.monitor_series import chart_point
from monitoring.resource_series import key, resource_values
from monitoring.calculator import parse_prom


def snapshot(ts, failed=False):
    data = {'host': {'cpu_percent': 8, 'memory': {'MemTotal': 128*2**30, 'MemAvailable': 32*2**30}, 'load': [1, 2, 3], 'disks': {'data2': {'available': 48*2**30, 'total': 100*2**30}}, 'network_rates': {'eth0.1': {'rx': 2**20, 'tx': None}}, 'disk_rates': {'nvme0n1': {'read': 3*2**20, 'write': 0}}}, 'gpus': [{'device': 'card0', 'HCU use (%)': '0', 'vram Total Used Memory (MiB)': 8192, 'vram Total Memory (MiB)': 16384, 'Temperature (Sensor junction) (C)': 45, 'Average Graphics Package Power (W)': 100}]}
    return {'ts': ts, 'nodes': {role: {'telemetry': {'status': 'error' if failed else 'ok', 'observed_at': ts, 'data': data}, 'metrics': {'status': 'ok', 'observed_at': ts, 'data': {'requests': 10, 'rates': {'requests': 0, 'decode_tokens': 0}}}} for role in ('prefill', 'decode')}}


def test_fresh_projection_units_zero_missing_and_encoded_names():
    point = chart_point(snapshot(100))
    resources = point['nodes']['prefill']['resources']
    assert 'memory' not in resources
    assert resources['gpu_utilization']['card0'] == 0
    assert resources['gpu_memory_ratio']['card0'] == 50
    assert 'network' not in resources
    assert '.' not in key('eth0.1 · 接收')
    stale = snapshot(125)
    stale['nodes']['prefill']['telemetry']['observed_at'] = 100
    assert 'gpu_utilization' not in chart_point(stale)['nodes']['prefill']['resources']
    assert 'memory' not in chart_point(stale)['nodes']['prefill']['resources']




if __name__ == '__main__':
    import time
    now = int(time.time())
    points = [chart_point(snapshot(now-30+i*5, failed=i == 3)) for i in range(7)]
    Path('../code-eval/evidence/monitor-curves-20260914/fixture.json').write_text(json.dumps({'snapshot': snapshot(now), 'history': {'points': points, 'stride': 1}}))
