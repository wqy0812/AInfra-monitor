import copy
import json
from pathlib import Path
import re
import tempfile
import unittest

from generate_xpu import generate
from xpu_hosts import configure, EMPTY


class XpuHostsTest(unittest.TestCase):
    def setUp(self):
        self.source = json.loads((Path(__file__).parent / 'projects/dcu-monitoring/dashboards/hosts-dcu.json').read_text())

    def test_host_scope_layout_and_card_metrics(self):
        before = copy.deepcopy(self.source)
        result = configure(self.source)
        self.assertEqual(before, self.source)
        self.assertEqual(result['spec']['layouts'], before['spec']['layouts'])
        counts = [0, 0]
        for key, panel in result['spec']['panels'].items():
            self.assertEqual(panel['spec']['plugin'], before['spec']['panels'][key]['spec']['plugin'])
            queries = [q['spec']['plugin']['spec']['query'] for q in panel['spec']['queries']]
            hardware = all("node_xpu_" in q for q in queries)
            counts[int(hardware)] += 1
            if hardware:
                for q in queries:
                    self.assertIn('job="xpu-hardware"', q)
                    self.assertIn('devid=~"$device"', q)
                    self.assertNotIn(EMPTY, q)
                continue
            for query in queries:
                for labels in re.findall(r'\{([^{}]*)\}', query):
                    self.assertIn('environment="xpu-pd"', labels)
                    self.assertIn('job="node-xpu"', labels)
                self.assertNotIn('dcu-pd', query)
                self.assertNotIn('node-prefill', query)
        self.assertEqual(counts, [10, 6])

    def test_generator_does_not_blank_hosts_again(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = {'dcu-monitoring': {
                'project': {'metadata': {'name': 'dcu-monitoring'}},
                'datasources': [{'metadata': {'name': 'vm', 'project': 'dcu-monitoring'}}],
                'dashboards': [self.source]}}
            path = root / 'snapshot.json'
            path.write_text(json.dumps(snapshot))
            output = generate(path, root / 'output')
            result = json.loads((output / 'dashboards/hosts-xpu.json').read_text())
            self.assertIn('node_cpu_seconds_total', result['spec']['panels']['p0']['spec']['queries'][0]['spec']['plugin']['spec']['query'])
            self.assertEqual(result['spec']['variables'][0]['spec']['plugin']['spec']['values'][1]['value'], 'xpu-2')


if __name__ == '__main__':
    unittest.main()
