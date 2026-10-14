"""Apply only filters required by each metric's observation semantics."""
import re

REQUEST_LABEL = re.compile(r'(?:^|,)\s*(?:stream|is_streaming|request_scope)\s*(?:=~|!~|!=|=)\s*"(?:\\.|[^"\\])*"\s*(?=,|$)')


def request_description(text):
    """Migrate known old prose without relabelling stream-only observations."""
    replacements = {
        '当前已纳入画像且尚未结束的流式请求数量': '当前已纳入画像且尚未结束的请求数量（含流式与非流式）',
        '网关解析确认 stream=true、进入请求画像时累计一次到达': '网关完成请求解析、进入请求画像时累计一次到达（含流式与非流式）',
        '进入下游生成阶段的流式请求结束速率': '进入下游生成阶段的请求结束速率（含流式与非流式）',
        '当前流式请求中年龄最大的请求': '当前全部在途请求中年龄最大的请求',
        '当前流式请求按处理阶段分组计数': '当前全部在途请求按处理阶段分组计数',
        '非流式仅在此图统计。': '本图展示总请求中的非流式子集；它们也计入请求量、画像、总耗时、错误及在途指标。',
        '全部流式生成结束请求': '全部生成结束请求',
        '且只统计流式请求画像': '画像包含流式及非流式请求',
        '读取流式请求的错误响应': '读取完整响应',
        '读取后续流、读取错误响应': '读取后续流、读取完整响应',
        '主机网关。按采集环境归属': 'Router 功能服务。按 Router 所属环境归属',
        '按网关所在环境归属，DCU 网关转发至 A3 时仍计入 DCU 曲线。': '按功能服务连接的 Router 所属环境归属；切流入口变化不改变归属。',
        '同一请求经两层网关会分别计数，不可相加当作全局去重请求量。': '切流服务不重复统计；经切流入口与直连功能服务的请求均由目标功能服务统计。',
        '同一请求经两层网关分别计数，勿相加作为全局请求量。': '切流服务不重复统计；功能服务包含切流入口与直连请求。',
        '两个网关分别计数，同一请求经过两层网关时不可相加去重。': '各 Router 功能服务分别统计，切流服务不重复统计。',
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return re.sub(r'(?<!非)流式请求速率', '请求速率', text)


def proxy_descriptions(resources):
    """Keep generated guidance aligned with the Router-bound observation source."""
    owner_note = '拆分后按 Router 所属环境统计，包含切流入口与直连请求；切流服务不重复计数。拆分前历史保留原归属。'
    peer_note = '写出耗时观测功能服务的直接连接对端；经切流入口访问时表示功能服务到切流服务这一段，不能据此判定最终客户端阻塞。'
    for dashboard in resources['dashboards']:
        spec = dashboard['spec']
        if dashboard['metadata']['name'].startswith('gateway'):
            desc = request_description(spec['display'].get('description', ''))
            if owner_note not in desc:
                desc += '\n\n' + owner_note
            spec['display']['description'] = desc
        for panel in spec['panels'].values():
            panel = panel['spec']
            desc = request_description(panel['display'].get('description', ''))
            query = ' '.join(q['spec']['plugin']['spec']['query'] for q in panel.get('queries', []))
            if ('aigate_stream_write_active_max_seconds' in query or 'gateway.write_active_max_seconds' in query) and peer_note not in desc:
                desc += '\n\n' + peer_note
            panel['display']['description'] = desc
    return resources


def gateway_scope(name):
    if name == 'aigate_nonstream_requests_total':
        return 'nonstreaming'
    if name.startswith(('aigate_first_increment_seconds', 'aigate_stream_')) or name == 'aigate_streams_waiting_first_output':
        return 'streaming'
    return 'all'


def apply_scope(query):
    def replace(match):
        name, body = match[1] or '', match[2]
        body = REQUEST_LABEL.sub('', body).strip(', ')
        metric = name
        if not metric:
            family = re.search(r'__name__=~?"(aigate_[^"(]+)', body)
            if family:
                metric = family[1]
        extra = ''
        if metric.startswith('aigate_'):
            # Live inventory counts backends, not ordinary/replay series pairs.
            # Retain unlabelled historical data while excluding replay gauges.
            if 'aigate_live_backend_groups' in query and 'traffic!="replay"' not in body:
                body += (',' if body else '') + 'traffic!="replay"'
            extra = 'request_scope="' + gateway_scope(metric) + '"'
        if extra:
            body += (',' if body else '') + extra
        return name + '{' + body + '}'
    result = re.sub(r'([a-zA-Z_:][a-zA-Z0-9_:]*)?\{([^{}]*)\}', replace, query)
    return result.replace('unless ((changes(aigate_profile_group_start_time_seconds', 'unless ignoring(request_scope) ((changes(aigate_profile_group_start_time_seconds')
