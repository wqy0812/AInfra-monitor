"""Current in-flight observations. These are gauges, not completed-request rates."""
import copy
import json
from generate import panel
from metric_scope import gateway_scope

ENVIRONMENTS = [('dcu-pd', 'DCU 主机网关', '#1976D2'), ('a3-vllm', 'A3 主机网关', '#ED6C02')]
STAGES = {
    'routing_discovery': '路由与模型发现',
    'preparing_upstream': '构造下游请求', 'waiting_upstream_headers': '等待下游响应头',
    'waiting_first_output': '等待首个有效输出', 'streaming': '读取后续流',
    'reading_response': '读取完整响应', 'writing_client': '向客户端写入',
    'finishing_stream': '流结束收尾',
}
PREFIX = 'live-'
RETIRED_PANELS = {'live-idle-' + str(t) for t in (5, 15, 30, 60)}


def selector(metric, environment, extra=''):
    if metric.startswith('aigate_'):
        extra = 'request_scope=' + json.dumps(gateway_scope(metric)) + (',' + extra if extra else '')
    return metric + '{job="aigate",environment=' + json.dumps(environment) + (',' + extra if extra else '') + '}'


def source_gate(environment):
    up = selector('up', environment)
    boot = selector('aigate_profile_start_time_seconds', environment)
    return (f'({up} == 1) and (time() - timestamp({up}) < 15) '
            f'and (min_over_time({up}[$__interval]) == 1) and (count_over_time({up}[$__interval]) >= ($__interval / 5)) '
            f'and on(job,instance,environment) ((time() - timestamp({boot}) < 15) '
            f'and (time() - {boot} >= $__interval) and (changes({boot}[$__interval]) == 0) '
            f'and (count_over_time({boot}[$__interval]) >= ($__interval / 5)))')


def complete_gauge(metric, environment, extra=''):
    s = selector(metric, environment, extra)
    groups = selector('aigate_live_backend_groups', environment)
    width = len(STAGES) if metric in ('aigate_inflight_oldest_stage', 'aigate_inflight_requests_by_stage') and not extra else 4 if metric == 'aigate_stream_idle_requests' and not extra else 1
    inventory = (f'(count by(job,instance,environment) ({s} and (time() - timestamp({s}) < 15)) '
                 f'== on(job,instance,environment) ({groups} * {width})) '
                 f'and on(job,instance,environment) (time() - timestamp({groups}) < 15) '
                 f'and on(job,instance,environment) (count_over_time({groups}[$__interval]) >= ($__interval / 5))')
    good = (f'(count_over_time({s}[$__interval]) >= ($__interval / 5)) '
            f'and (time() - timestamp({s}) < 15) '
            f'and on(job,instance,environment) ({inventory}) '
            f'and on(job,instance,environment) ({source_gate(environment)})')
    bad = f'(count_over_time({s}[$__interval]) unless ({good}))'
    return f'({s} and ({good})) unless on(environment) (count by(environment) ({bad}))'


def aggregate(metric, environment, operation='sum', extra=''):
    return f'{operation} by(environment) ({complete_gauge(metric, environment, extra)})'


def oldest(environment):
    age = complete_gauge('aigate_inflight_oldest_age_seconds', environment)
    stages = complete_gauge('aigate_inflight_oldest_stage', environment)
    stage_samples = selector('aigate_inflight_oldest_stage', environment)
    # Select the backend first, then attach the stage from that same source.
    joined = (f'(topk by(environment) (1, ({age}) > 0)) '
              f'* on(job,instance,environment,backend) group_left(stage) '
              f'((({stages}) == 1) and (min_over_time({stage_samples}[$__interval]) == 1))')
    for stage, label in STAGES.items():
        joined = f'label_replace({joined}, "stage_name", {json.dumps(label, ensure_ascii=False)}, "stage", "{stage}")'
    # A measured empty live set is zero. No absent-series fallback is used.
    idle = f'label_replace((max by(environment) ({age})) == 0, "stage_name", "无在途请求", "environment", ".*")'
    return f'({joined}) or ({idle})'


def build_panels():
    common = '按指标范围统计。当前在途状态，5 秒采集、15 秒刷新；采集失败、缺样、过期、升级前或跨进程重启窗口留空。正常空闲显示零。'
    panels = {}
    def comparison(key, title, make_query, unit, description, legends=None):
        p = panel(title, [(make_query(env), (legends or {}).get(env, name)) for env, name, _ in ENVIRONMENTS], unit, common + description)
        p['spec']['plugin']['spec']['visual']['lineWidth'] = 2
        p['spec']['plugin']['spec']['querySettings'] = [
            {'queryIndex': i, 'colorMode': 'fixed', 'colorValue': color}
            for i, (_, _, color) in enumerate(ENVIRONMENTS)]
        panels[PREFIX + key] = p
    comparison('waiting', '等待首个有效输出 · 请求数', lambda e: aggregate('aigate_streams_waiting_first_output', e), '请求', '仅流式；解析为 stream=true 后计入，包含路由及模型发现。')
    comparison('wait-max', '等待首个有效输出 · 最长等待', lambda e: aggregate('aigate_stream_first_output_wait_max_seconds', e, 'max'), '秒', '从网关接收请求起计时，收到有效正文、推理、拒绝或工具增量后退出。')
    comparison('idle-max', '流停顿 · 最长无新内容间隔', lambda e: aggregate('aigate_stream_idle_max_seconds', e, 'max'), '秒', '观察网关收到的下游内容；客户端写入受阻时结合当前阶段判断。')
    comparison('oldest', '最老在途请求 · 年龄与当前阶段', oldest, '秒', '流式及非流式请求；每个网关选择最老请求并关联其后端及阶段。阶段变化不跨线连接。', {env: name + ' · {{backend}} · {{stage_name}}' for env, name, _ in ENVIRONMENTS})
    for env, name, _ in ENVIRONMENTS:
        queries = [(f'sum by(environment) ({complete_gauge("aigate_inflight_requests_by_stage", env, "stage=" + json.dumps(stage))})', label) for stage, label in STAGES.items()]
        panels[PREFIX + 'stages-' + env] = panel(name + ' · 在途处理阶段', queries, '请求', common + '一个请求同时只属于一个阶段；流读取与客户端写入会切换阶段。')
    comparison('unknown', '有效输出观察未知 · 流数量', lambda e: aggregate('aigate_stream_observation_unknown', e), '流', '解析异常或超过观察上限的在途流；退出无法可靠判断的等待/停顿分类，但仍计入年龄与阶段。')
    from gateway_generation import complete_rate
    p = panel('非流式请求数', [(f'60 * ({complete_rate("aigate_nonstream_requests_total", env)})', name) for env, name, _ in ENVIRONMENTS],
              '请求', '最近 1 分钟新增非流式请求数，按解析确认 stream=false（含省略 stream）时计数；rate × 60 为窗口估算值。非流式同时计入请求量、画像、总耗时、错误率及在途状态，不进入首增量或流停顿统计。采集缺失、重启或窗口不足留空，正常无请求显示零。两个网关分别计数，同一请求经过两层网关时不可相加去重。')
    p['spec']['plugin']['spec']['querySettings'] = [
        {'queryIndex': i, 'colorMode': 'fixed', 'colorValue': color}
        for i, (_, _, color) in enumerate(ENVIRONMENTS)]
    panels[PREFIX + 'nonstream-count'] = p
    return panels


def extend_dashboard(document):
    """Replace only our fixed live IDs; preserve existing panels and layout offsets."""
    result = copy.deepcopy(document)
    spec = result['spec']
    live = build_panels()
    rows = (len(live) + 1) // 2
    layouts = spec['layouts']
    if len(layouts) != 1 or layouts[0]['kind'] != 'Grid':
        raise ValueError('Expected the existing single grid layout')
    items = layouts[0]['spec']['items']
    owned_refs = {'#/spec/panels/' + key for key in set(live) | RETIRED_PANELS}
    old_live = [x for x in items if x.get('content', {}).get('$ref', '') in owned_refs]
    old_height = max((x['y'] + x['height'] for x in old_live), default=0)
    preserved = [x for x in items if x not in old_live]
    for item in preserved:
        item['y'] += rows * 8 - old_height
    added = [{'x': (i % 2) * 12, 'y': (i // 2) * 8, 'width': 12, 'height': 8,
              'content': {'$ref': '#/spec/panels/' + key}} for i, key in enumerate(live)]
    if len(added) % 2:
        # Fill the last row so Perses vertical compaction cannot pull an old
        # right-column panel up into the new diagnostic region.
        added[-1]['width'] = 24
    spec['panels'] = {**live, **{k: v for k, v in spec['panels'].items() if k not in live and k not in RETIRED_PANELS}}
    layouts[0]['spec']['items'] = added + preserved
    return result
