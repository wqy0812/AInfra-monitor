"""Keep current operator guidance reproducible without deployment evidence."""
import copy
import json
from pathlib import Path
import runpy
import shutil
import sys

import pytest

import project_coverage
import project_split
from dashboard_columns import apply as columns
from metric_scope import request_description

ROOT = Path(__file__).resolve().parent


def test_docs_only_is_complete_stable_and_preserves_historical_inventory(tmp_path, monkeypatch):
    shutil.copytree(ROOT / 'projects', tmp_path / 'projects')
    catalog = (ROOT / 'metric_coverage.json').read_bytes()
    (tmp_path / 'metric_coverage.json').write_bytes(catalog)
    monkeypatch.setattr(project_coverage, 'ROOT', tmp_path)
    monkeypatch.setattr(sys, 'argv', ['project_coverage.py', '--docs-only'])
    project_coverage.main()
    files = ['panel_descriptions.json', 'METRICS_GUIDE.md', 'METRIC_COVERAGE.md']
    first = {name: (tmp_path / name).read_bytes() for name in files}
    with pytest.raises(SystemExit) as completed:
        runpy.run_path(str(ROOT / 'annotate_panels.py'), run_name='__main__')
    assert completed.value.code == 0
    assert first == {name: (tmp_path / name).read_bytes() for name in files}
    assert (tmp_path / 'metric_coverage.json').read_bytes() == catalog
    assert all((ROOT / name).read_bytes() == content for name, content in first.items())
    descriptions = json.loads(first['panel_descriptions.json'])
    count = 0
    for d in columns(project_split.read_resources(ROOT / 'projects'))['dashboards']:
        for key, panel in d['spec']['panels'].items():
            assert descriptions[d['metadata']['project']][d['metadata']['name']][key] == panel['spec']['display']['description']
            count += 1
    assert count == 249  # 2026-10-09: trimmed to 204 detail panels plus three 15-panel summaries.
    assert '不代表当前在线目标' in first['METRIC_COVERAGE.md'].decode()
    assert '当前沿用线上 v1/latency-v2' not in first['METRIC_COVERAGE.md'].decode()


def test_current_gateway_descriptions_match_populations_and_keep_all_panels():
    documents = project_split.read_resources(ROOT / 'projects')['dashboards']
    for d in documents:
        if not d['metadata']['name'].startswith('gateway'):continue
        for key, panel in d['spec']['panels'].items():
            description = panel['spec']['display']['description']
            assert not any(text in description for text in ('当前流式请求中年龄最大的请求', '当前流式请求按处理阶段',
                '非流式仅在此图统计', '尚未结束的流式请求数量', '解析确认 stream=true、进入请求画像', '读取流式请求的错误响应'))
            if key == 'live-nonstream-count':assert '子集' in description and '也计入' in description
            if key == 'live-waiting':assert '流式' in description
        if d['metadata']['name'] == 'gateway-generation':assert len(d['spec']['panels']) == 6


def test_description_migration_is_idempotent_and_does_not_change_stream_only_meanings():
    old = '当前流式请求中年龄最大的请求。非流式仅在此图统计。'
    new = request_description(old)
    assert request_description(new) == new
    original = '当前已确认为流式、但尚未收到首个有效内容增量的请求数。'
    assert request_description(original) == original
    assert request_description('非流式请求速率') == '非流式请求速率'


def test_description_normalization_preserves_custom_panels_and_numeric_queries():
    resources = project_split.read_resources(ROOT / 'projects')
    d = copy.deepcopy(next(d for d in resources['dashboards'] if d['metadata']['name'] == 'gateway-generation'))
    d['spec']['panels']['live-oldest']['spec']['display']['description'] = '自定义注释：当前流式请求中年龄最大的请求。'
    after = project_split.normalize_dashboard(d, d['metadata']['project'])
    assert '自定义注释：当前全部在途请求' in after['spec']['panels']['live-oldest']['spec']['display']['description']
    for key, panel in d['spec']['panels'].items():
        assert after['spec']['panels'][key]['spec']['queries'] == panel['spec']['queries']
        assert after['spec']['panels'][key]['spec']['plugin'] == panel['spec']['plugin']
    assert after['spec']['layouts'] == d['spec']['layouts']
