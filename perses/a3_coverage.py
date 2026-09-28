"""Append A3 collection scope without replacing live queries or layouts."""
import copy

MARKER = '\n\nA3 引擎采集范围\n'
NOTICE = ('Prefill：4 个引擎（7100–7103）；Decode：16 个引擎（7100–7115）。'
          'Decode 为 8 张物理卡、每卡 2 个芯片设备，DP=16 / TP=1。'
          '2026-09-28 修复前仅采集 Decode 前 4 个端点；已确认 DP=16 漏采期间的历史保留，'
          '但不代表全量，不能乘倍数补算或直接作为完整性能基线。更早拓扑未经核实，不推定漏采。'
          '精确修复时间见监控 API 的 collection_coverage.repair_complete_at。')


def annotate(document, repaired_at=None):
    result = copy.deepcopy(document)
    if result['metadata'].get('project') != 'a3-monitoring':
        return result
    if result['metadata']['name'] not in ('backend-performance', 'backend-prefill', 'backend-decode', 'a3-cache', 'monitoring-health'):
        return result
    displays = [result['spec'].setdefault('display', {})]
    displays += [p['spec'].setdefault('display', {}) for p in result['spec'].get('panels', {}).values()]
    for display in displays:
        old = display.get('description', '')
        # Keep a previously published exact boundary when regenerating resources.
        suffix = old.split(MARKER, 1)[1] if MARKER in old and repaired_at is None else NOTICE
        if repaired_at is not None:
            suffix += ' 本次完整采集确认时间：' + repaired_at + '。'
        display['description'] = old.split(MARKER, 1)[0] + MARKER + suffix
    return result
