"""Lossless, snapshot-derived query consolidation for fourteen known panels."""
import copy
import json
import re

PROJECTS = ('a3-monitoring', 'dcu-monitoring', 'xpu-monitoring')
PERCENTILES = ('core-ttft', 'core-itl', 'core-e2e')
SERIES_LABEL = 'perses_series'
ORDER_LABEL = 'perses_order'


def target(project, dashboard, panel):
    return project in PROJECTS and (
        (dashboard == 'backend-performance' and panel in PERCENTILES)
        or (dashboard == 'gateway-generation' and panel == 'live-stages')
        or (project == 'a3-monitoring' and dashboard == 'a3-cache'
            and panel in ('mooncake-requests', 'mooncake-failures')))


def consolidate(panel, kind):
    result = copy.deepcopy(panel)
    spec = result['spec']
    queries = spec['queries']
    if len(queries) == 1:
        return result
    if spec['plugin']['spec'].get('querySettings'):
        raise ValueError('Per-query rendering overrides need explicit migration')
    plugins = [q['spec']['plugin']['spec'] for q in queries]
    if any(q['spec']['plugin']['kind'] != 'PrometheusTimeSeriesQuery' for q in queries):
        raise ValueError('Unexpected query plugin')
    settings = [{k: v for k, v in p.items() if k not in ('query', 'seriesNameFormat')} for p in plugins]
    if any(x != settings[0] for x in settings[1:]):
        raise ValueError('Query options differ')
    expressions = [p['query'] for p in plugins]
    if kind == 'percentiles':
        if len(queries) != 3 or len({p.get('seriesNameFormat') for p in plugins}) != 1:
            raise ValueError('Expected three identically formatted percentile queries')
        normalized = [re.sub(r'\.p(?:50|95|99)(?=")', '.PERCENTILE', e) for e in expressions]
        if len(set(normalized)) != 1 or '.PERCENTILE' not in normalized[0]:
            raise ValueError('Percentile expressions are not equivalent templates')
        if [set(re.findall(r'\.p(50|95|99)(?=")', e)) for e in expressions] != [{'50'}, {'95'}, {'99'}]:
            raise ValueError('Unexpected percentile order or selectors')
        expression = normalized[0].replace('.PERCENTILE', '.(p50|p95|p99)')
        # The old three query groups are percentile-major. A bare selector is
        # path-major and changes the legend and palette assignment. Remove the
        # ordering label is presentation metadata, just as for stage/operation.
        # sort_by_label must be outermost: VM otherwise sorts range results by
        # the ordinary label set again.
        for index, percentile in enumerate(('p50', 'p95', 'p99')):
            expression = ('label_replace((' + expression + '), "' + ORDER_LABEL + '", "'
                          + ('%02d' % index) + '", "path", ".*[.]' + percentile + '")')
        expression = 'sort_by_label((' + expression + '), "' + ORDER_LABEL + '", "path")'
    else:
        expected = 8 if kind == 'stages' else 10
        if len(queries) != expected or any(SERIES_LABEL in e or ORDER_LABEL in e for e in expressions):
            raise ValueError('Unexpected series count or reserved label')
        normalized, arguments = [], []
        for expression in expressions:
            if kind == 'stages':
                values = set(re.findall(r'\bstage="([^"]+)"', expression))
                if len(values) != 1:
                    raise ValueError('Expected one stage per query')
                argument = next(iter(values))
                body = expression.replace('stage=' + json.dumps(argument), 'stage=series_argument')
            else:
                values = set(re.findall(r'\b(master_[a-z_]+_(?:requests|failures)_total)\{', expression))
                if len(values) != 1:
                    raise ValueError('Expected one Mooncake metric per query')
                argument = next(iter(values))
                body = expression.replace(argument + '{', '{__name__=series_argument,')
            arguments.append(argument)
            normalized.append(body)
        if len(set(normalized)) != 1 or len(set(arguments)) != expected:
            raise ValueError('Series expressions are not equivalent templates')
        if kind == 'stages':
            # Partition every aggregate and invalidity veto by stage. Matching
            # backend-group/up series remains per gateway, with many stages on
            # the left. This evaluates the shared selectors once.
            expression = normalized[0].replace('stage=series_argument', 'stage=~' + json.dumps('|'.join(arguments)))
            expression = expression.replace('sum by(environment)', 'sum by(environment,stage)')
            expression = expression.replace('count by(environment)', 'count by(environment,stage)')
            expression = expression.replace('count by(job,instance,environment)', 'count by(job,instance,environment,stage)')
            expression = expression.replace('and on(job,instance,environment) ((count by(job,instance,environment,stage)',
                                            'and on(job,instance,environment,stage) ((count by(job,instance,environment,stage)')
            expression = expression.replace('== on(job,instance,environment)', '== on(job,instance,environment) group_left()')
            expression = expression.replace('unless on(environment)', 'unless on(environment,stage)')
            for i, (argument, p) in enumerate(zip(arguments, plugins)):
                expression = ('label_replace(label_replace((' + expression + '), "' + SERIES_LABEL + '", '
                    + json.dumps(p['seriesNameFormat'], ensure_ascii=False) + ', "stage", ' + json.dumps(argument)
                    + '), "' + ORDER_LABEL + '", "' + ('%02d' % i) + '", "stage", ' + json.dumps(argument) + ')')
            expression = 'sort_by_label(label_del((' + expression + '), "stage"), "' + ORDER_LABEL + '")'
            plugins[0]['seriesNameFormat'] = '{{' + SERIES_LABEL + '}}'
            plugins[0]['query'] = expression
            spec['queries'] = [queries[0]]
            return result
        branches = [
            'label_set(perses_series_query(' + json.dumps(argument) + '), '
            + json.dumps(SERIES_LABEL) + ', ' + json.dumps(p['seriesNameFormat'], ensure_ascii=False)
            + ', ' + json.dumps(ORDER_LABEL) + ', ' + json.dumps('%02d' % i) + ')'
            for i, (argument, p) in enumerate(zip(arguments, plugins))
        ]
        expression = ('WITH (perses_series_query(series_argument) = (' + normalized[0] + ')) '
                      'sort_by_label(union(' + ', '.join(branches) + '), "' + ORDER_LABEL + '")')
        plugins[0]['seriesNameFormat'] = '{{' + SERIES_LABEL + '}}'
    plugins[0]['query'] = expression
    spec['queries'] = [queries[0]]
    return result


def optimize(document):
    result = copy.deepcopy(document)
    if result.get('kind') != 'Dashboard':
        return result
    project = result['metadata']['project']
    dashboard = result['metadata']['name']
    for key, panel in result['spec']['panels'].items():
        if target(project, dashboard, key):
            kind = 'percentiles' if dashboard == 'backend-performance' else 'stages' if key == 'live-stages' else 'operations'
            result['spec']['panels'][key] = consolidate(panel, kind)
    return result


def prepare(resources):
    result = copy.deepcopy(resources)
    changes = []
    for i, document in enumerate(resources['dashboards']):
        candidate = optimize(document)
        result['dashboards'][i] = candidate
        for key, before in document['spec']['panels'].items():
            after = candidate['spec']['panels'][key]
            if before != after:
                changes.append({'project': document['metadata']['project'],
                                'dashboard': document['metadata']['name'], 'panel': key,
                                'before': before, 'after': after})
    return result, changes
