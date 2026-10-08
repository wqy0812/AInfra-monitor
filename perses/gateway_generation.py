# Project-aware CLI routing; legacy helpers below remain importable.
if __name__ == "__main__":
    from project_split import main
    main()
    raise SystemExit(0)

"""Two gateway series per chart, with conservative counter coverage gates."""
import json
from pathlib import Path
from generate import panel
from metric_scope import gateway_scope

ROOT = Path(__file__).resolve().parent
ENVIRONMENTS = [('dcu-pd', 'DCU 主机网关', '#1976D2'), ('a3-vllm', 'A3 主机网关', '#ED6C02')]
ENDED = 'aigate_generation_requests_ended_total'
ERRORS = 'aigate_upstream_errors_total'
ORIGIN = 'aigate_error_metrics_start_time_seconds'


def selector(metric, environment, extra=''):
    if metric.startswith('aigate_'):
        extra = 'request_scope=' + json.dumps(gateway_scope(metric)) + (',' + extra if extra else '')
    return metric + '{job="aigate",environment=' + json.dumps(environment) + (',' + extra if extra else '') + '}'


def source_gate(environment):
    up = selector('up', environment)
    origin = selector(ORIGIN, environment)
    # Validate both the rate window and the plotted interval. Long query steps
    # must not bridge a restart or scrape outage hidden between plotted points.
    return (f'({up} == 1) and (time() - timestamp({up}) < 15) '
            f'and (min_over_time({up}[1m]) == 1) and (count_over_time({up}[1m]) >= 12) '
            f'and (min_over_time({up}[$__interval]) == 1) and (count_over_time({up}[$__interval]) >= ($__interval / 5)) '
            f'and on(job,instance,environment) ((time() - {origin} >= 60) and (time() - timestamp({origin}) < 15) '
            f'and (changes({origin}[1m]) == 0) and (count_over_time({origin}[1m]) >= 12) '
            f'and (changes({origin}[$__interval]) == 0) and (count_over_time({origin}[$__interval]) >= ($__interval / 5)))')


def complete_rate(metric, environment, extra=''):
    s = selector(metric, environment, extra)
    good = (f'(count_over_time({s}[1m]) >= 12) and (resets({s}[1m]) == 0) '
            f'and (count_over_time({s}[$__interval]) >= ($__interval / 5)) and (resets({s}[$__interval]) == 0) '
            f'and (time() - timestamp({s}) < 15) '
            f'and on(job,instance,environment) ({source_gate(environment)})')
    # Do not silently add only the healthy subset of backend/model/result series.
    bad = f'(count_over_time({s}[1m]) unless ({good}))'
    return (f'(sum by(environment) (rate({s}[1m]) and ({good}))) '
            f'unless on(environment) (count by(environment) ({bad}))')


def ratio(metric, environment, extra=''):
    numerator = complete_rate(metric, environment, extra)
    denominator = complete_rate(ENDED, environment)
    return f'100 * (({numerator}) / on(environment) (({denominator}) > 0))'


def queries(environment):
    return [complete_rate(ENDED, environment), complete_rate(ERRORS, environment),
            ratio(ERRORS, environment), ratio(ENDED, environment, 'result="client_cancelled"'),
            ratio(ENDED, environment, 'result="client_disconnected"'),
            ratio(ENDED, environment, 'result="unknown"')]


def build_dashboard():
    common = '统计流式及非流式生成请求。1 分钟窗口；无样本、分母为零、采集断档或计数重置留空。未出现的类别不补零，已有有效空闲序列显示零。'
    titles = [('生成结束速率', '请求 / 秒'), ('后端错误速率', '次 / 秒'), ('后端错误率', '%'),
              ('客户端取消占比', '%'), ('客户端断开占比', '%'), ('未知结果占比', '%')]
    expressions = [queries(env) for env, _, _ in ENVIRONMENTS]
    panels = {}
    for i, (title, unit) in enumerate(titles):
        desc = common
        if 2 <= i <= 5:
            desc += ' 分母为全部流式生成结束请求，包含成功、错误、取消、断开和未知；不含入口拒绝、发现及探测。'
        p = panel(title, [(expressions[k][i], name) for k, (_, name, _) in enumerate(ENVIRONMENTS)], unit, desc)
        chart = p['spec']['plugin']['spec']
        chart['visual']['lineWidth'] = 2
        chart['querySettings'] = [{'queryIndex': k, 'colorMode': 'fixed', 'colorValue': color}
                                  for k, (_, _, color) in enumerate(ENVIRONMENTS)]
        if unit == '%':
            chart['yAxis']['max'] = 100
        panels[f'generation-{i}'] = p
    return {'kind': 'Dashboard', 'metadata': {'name': 'gateway-generation', 'project': 'dcu-monitoring'}, 'spec': {
        'display': {'name': '网关生成监控', 'description': '蓝色：DCU 主机网关；橙色：A3 主机网关。按网关采集环境区分，DCU 网关可能转发到 A3。同一请求经两层网关分别计数，勿相加作为全局请求量。'},
        'duration': '1h', 'refreshInterval': '15s', 'panels': panels,
        'layouts': [{'kind': 'Grid', 'spec': {'items': [
            {'x': (i % 2) * 12, 'y': (i // 2) * 8, 'width': 12, 'height': 8,
             'content': {'$ref': '#/spec/panels/' + key}} for i, key in enumerate(panels)]}}]}}
