import copy
import unittest
from gateway_generation import build_dashboard
from publish_gateway_scope import migrate, scope_query


class ScopeMigrationTest(unittest.TestCase):
    def test_preserves_edits_and_scopes_old_queries(self):
        old = build_dashboard()
        for p in old['spec']['panels'].values():
            for q in p['spec']['queries']:
                s = q['spec']['plugin']['spec']
                s['query'] = s['query'].replace('request_scope="streaming",', '')
        old['spec']['panels']['generation-0']['spec']['display']['name'] = '自定义标题'
        old['spec']['panels']['custom'] = copy.deepcopy(old['spec']['panels']['generation-0'])
        old['spec']['panels']['custom']['spec']['queries'][0]['spec']['plugin']['spec']['query'] = 'up{job="custom"}'
        original = copy.deepcopy(old)
        new = migrate(old)
        self.assertEqual(old, original)
        self.assertEqual(migrate(new), new)
        self.assertEqual(new['spec']['panels']['generation-0']['spec']['display']['name'], '自定义标题')
        self.assertEqual(new['spec']['panels']['custom']['spec']['queries'][0], old['spec']['panels']['custom']['spec']['queries'][0])
        self.assertIn('live-nonstream-count', new['spec']['panels'])
        for k in range(6):
            for q in new['spec']['panels'][f'generation-{k}']['spec']['queries']:
                self.assertIn('request_scope="all"', q['spec']['plugin']['spec']['query'])

    def test_scrape_gate_matches_source_without_scope(self):
        from generate import gateway_rate
        q = gateway_rate('aigate_requests_started_total')
        for function in ('min_over_time', 'count_over_time'):
            self.assertIn('and on(job,instance) (' + function + '(up{job="aigate"}[1m])', q)
        old = q.replace('and on(job,instance) (min_over_time(up{job="aigate"}[1m])',
                        'and (min_over_time(up{job="aigate"}[1m])')
        self.assertEqual(scope_query(old), q)
        self.assertEqual(scope_query(q), q)

    def test_scope_is_specific_and_idempotent(self):
        old = 'aigate_nonstream_requests_total{job="aigate"} + aigate_requests_inflight{job="aigate"} + up{job="aigate"}'
        new = scope_query(old)
        self.assertEqual(scope_query(new), new)
        self.assertIn('aigate_nonstream_requests_total{job="aigate",request_scope="nonstreaming"}', new)
        self.assertIn('up{job="aigate"}', new)
        self.assertEqual(new.count('request_scope='), 2)


if __name__ == '__main__':
    unittest.main()
