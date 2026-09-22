import copy
import unittest
from gateway_generation import build_dashboard
from gateway_live import extend_dashboard, build_panels


class DashboardMergeTest(unittest.TestCase):
    def test_preserve_edits_and_repeat(self):
        old = build_dashboard()
        old['spec']['panels']['generation-0']['spec']['display']['name'] = '用户修改标题'
        old['spec']['panels']['live-user-panel'] = copy.deepcopy(old['spec']['panels']['generation-0'])
        old['spec']['layouts'][0]['spec']['items'].append({'x': 0, 'y': 24, 'width': 24, 'height': 5, 'content': {'$ref': '#/spec/panels/live-user-panel'}})
        new = extend_dashboard(old)
        self.assertEqual(extend_dashboard(new), new)
        for key, value in old['spec']['panels'].items():
            self.assertEqual(new['spec']['panels'][key], value)
        before_items = old['spec']['layouts'][0]['spec']['items']
        after_items = new['spec']['layouts'][0]['spec']['items'][-len(before_items):]
        for before, after in zip(before_items, after_items):
            expected = dict(before, y=before['y'] + 32)
            self.assertEqual(after, expected)
        self.assertEqual(len(build_panels()), 8)

    def test_reject_unexpected_layout(self):
        old = build_dashboard()
        old['spec']['layouts'].append(copy.deepcopy(old['spec']['layouts'][0]))
        with self.assertRaises(ValueError):
            extend_dashboard(old)


if __name__ == '__main__':
    unittest.main()
