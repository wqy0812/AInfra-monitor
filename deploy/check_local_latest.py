"""Read-only real-source replay in the candidate image; writes no VM data."""
import json
import time
import urllib.parse
import urllib.request

from monitoring import a3
from monitoring.api import encode
from monitoring.replay import decode_export, replay


def export(selector, start, end):
    url = 'http://127.0.0.1:18428/api/v1/export?' + urllib.parse.urlencode(
        {'match[]': selector, 'start': start, 'end': end})
    with urllib.request.urlopen(url, timeout=30) as r:
        return [json.loads(line) for line in r.read().decode().splitlines() if line]


end = int((time.time() - 10) // 5) * 5
start = end - 180
checks = {}
for environment in ('dcu-pd', 'a3-vllm'):
    selector = ('{environment="dcu-pd",job=~"sglang-prefill|sglang-decode"}' if environment == 'dcu-pd'
                else '{environment="a3-vllm",job="vllm-a3"}')
    raw = export(selector, start, end)
    decoder, calculate = (decode_export, replay) if environment == 'dcu-pd' else (a3.decode_export, a3.replay)
    _, points = calculate(decoder(raw), start, end)
    values = [p['nodes']['decode'].get('output_tokens') for p in points[-12:]]
    source = [r for r in raw if r['metric']['__name__'].endswith('generation_tokens_total')]
    scopes = sorted({r['metric'].get('is_streaming', '<absent>') for r in source})
    if environment == 'dcu-pd':
        assert any(v is not None for v in values), values
        assert 'schema="request-streaming-v1"' in encode(points[-1:])
    elif scopes == ['<absent>']:
        assert all(v is None for v in values), values
    checks[environment] = {'source_scopes': scopes, 'decode_output_tokens_last_12': values,
                           'last_timestamp': points[-1]['ts'], 'source_series': len(source)}
print(json.dumps({'passed': True, 'start': start, 'end': end, 'checks': checks}))
