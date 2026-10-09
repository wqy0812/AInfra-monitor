import json
import re
from pathlib import Path
import yaml
from align_dashboards import NODES
from xpu_cache import configure, RETIRED
from xpu_topology import configure as configure_topology, scrape

ROOT = Path(__file__).parent


def test_topology_repair_is_idempotent_and_preserves_other_scopes():
    config = (ROOT.parent/'deploy/scrape.yml').read_text()
    assert scrape(config) == config
    for project in ('xpu-monitoring', 'dcu-monitoring', 'a3-monitoring'):
        for path in (ROOT/'projects'/project/'dashboards').glob('*.json'):
            source = json.loads(path.read_text())
            assert configure_topology(source) == source
    source = json.loads((ROOT/'projects/xpu-monitoring/dashboards/hosts-xpu.json').read_text())
    stale = json.loads(json.dumps(source).replace('xpu-1', 'SWAP').replace('xpu-2', 'xpu-1').replace('SWAP', 'xpu-2'))
    assert configure_topology(stale) == source


def test_role_targets_and_cache_whitelist_match_live_topology():
    assert NODES['xpu-monitoring'] == ('xpu-2', 'xpu-1')
    jobs = yaml.safe_load((ROOT.parent/'deploy/scrape.yml').read_text())['scrape_configs']
    seen = []
    for job in jobs:
        for group in job.get('static_configs', []):
            labels = group.get('labels', {})
            if labels.get('environment') != 'xpu-pd' or 'role' not in labels:
                continue
            role = labels['role']; ip = '122.209.21.34' if role == 'prefill' else '122.209.21.33'
            assert labels['node'] == ('xpu-2' if role == 'prefill' else 'xpu-1')
            assert all(t.startswith(ip+':') for t in group['targets'])
            seen.append((role, labels['service']))
        if job['job_name'] == 'sglang-prefill':
            pattern = job['metric_relabel_configs'][0]['regex']
            for name in ('cache_hit_rate', 'realtime_tokens_total', 'num_used_tokens', 'max_total_num_tokens',
                         'hicache_scheduler_idle_with_pending_total', 'hicache_scheduler_idle_with_pending_seconds_total'):
                assert re.fullmatch(pattern, 'xpu-pd;sglang:'+name)
    assert len(seen) == 6


def test_cache_has_real_prefill_queries_and_no_placeholders():
    d = json.loads((ROOT/'projects/xpu-monitoring/dashboards/cache-store.json').read_text())
    for candidate in (d, configure(d)):
        assert len(candidate['spec']['panels']) == 4
        for p in candidate['spec']['panels'].values():
            for q in p['spec']['queries']:
                expr = q['spec']['plugin']['spec']['query']
                assert 'instance="122.209.21.34:8501"' in expr
                assert 'job="sglang-prefill"' in expr
                assert 'vector(0)' not in expr
                assert not any(name in expr for name in RETIRED)
        for key in ('prefetch-idle-ticks', 'prefetch-idle-time'):
            expr = candidate['spec']['panels'][key]['spec']['queries'][0]['spec']['plugin']['spec']['query']
            assert 'sum(' not in expr and 'sum by' not in expr
            assert 'resets(' in expr and 'count_over_time(' in expr
        expr = candidate['spec']['panels']['cache-hit-window']['spec']['queries'][0]['spec']['plugin']['spec']['query']
        assert 'prefill_compute' in expr and 'prefill_cache' in expr and '> 0' in expr
    assert configure(d) == d
