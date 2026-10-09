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
        root = Path(__file__).parent / 'projects/dcu-monitoring/dashboards'
        self.source = json.loads((root / 'hosts-dcu.json').read_text())
        hardware = json.loads((root / 'accelerator-resources.json').read_text())
        # Reconstruct the legacy mixed-layout input for this legacy converter test.
        self.source['spec']['panels'] = {k.removeprefix('core-'):v for k,v in self.source['spec']['panels'].items()}
        aliases = dict(zip(['utilization','memory-used','temperature','power','memory-total','memory-ratio'], ['p5','p6','p7','p8','extra-vram-total','extra-vram-ratio']))
        self.source['spec']['panels'].update({aliases[k.removeprefix('core-')]:v for k,v in hardware['spec']['panels'].items()})
        # The legacy input still had the ratio panels retired on 2026-10-09; stand in with same-source panels.
        panels = self.source['spec']['panels']
        for legacy, source in (('p2', 'extra-fs-free'), ('extra-memory-ratio', 'p1'), ('extra-vram-ratio', 'extra-vram-total')):
            panels.setdefault(legacy, copy.deepcopy(panels[source]))
        for p in self.source['spec']['panels'].values():
            for q in p['spec']['queries']:
                x=q['spec']['plugin']['spec'];x['query']=x['query'].replace(',node=~"$role"','')
        self.source['spec']['variables'] = hardware['spec']['variables']
        from project_split import grid
        from xpu_hardware import PANELS
        self.source['spec']['layouts'][0]['spec']['items'] = grid(list(self.source['spec']['panels']))
        for key, (_, unit, _, _) in PANELS.items():
            self.source['spec']['panels'][key]['spec']['plugin']['spec']['yAxis']['label'] = unit

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
