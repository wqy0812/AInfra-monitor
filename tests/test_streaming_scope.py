"""Request-profile and dashboard population contracts."""
import json
from pathlib import Path
from monitoring.profile_counters import metric_selector, summary_rows
from monitoring.request_profile import selector

def test_profile_scope_follows_metric_and_discards_old_general_streaming_samples():
    assert 'request_scope="all"' in selector('requests_routed_total')
    assert 'request_scope="streaming"' in selector('first_increment_seconds_count')
    assert 'request_scope=~"all|streaming"' in metric_selector({},['requests_started_total','first_increment_seconds_count'])
    rs = [{'metric':{'__name__':name,'request_scope':scope,'rollup':'last_over_time'},'value':[100,'2']}
          for name in ('aigate_requests_started_total','aigate_first_increment_seconds_count') for scope in ('all','streaming')]
    selected = [dict(identity) for identity in summary_rows(rs)]
    assert len(selected)==2
    assert {r['__name__']:r['request_scope'] for r in selected} == {'aigate_requests_started_total':'all','aigate_first_increment_seconds_count':'streaming'}


def test_saved_dashboard_queries_follow_the_current_metric_policy():
    import sys
    root = Path(__file__).parents[1]/'perses'
    sys.path.insert(0,str(root))
    from metric_scope import apply_scope
    from project_split import current_request_schema
    for p in list((root/'projects').glob('*/dashboards/*.json'))+list((root/'dashboards').glob('*.json')):
        doc=json.loads(p.read_text())
        for panel in doc['spec']['panels'].values():
            for query in panel['spec'].get('queries',[]):
                expr=query['spec']['plugin']['spec']['query']
                assert expr == apply_scope(expr), p
                assert expr == current_request_schema(expr), p
