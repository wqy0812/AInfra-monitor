"""Selected native A3 Mooncake panels; retain the full collection catalog."""
import copy
from generate import panel
from project_queries import Queries
from dashboard_reorg import present

OPERATIONS = {
    '读写': ['exist_key', 'get_replica_list', 'get_replica_list_by_regex', 'put_start', 'put_end', 'put_revoke', 'batch_exist_key', 'batch_get_replica_list', 'batch_put_start', 'batch_put_end', 'batch_put_revoke', 'batch_query_ip'],
    '删除与磁盘副本': ['remove', 'remove_by_regex', 'remove_all', 'evict_disk_replica', 'batch_replica_clear'],
    'Segment 管理': ['mount_segment', 'unmount_segment', 'remount_segment', 'ping'],
    '复制迁移': ['copy_start', 'copy_end', 'copy_revoke', 'move_start', 'move_end', 'move_revoke'],
    '任务管理': ['create_copy_task', 'create_move_task', 'update_task', 'query_task', 'fetch_tasks'],
}
GAUGES = ['master_allocated_bytes', 'master_total_capacity_bytes', 'segment_allocated_bytes', 'segment_total_capacity_bytes', 'master_allocated_file_size_bytes', 'master_total_file_capacity_bytes', 'master_key_count', 'master_soft_pin_key_count', 'master_active_clients', 'master_put_start_discarded_staging_size', 'ha_oplog_last_sequence_id', 'ha_oplog_applied_sequence_id', 'ha_oplog_standby_lag', 'ha_oplog_pending_entries', 'ha_pending_mutation_queue_size', 'ha_standby_state']
COUNTERS = ['master_attempted_evictions_total', 'master_successful_evictions_total', 'master_evicted_key_count', 'master_evicted_size_bytes', 'master_put_start_alloc_failures_total', 'master_put_start_discard_cnt', 'master_put_start_release_cnt']
COUNTERS += ['master_' + op + '_' + suffix + '_total' for ops in OPERATIONS.values() for op in ops for suffix in ('requests', 'failures')]
BOUNDS = ['4096.000000', '65536.000000', '262144.000000', '1048576.000000', '4194304.000000', '16777216.000000', '67108864.000000', '+Inf']
METRICS = GAUGES + COUNTERS + ['master_value_size_bytes_' + suffix for suffix in ('bucket', 'count', 'sum')]
NOTE = '仅 A3 Mooncake Master；有效零值保留，缺采、过期、计数重置留空。Store 分层命中率和 SSD 实际读写未暴露。'


def configure(document):
    if (document['metadata'].get('project'), document['metadata']['name']) != ('a3-monitoring', 'a3-cache'):
        return document
    d = copy.deepcopy(document)
    # Replace only this catalog; retain all engine/user panels and their layout.
    d['spec']['panels'] = {k: v for k, v in d['spec']['panels'].items() if not k.startswith('mooncake-')}
    layouts = []
    for layout in d['spec']['layouts']:
        layout['spec']['items'] = [i for i in layout['spec']['items'] if not i['content']['$ref'].split('/')[-1].startswith('mooncake-')]
        if layout['spec']['items']:
            layouts.append(layout)
    d['spec']['layouts'] = layouts
    q = Queries('a3-vllm', 'mooncake-a3')
    groups = {}

    def add(group, key, title, unit, queries, desc=''):
        key = 'mooncake-' + key
        p = panel(title, queries, unit, desc + '\n\n' + NOTE)
        present(p, 'a3-cache')
        d['spec']['panels'][key] = p
        groups.setdefault(group, []).append(key)

    for key, name, used, total in [('capacity', '内存', 'master_allocated_bytes', 'master_total_capacity_bytes'), ('file-capacity', 'SSD 缓存', 'master_allocated_file_size_bytes', 'master_total_file_capacity_bytes')]:
        desc = '后端登记的有效对象量与配额，不等于物理文件占用或 SSD I/O。' if key == 'file-capacity' else 'Master 管理的 segment 内存，不是 NPU 显存。'
        add('Mooncake 容量与对象', key, 'Mooncake ' + name + '容量', 'GiB', [(q.gauge(m) + ' / 1024^3', label) for m, label in [(used, '有效对象占用'), (total, '总配额')]], desc)
    add('Mooncake 容量与对象', 'segments', 'Segment 内存容量', 'GiB', [(q.gauge(m) + ' / 1024^3', '{{segment}} ' + label) for m, label in [('segment_allocated_bytes', '已分配'), ('segment_total_capacity_bytes', '总容量')]], '逐 segment 展示，未挂载 segment 的有效零值保留。')
    add('Mooncake 容量与对象', 'keys', 'Mooncake 对象数', '个', [(q.gauge(m), label) for m, label in [('master_key_count', 'Key'), ('master_soft_pin_key_count', '软固定 Key')]])
    hist = 'master_value_size_bytes'
    add('Mooncake 容量与对象', 'object-average', '对象平均大小', 'MiB', [('(' + q.rate(hist + '_sum') + ') / ((' + q.rate(hist + '_count') + ') > 0) / 1024^2', '平均值')])
    for key, title, unit, metrics in [
        ('evictions', '驱逐操作', '次/秒', [('master_attempted_evictions_total', '尝试'), ('master_successful_evictions_total', '成功')]),
        ('evicted-keys', '驱逐对象', '个/秒', [('master_evicted_key_count', 'Key')]),
        ('evicted-bytes', '驱逐数据', '字节/秒', [('master_evicted_size_bytes', '字节')]),
        ('put-cleanup', '写入分配与清理', '次/秒', [('master_put_start_alloc_failures_total', '分配失败'), ('master_put_start_discard_cnt', '丢弃'), ('master_put_start_release_cnt', '释放')]),
    ]:
        add('Mooncake 驱逐与写入清理', key, title + '速率', unit, [(q.rate(m), label) for m, label in metrics], '对象操作计数，不代表物理 SSD 吞吐。')
    add('Mooncake 驱逐与写入清理', 'staging', '待释放暂存内存', 'MiB', [(q.gauge('master_put_start_discarded_staging_size') + ' / 1024^2', '已丢弃未释放')])
    for title, keys in groups.items():
        d['spec']['layouts'].append({'kind': 'Grid', 'spec': {'display': {'title': title}, 'items': [
            {'x': i % 2 * 12, 'y': i // 2 * 8, 'width': 12, 'height': 8, 'content': {'$ref': '#/spec/panels/' + key}} for i, key in enumerate(keys)]}})
    return d
