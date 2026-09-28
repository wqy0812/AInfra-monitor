import copy
from a3_coverage import annotate, MARKER


def test_only_descriptions_change_and_exact_boundary_survives_regeneration():
    document = {'metadata': {'project': 'a3-monitoring', 'name': 'backend-decode'},
                'spec': {'display': {'name': 'Decode'}, 'layouts': [1], 'panels': {'p': {
                    'spec': {'display': {'name': 'tokens', 'description': 'original'},
                             'queries': [{'query': 'unchanged'}]}}}}}
    saved = copy.deepcopy(document)
    changed = annotate(document, '2026-09-28T14:19:30+08:00')
    assert document == saved
    assert changed['spec']['panels']['p']['spec']['queries'] == document['spec']['panels']['p']['spec']['queries']
    assert changed['spec']['layouts'] == document['spec']['layouts']
    assert MARKER in changed['spec']['display']['description']
    assert annotate(changed) == changed
    assert annotate(changed, '2026-09-28T14:19:30+08:00') == changed
    document['metadata']['project'] = 'xpu-monitoring'
    assert annotate(document) == document
