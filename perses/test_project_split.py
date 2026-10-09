import copy
import json
import unittest
import tempfile
from unittest.mock import patch
from pathlib import Path

import project_split as split


class ProjectSplitTest(unittest.TestCase):
    def test_request_schema_migration_preserves_other_selectors(self):
        query = ('monitoring_chart_value{schema="latency-v2",path=~"nodes.$role.percentiles.ttft.p95"}'
                 ' + monitoring_chart_value{schema="v1",path=~"nodes.$role.cpu"}')
        changed = split.current_request_schema(query)
        self.assertIn('schema="request-metrics-v2",path=~"nodes.$role.percentiles.ttft.p95"', changed)
        self.assertIn('schema="v1",path=~"nodes.$role.cpu"', changed)
        self.assertEqual(split.current_request_schema(changed), changed)
        self.assertIn('schema="request-metrics-v2"', split.derived('a3-vllm', 'nodes.$role.resources.queue..*'))

    def setUp(self):
        split.BOUNDS.update(json.loads((split.ROOT / "histogram_bounds.json").read_text()))
        self.resources = split.read_resources(split.ROOT / "projects")

    def test_resource_structure_and_no_request_filters(self):
        split.validate(self.resources)
        self.assertEqual(len(self.resources["dashboards"]), 30)
        self.assertEqual(split.no_request_filter(
            'm{request_scope="streaming",a="b"} + m{a="b",is_streaming!="false"} + m{stream="true"}'),
            'm{a="b"} + m{a="b"} + m{}')

    def test_generation_preserves_scopes_and_colors(self):
        for d in self.resources["dashboards"]:
            if d["metadata"]["name"] == "gateway-generation":
                env = split.PROJECTS[d["metadata"]["project"]]
                self.assertIn("live-stages", d["spec"]["panels"])
                self.assertEqual(len(d["spec"]["panels"]), 6)
                d = next(x for x in self.resources["dashboards"] if x["metadata"]["project"] == d["metadata"]["project"] and x["metadata"]["name"] == "gateway-requests")
                self.assertEqual(len(d["spec"]["panels"]["generation-0"]["spec"]["queries"]), 4)
                setting = d["spec"]["panels"]["generation-0"]["spec"]["plugin"]["spec"]["querySettings"]
                self.assertEqual(setting[0]["queryIndex"], 0)

    def test_regeneration_is_stable(self):
        original = copy.deepcopy(self.resources)
        generated = split.build(self.resources)
        by_key = lambda r: {(d["metadata"].get("project", ""), d["metadata"]["name"]): d for group in r.values() for d in group}
        self.assertEqual(by_key(generated), by_key(self.resources))
        self.assertEqual(original, self.resources)

    def test_histogram_has_bucket_and_group_validation(self):
        from project_queries import Queries
        expression = Queries("a3-vllm", "vllm-a3").histogram("latency", ["1", "2", "+Inf"], .95, "environment,node")
        for required in ("count without(le)", "ignoring(le)", "resets(", "unless on(environment,node)", "sum by(le,environment,node)"):
            self.assertIn(required, expression)

    def test_validation_rejects_wrong_environment_even_for_xpu(self):
        resources = copy.deepcopy(self.resources)
        dashboard = next(d for d in resources['dashboards'] if
                         d['metadata']['project'] == 'xpu-monitoring' and d['metadata']['name'] == 'backend-performance')
        query = next(iter(dashboard['spec']['panels'].values()))['spec']['queries'][0]['spec']['plugin']['spec']
        query['query'] = query['query'].replace('environment="xpu-pd"', 'environment="dcu-pd"')
        with self.assertRaises(AssertionError):
            split.validate(resources)
        query['query'] = 'vector(0) unless on() vector(0)'
        with self.assertRaises(AssertionError):
            split.validate(resources)

    def test_trimmed_panels_stay_retired(self):
        from panel_trim import RETIRED, RETIRED_DASHBOARDS, PROFILE_DROPS, retire
        dashboards = {(d['metadata']['project'], d['metadata']['name']): d for d in self.resources['dashboards']}
        self.assertFalse({name for _, name in dashboards} & set(RETIRED_DASHBOARDS))
        for (project, name), retired in RETIRED.items():
            d = dashboards[(project, name)]
            self.assertFalse(set(retired) & set(d['spec']['panels']), (project, name))
            for keeper in filter(None, retired.values()):
                self.assertIn(keeper, d['spec']['panels'])
        for (project, name), d in dashboards.items():
            self.assertFalse([k for k in d['spec']['panels'] if k.endswith('-samples')], (project, name))
            if name == 'gateway-requests':
                merged = d['spec']['panels']['generation-0']['spec']['queries']
                self.assertEqual([q['spec']['plugin']['spec']['seriesNameFormat'] for q in merged],
                                 ['全部结束', '客户端取消', '客户端断开', '未知结果'])
                self.assertTrue(all('aigate_generation_requests_ended_total' in q['spec']['plugin']['spec']['query'] and
                                    not q['spec']['plugin']['spec']['query'].startswith('100 *') for q in merged))
            if name == 'accelerator-resources':
                used = d['spec']['panels']['core-memory-used']['spec']['queries']
                self.assertEqual(used[-1]['spec']['plugin']['spec']['seriesNameFormat'], '单卡总量')
                self.assertTrue(used[-1]['spec']['plugin']['spec']['query'].startswith('max('))
            if name == 'monitoring-health':
                self.assertEqual(len(d['spec']['panels']), 7)
                self.assertIn('aigate_profile_dropped_events_total', d['spec']['panels'][PROFILE_DROPS]['spec']['queries'][0]['spec']['plugin']['spec']['query'])
        hosts = dashboards[('dcu-monitoring', 'hosts-dcu')]
        order = [x['content']['$ref'].rsplit('/', 1)[1] for x in hosts['spec']['layouts'][0]['spec']['items']]
        self.assertEqual(order[:3], ['core-p0', 'core-p1', 'core-extra-fs-free'])
        decode = dashboards[('dcu-monitoring', 'backend-decode')]
        core = [x['content']['$ref'].rsplit('/', 1)[1] for x in decode['spec']['layouts'][0]['spec']['items']]
        self.assertEqual(core, ['core-queue', 'bn-decode-kv-capacity'])
        # Restoring a retired panel is removed again with the value panel back in its slot.
        restored = copy.deepcopy(hosts)
        restored['spec']['panels']['core-p2'] = copy.deepcopy(restored['spec']['panels']['core-p1'])
        restored['spec']['layouts'][0]['spec']['items'] = split.grid(order[:2] + ['core-p2'] + order[2:])
        self.assertEqual(retire(restored), hosts)
        for d in self.resources['dashboards']:
            self.assertIs(retire(d), d)

    def test_drilldown_collapses_details_and_draws_fewer_lines(self):
        from drilldown_layout import COLLAPSED, FEWER_DEVICES, present
        for d in self.resources['dashboards']:
            name = d['metadata']['name']
            self.assertEqual(present(d), d)
            for layout in d['spec']['layouts']:
                display = layout['spec'].get('display', {})
                self.assertEqual(display.get('collapse') == {'open': False}, display.get('title') in COLLAPSED.get(name, ()), (name, display))
            if name in ('hosts-dcu', 'a3-hosts', 'hosts-xpu'):
                load = d['spec']['panels']['core-extra-load']['spec']['queries']
                self.assertEqual([q['spec']['plugin']['spec']['seriesNameFormat'] for q in load], ['{{node}} · 5m'])
                for key in ('core-p3', 'core-p4'):
                    for q in d['spec']['panels'][key]['spec']['queries']:
                        self.assertIn('device!~"' + FEWER_DEVICES + '"', q['spec']['plugin']['spec']['query'])
            if name == 'a3-cache':
                for key in ('extra-prefix_cache_', 'extra-external_prefix_cache_'):
                    for q in d['spec']['panels'][key]['spec']['queries']:
                        self.assertIn('sum by(environment,node)', q['spec']['plugin']['spec']['query'])

    def test_a3_prefix_expected_endpoints_match_api_collection_scope(self):
        from drilldown_layout import A3_ENGINE_TARGETS
        from monitoring.a3 import NODES, INSTANCE_COUNTS
        self.assertEqual(A3_ENGINE_TARGETS, {node: (address, INSTANCE_COUNTS[role])
                                           for role, (node, address) in NODES.items()})

    def test_legacy_two_project_generation_and_retired_panels(self):
        resources = {kind: [d for d in docs if d['metadata'].get('project', d['metadata']['name']) != 'xpu-monitoring']
                     for kind, docs in self.resources.items()}
        generated = split.build(resources)
        split.validate(generated)
        self.assertEqual(len(generated["dashboards"]), 20)
        for d in generated['dashboards']:
            self.assertFalse(set(d['spec']['panels']) & {'live-idle-5', 'live-idle-15', 'live-idle-30', 'live-idle-60'})


if __name__ == "__main__":
    unittest.main()
