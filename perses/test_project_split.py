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
        self.assertEqual(len(self.resources["dashboards"]), 16)
        self.assertEqual(split.no_request_filter(
            'm{request_scope="streaming",a="b"} + m{a="b",is_streaming!="false"} + m{stream="true"}'),
            'm{a="b"} + m{a="b"} + m{}')

    def test_generation_preserves_scopes_and_colors(self):
        for d in self.resources["dashboards"]:
            if d["metadata"]["name"] == "gateway-generation":
                env = split.PROJECTS[d["metadata"]["project"]]
                self.assertIn("live-stages-" + env, d["spec"]["panels"])
                self.assertEqual(len(d["spec"]["panels"]), 17)
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

    def test_rollback_restores_updated_spec(self):
        import project_release as release
        old = {"kind": "Dashboard", "metadata": {"project": "dcu-monitoring", "name": "example"}, "spec": {"display": {"name": "old"}}}
        new = copy.deepcopy(old)
        new["spec"]["display"]["name"] = "new"
        calls = []
        def fake_http(url, method="GET", data=None):
            calls.append((method, data))
            return copy.deepcopy(new) if method == "GET" else None
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "journal.json").write_text(json.dumps([{"action": "update", "before": old, "candidate": new}]))
            with patch.object(release, "http", fake_http):
                release.rollback(root)
            self.assertEqual(calls[-1][0], "PUT")
            self.assertEqual(calls[-1][1]["spec"], old["spec"])

    def test_rollback_keeps_concurrent_project_children(self):
        import project_release as release
        project = {"kind": "Project", "metadata": {"name": "a3-monitoring"}, "spec": {"display": {"name": "A3"}}}
        methods = []
        def fake_http(url, method="GET", data=None):
            methods.append(method)
            return [{"kind": "Dashboard", "metadata": {"name": "user-created"}}] if url.endswith("/dashboards") else project
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "journal.json").write_text(json.dumps([{"action": "create", "candidate": project}]))
            with patch.object(release, "http", fake_http), self.assertRaises(AssertionError):
                release.rollback(root)
            self.assertNotIn("DELETE", methods)


if __name__ == "__main__":
    unittest.main()
