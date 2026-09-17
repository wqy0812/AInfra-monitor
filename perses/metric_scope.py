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
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return re.sub(r'(?<!非)流式请求速率', '请求速率', text)


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
            extra = 'request_scope="' + gateway_scope(metric) + '"'
        if extra:
            body += (',' if body else '') + extra
        return name + '{' + body + '}'
    result = re.sub(r'([a-zA-Z_:][a-zA-Z0-9_:]*)?\{([^{}]*)\}', replace, query)
    return result.replace('unless ((changes(aigate_profile_group_start_time_seconds', 'unless ignoring(request_scope) ((changes(aigate_profile_group_start_time_seconds')
