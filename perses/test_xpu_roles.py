import json
import re
from pathlib import Path
import yaml
from align_dashboards import NODES
from xpu_cache import configure

ROOT = Path(__file__).parent


def test_role_targets_and_cache_whitelist_match_live_topology():
    assert NODES['xpu-monitoring'] == ('xpu-1', 'xpu-2')
    jobs = yaml.safe_load((ROOT.parent/'deploy/scrape.yml').read_text())['scrape_configs']
    seen = []
    for job in jobs:
        for group in job.get('static_configs', []):
            labels = group.get('labels', {})
            if labels.get('environment') != 'xpu-pd' or 'role' not in labels:
                continue
            role = labels['role']; ip = '122.209.21.33' if role == 'prefill' else '122.209.21.34'
            assert labels['node'] == ('xpu-1' if role == 'prefill' else 'xpu-2')
            assert all(t.startswith(ip+':') for t in group['targets'])
            seen.append((role, labels['service']))
        if job['job_name'] == 'sglang-prefill':
            pattern = job['metric_relabel_configs'][0]['regex']
            for name in ('load_back_tokens_total', 'evicted_tokens_total', 'load_back_duration_seconds_count', 'eviction_duration_seconds_sum'):
                assert re.fullmatch(pattern, 'xpu-pd;sglang:'+name)
    assert len(seen) == 6


def test_cache_has_real_prefill_queries_and_no_placeholders():
    d = json.loads((ROOT/'projects/xpu-monitoring/dashboards/cache-store.json').read_text())
    for candidate in (d, configure(d)):
        assert len(candidate['spec']['panels']) == 8
        for p in candidate['spec']['panels'].values():
            for q in p['spec']['queries']:
                expr = q['spec']['plugin']['spec']['query']
                assert 'instance="122.209.21.33:8501"' in expr
                assert 'job="sglang-prefill"' in expr
                assert 'vector(0)' not in expr
        assert '回载操作速率' in candidate['spec']['panels']['p7']['spec']['display']['name']
