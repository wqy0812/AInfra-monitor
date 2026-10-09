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
                self.assertEqual(len(d["spec"]["panels"]), 8)
                d = next(x for x in self.resources["dashboards"] if x["metadata"]["project"] == d["metadata"]["project"] and x["metadata"]["name"] == "gateway-requests")
                self.assertEqual(len(d["spec"]["panels"]["generation-0"]["spec"]["queries"]), 1)
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

    def test_duplicate_ratio_panels_stay_retired(self):
        from retire_duplicate_ratios import RETIRED, retire
        dashboards = {(d['metadata']['project'], d['metadata']['name']): d for d in self.resources['dashboards']}
        for (project, name), retired in RETIRED.items():
            d = dashboards[(project, name)]
            self.assertFalse(set(retired) & set(d['spec']['panels']), (project, name))
            for keeper in filter(None, retired.values()):
                self.assertIn(keeper, d['spec']['panels'])
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

    def test_legacy_two_project_generation_and_retired_panels(self):
        resources = {kind: [d for d in docs if d['metadata'].get('project', d['metadata']['name']) != 'xpu-monitoring']
                     for kind, docs in self.resources.items()}
        generated = split.build(resources)
        split.validate(generated)
        self.assertEqual(len(generated['dashboards']), 20)
        for d in generated['dashboards']:
            self.assertFalse(set(d['spec']['panels']) & {'live-idle-5', 'live-idle-15', 'live-idle-30', 'live-idle-60'})


if __name__ == "__main__":
    unittest.main()
