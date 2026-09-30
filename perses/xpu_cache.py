"""XPU Prefill cache panels backed by currently exported scheduler metrics."""
import copy
import json
from pathlib import Path

from generate import panel
from project_queries import Queries
from xpu_topology import IPS

RANK = ',tp_rank="0",pp_rank="0"'
RETIRED = ('load_back_tokens_total', 'evicted_tokens_total',
           'load_back_duration_seconds', 'eviction_duration_seconds')


def configure(source):
    d = copy.deepcopy(source)
    q = Queries('xpu-pd', 'sglang-prefill', ',instance="' + IPS['prefill'] + ':8501"')
    batch = q.gauge('sglang:cache_hit_rate', RANK)
    compute = q.rate('sglang:realtime_tokens_total', RANK + ',mode="prefill_compute"')
    cached = q.rate('sglang:realtime_tokens_total', RANK + ',mode="prefill_cache"')
    idle_ticks = q.rate('sglang:hicache_scheduler_idle_with_pending_total')
    idle_time = q.rate('sglang:hicache_scheduler_idle_with_pending_seconds_total')
    common = '仅取当前 Prefill。每 5 秒采集；失败、过期和缺样留空，有效零值保留。'

    def chart(title, queries, desc, maximum=None):
        p = panel(title, queries, desc=desc + common)
        p['spec']['plugin']['spec']['yAxis'].pop('label', None)
        if maximum is not None:
            p['spec']['plugin']['spec']['yAxis']['max'] = maximum
        return p

    d['spec']['display'] = {'name': '缓存与存储', 'description':
        'XPU Prefill 缓存复用和 HiCache 预取等待。设备 KV 池占用见 Prefill 诊断。仅展示当前原生指标；没有 CPU 缓存容量、分层命中率或回载/淘汰传输指标。'}
    d['spec']['variables'] = []
    d['spec']['panels'] = {
        'p0': chart('采集状态（up）', [(q.s('up') + ' and (time() - timestamp(' + q.s('up') + ') < 15)', 'Prefill')],
                    '1 表示指标抓取成功，0 表示抓取失败。', 1),
        'p1': chart('最近批次前缀缓存命中率（%）', [('(' + batch + ' >= 0 and ' + batch + ' <= 1) * 100', '最近批次')],
                    '原生 cache_hit_rate = 最近上报批次的复用 Token /（新增计算 Token + 复用 Token）。取 TP0/PP0；无新批次时可能保持上一值。它不是 CPU 独立命中率。', 100),
        'p2': chart('Prefill 计算与缓存复用（Token/秒）', [(compute, '新增计算'), (cached, '缓存复用')],
                    'realtime_tokens_total 的 prefill_compute、prefill_cache 一分钟速率，取 TP0/PP0 的调度器批次计数；不与其他 rank 相加，也不作为唯一请求 Token 总量。窗口内计数重置留空。'),
        'cache-hit-window': chart('一分钟前缀缓存命中率（%）', [
            ('100 * (' + cached + ') / ignoring(mode) (((' + compute + ') + ignoring(mode) (' + cached + ')) > 0)', '一分钟加权命中率')],
            '一分钟内缓存复用 Token 速率 /（新增计算 + 缓存复用 Token 速率），按 Token 加权，非批次命中率的算术平均。无工作量或任一来源缺失时留空。', 100),
        'prefetch-idle-ticks': chart('HiCache 预取等待空转频率（次/秒）', [(idle_ticks, 'TP{{tp_rank}} / PP{{pp_rank}}')],
            'waiting_queue 中有请求等待 HiCache L3 预取，当前无可运行批次且无在途 overlap 批次时，累加调度器空转 tick。显示每个 rank 的一分钟速率，不是请求数或预取操作数，不跨 rank 相加；当前零值表示此条件未发生。'),
        'prefetch-idle-time': chart('HiCache 预取等待空转时间占比（%）', [('100 * ' + idle_time, 'TP{{tp_rank}} / PP{{pp_rank}}')],
            '上述空转状态累计墙钟秒数的一分钟速率 × 100，按 rank 分线；不是 XPU 硬件整体空闲率或缓存 I/O 耗时，不跨 rank 相加。窗口内计数重置留空。')}
    groups = [('缓存复用', ['p1', 'cache-hit-window', 'p2', 'p0']),
              ('HiCache 预取等待', ['prefetch-idle-ticks', 'prefetch-idle-time'])]
    d['spec']['layouts'] = [{'kind': 'Grid', 'spec': {'display': {'title': title}, 'items': [
        {'x': 12 * (i % 2), 'y': 8 * (i // 2), 'width': 24 if len(keys) == 1 else 12, 'height': 8,
         'content': {'$ref': '#/spec/panels/' + key}} for i, key in enumerate(keys)]}} for title, keys in groups]
    return d


if __name__ == '__main__':
    p = Path(__file__).parent / 'projects/xpu-monitoring/dashboards/cache-store.json'
    p.write_text(json.dumps(configure(json.loads(p.read_text())), ensure_ascii=False, indent=2) + '\n')
