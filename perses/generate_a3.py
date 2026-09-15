"""Add A3 dashboards without regenerating or overwriting any DCU dashboard."""
from generate import dashboard, panel, variable, derived_schema
import json

ROLE = variable('role', '角色', [{'value':'.*','label':'全部'}, {'value':'prefill','label':'Prefill / A3-1'}, {'value':'decode','label':'Decode / A3-2'}])
NODE = variable('node', '节点', [{'value':'.*','label':'全部'}, {'value':'a3-1','label':'A3-1'}, {'value':'a3-2','label':'A3-2'}])
ENV = 'environment="a3-vllm"'


def derived(path, scale=1):
    s = '{' + ENV + ',schema=' + json.dumps(derived_schema(path)) + ',path=~' + json.dumps(path) + '}'
    value, valid = 'monitoring_chart_value' + s, 'monitoring_chart_valid' + s
    return f'({value} and ({valid} == 1) and (min_over_time({valid}[$__interval]) == 1) and (count_over_time({valid}[$__interval]) >= ($__interval / 5)) and (time() - timestamp({value}) < 15)) * {scale}'


def fresh(expr, job):
    up = 'up{' + ENV + ',job="' + job + '"}'
    return f'({expr}) and on(job,instance) ((min_over_time({up}[$__interval]) == 1) and (count_over_time({up}[$__interval]) >= ($__interval / 5)) and (time() - timestamp({up}) < 15))'


def host_metric(name, extra=''):
    return name + '{' + ENV + ',job="node-a3",node=~"$node"' + extra + '}'


def host_rate(name, extra=''):
    m = host_metric(name, extra)
    boot = host_metric('node_boot_time_seconds')
    up = 'up{' + ENV + ',job="node-a3",node=~"$node"}'
    return f'(rate({m}[1m]) and (resets({m}[1m]) == 0) and (count_over_time({m}[1m]) >= 12) and (time() - timestamp({m}) < 15)) and on(job,instance) ((changes({boot}[1m]) == 0) and (count_over_time({boot}[1m]) >= 12) and (min_over_time({up}[1m]) == 1) and (count_over_time({up}[1m]) >= 12))'


if __name__ == '__main__':
    p = [panel('A3 采集目标状态', [('up{'+ENV+'} and (time() - timestamp(up{'+ENV+'}) < 15)', '{{node}} {{instance}}')], '1 = 正常')]
    for path, title, unit in [('requests','完成请求速率','req/s'), ('decode_tokens','Token 生成速率','token/s')]:
        p.append(panel(title, [(derived('nodes.$role.'+path), '{{path}}')], unit, '每侧 4 个实例，逐实例按实际抓取间隔计算。完成量包含各结束原因，不代表成功量。'))
    for metric, title, scale in [('ttft','首 Token 时延 TTFT',1000), ('itl','Token 间隔 ITL',1000), ('e2e','请求总时延 E2E',1)]:
        p.append(panel(title, [(derived('nodes.$role.percentiles.'+metric+'.'+q,scale),'{{path}}') for q in ['p50','p95','p99']], 's' if metric=='e2e' else 'ms', '最近约 60 秒，先合并 4 个实例的直方图增量再计算分位数；服务侧口径，无样本留空。'))
    p.append(panel('运行与排队请求', [(derived('nodes.$role.resources.queue..*'),'{{path}}')], '请求'))
    dashboard('a3-overview','A3 · 运行概览',p,[ROLE])

    cpu = '100 * (1 - sum by(job,instance,node) (' + host_rate('node_cpu_seconds_total',',mode=~"idle|iowait"') + ') / sum by(job,instance,node) (' + host_rate('node_cpu_seconds_total',',mode!~"guest|guest_nice"') + '))'
    p = [panel('主机 CPU',[(fresh(cpu,'node-a3'),'{{node}}')],'%')]
    total, available = host_metric('node_memory_MemTotal_bytes'),host_metric('node_memory_MemAvailable_bytes')
    p.append(panel('主机内存',[(fresh(f'({total} - {available}) / 1024^3','node-a3'),'{{node}} 已用'),(fresh(total+' / 1024^3','node-a3'),'{{node}} 总量')],'GiB'))
    fs = ',fstype!~"tmpfs|devtmpfs|overlay|squashfs"'
    capacity, available = host_metric('node_filesystem_size_bytes',fs),host_metric('node_filesystem_avail_bytes',fs)
    p.append(panel('文件系统容量使用率',[(fresh(f'100 * (1 - {available} / {capacity}) and ({capacity} > 0)','node-a3'),'{{node}} {{mountpoint}}')],'%'))
    for title, names in [('磁盘 I/O',[('node_disk_read_bytes_total','读'),('node_disk_written_bytes_total','写')]),('网络吞吐',[('node_network_receive_bytes_total','接收'),('node_network_transmit_bytes_total','发送')])]:
        p.append(panel(title,[(fresh('('+host_rate(name,',device!~"lo|loop.*|ram.*|veth.*|docker.*|br-.*"')+') / 1024^2','node-a3'),'{{node}} {{device}} '+label) for name,label in names],'MiB/s','逐设备展示，不合计逻辑盘和物理盘；采集缺失、重置及启动边界留空。'))
    dashboard('a3-hosts','A3 · 主机',p,[NODE])

    p = [panel('实例 KV 缓存占用',[(derived('nodes.$role.resources.kv_usage..*',100),'{{path}}')],'%','vLLM KV-cache usage；不是 NPU 显存总使用率。')]
    for path,title,desc in [('ratio','本地前缀缓存命中率','本地前缀命中 Token / 查询 Token。'),('external_ratio','外部前缀缓存命中率','跨实例 KV 共享命中 Token / 查询 Token；不表示 HiCache CPU、Mooncake Store 或 SSD 命中。')]:
        p.append(panel(title,[(derived('nodes.$role.cache_60s.'+path,100),'{{path}}')],'%','最近约 60 秒；按 Token 加权，无查询留空。'+desc))
    dashboard('a3-cache','A3 · 缓存',p,[ROLE])
    print('Generated 3 A3 dashboards; DCU resources unchanged')
