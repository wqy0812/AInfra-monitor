"""Remove retired stream idle threshold panels and close their empty grid rows."""
import copy

RETIRED = {'live-idle-' + str(t) for t in (5, 15, 30, 60)}


def remove_panels(document):
    result = copy.deepcopy(document)
    spec = result['spec']
    retired = set(RETIRED) if result['metadata']['name'] == 'gateway-generation' else set()
    if result['metadata']['name'] in ('overview', 'a3-overview'):
        retired.update(k for k, p in spec['panels'].items() if p['spec']['display']['name'] == '主机 CPU')
    for key in retired:
        spec['panels'].pop(key, None)
    refs = {'#/spec/panels/' + key for key in retired}
    for layout in spec['layouts']:
        assert layout['kind'] == 'Grid'
        items = layout['spec']['items']
        kept = [item for item in items if item['content'].get('$ref') not in refs]
        removed = [item for item in items if item not in kept]
        empty_rows = set()
        for item in removed:
            for row in range(item['y'], item['y'] + item['height']):
                if not any(other['y'] <= row < other['y'] + other['height'] for other in kept):
                    empty_rows.add(row)
        for item in kept:
            item['y'] -= sum(row < item['y'] for row in empty_rows)
        layout['spec']['items'] = kept
    return result
