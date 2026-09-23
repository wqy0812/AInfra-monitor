"""Generate DCU/A3 project resources from a live snapshot or current resources."""
import argparse
import collections
import copy
import json
import re
from pathlib import Path

from generate import panel, variable, derived_schema
from project_queries import Queries, ratio
from metric_scope import apply_scope, request_description

ROOT = Path(__file__).resolve().parent
PROJECTS = {"dcu-monitoring": "dcu-pd", "a3-monitoring": "a3-vllm", "xpu-monitoring": "xpu-pd"}
REQUEST_LABEL = re.compile(r'(?:^|,)\s*(?:stream|is_streaming|request_scope)\s*(?:=~|!~|!=|=)\s*"(?:\\.|[^"\\])*"\s*(?=,|$)')
BOUNDS = {}


def current_request_schema(query):
    """Move only request-derived selectors; preserve resource and raw queries."""
    def replace(match):
        labels = match[2]
        path = re.search(r'\bpath\s*=~?\s*("(?:\\.|[^"\\])*")', labels)
        if path and derived_schema(json.loads(path[1])) == "request-metrics-v2":
            labels = re.sub(r'\bschema="[^"]*"', 'schema="request-metrics-v2"', labels)
        return match[1] + "{" + labels + "}"
    return re.sub(r'(monitoring_chart_\w+)\{([^}]*)\}', replace, query)


def no_request_filter(query):
    # Legacy public name retained for release tooling; now canonicalizes per metric.
    return apply_scope(query)


def scoped(query, environment):
    query = no_request_filter(query)
    def replace(match):
        body = match.group(1)
        if re.search(r'\benvironment\s*[=!~]', body):
            body = re.sub(r'\benvironment\s*(?:=~|!~|!=|=)\s*"[^"]*"', 'environment=' + json.dumps(environment), body)
        else:
            body += ("," if body else "") + 'environment=' + json.dumps(environment)
        return "{" + body + "}"
    return re.sub(r"\{([^{}]*)\}", replace, query)


def read_resources(root):
    result = {"projects": [], "datasources": [], "dashboards": []}
    for p in sorted(root.rglob("*.json")):
        obj = json.loads(p.read_text())
        kind = {"Project": "projects", "Datasource": "datasources", "Dashboard": "dashboards"}.get(obj.get("kind"))
        if kind:
            result[kind].append(obj)
    return result


def clean_resource(document, project):
    d = copy.deepcopy(document)
    d["metadata"] = {"name": d["metadata"]["name"], **({"project": project} if d["kind"] != "Project" else {})}
    return d


def normalize_dashboard(document, project):
    d = clean_resource(document, project)
    env = PROJECTS[project]
    gateway = d["metadata"]["name"].startswith("gateway")
    for p in d["spec"]["panels"].values():
        for q in p["spec"].get("queries", []):
            s = q["spec"]["plugin"]["spec"]
            s["query"] = current_request_schema(scoped(s["query"], env)).replace('读取流式请求的错误响应', '读取完整响应')
            if 'seriesNameFormat' in s:
                s['seriesNameFormat'] = s['seriesNameFormat'].replace('读取流式请求的错误响应', '读取完整响应')
        display = p["spec"]["display"]
        desc = request_description(display.get("description", ""))
        desc = desc.replace('仅流式请求；每个网关', '流式及非流式请求；每个网关').replace('读取流式请求的错误响应', '读取完整响应')
        desc = desc.replace('仅此项统计非流式，不进入画像、延迟、错误率或在途阶段。', '非流式同时计入画像、总耗时、错误率和在途状态。')
        desc = desc.replace('当前 aigate 画像源仅记录流式请求。移除面板筛选不改变源程序的记录范围。Token 和耗时在请求结束时计入。', '画像包含流式及非流式。网关首增量只统计有效流式输出；Token 和总耗时在请求结束时计入。')
        for old in ("仅统计流式生成请求。", "仅统计流式请求（request_scope=streaming）。",
                    "仅统计 stream=true。", "非流式不进入其他 17 项。"):
            desc = desc.replace(old, "")
        if gateway:
            desc = re.sub(r"蓝色为 DCU 主机网关，橙色为 A3 主机网关；阶段图的颜色区分处理阶段。", "", desc)
            desc = desc.replace("当前查询显示 DCU 主机网关，且只统计流式请求画像；它不是 DCU/A3 两网关的合计，也不等于下游硬件归属。", "")
            desc = re.sub(r"\n\n项目与源口径\n.*$", "", desc, flags=re.S)
            owner = {"dcu-pd": "DCU", "a3-vllm": "A3", "xpu-pd": "XPU"}[env]
            desc = re.sub(r"曲线与范围\n.*?(?=\n\n时间口径)",
                          "曲线与范围\n" + owner + " 主机网关。按采集环境归属；图例区分本环境的后端或阶段。",
                          desc, flags=re.S)
        if any('schema="request-metrics-v2"' in q['spec']['plugin']['spec']['query']
               for q in p['spec'].get('queries', [])):
            desc = re.sub(r'\n\n请求派生口径\n.*$', '', desc, flags=re.S)
            desc += ('\n\n请求派生口径\n后端指标使用原生统计，不强制区分流式。正常业务以流式为主；精度测试期间可能混入非流式并影响延迟分布。DCU ITL 按输出批次平均。新口径上线前的历史留空，不做迁移。')
        from dcu_bottlenecks import compact_description
        display["description"] = compact_description(desc)
    return d


def split_gateway(document, project):
    d = copy.deepcopy(document)
    env = PROJECTS[project]
    for key, p in list(d["spec"]["panels"].items()):
        kept = []
        indexes = {}
        for old, q in enumerate(p["spec"]["queries"]):
            expr = q["spec"]["plugin"]["spec"]["query"]
            environments = set(re.findall(r'environment="([^"]+)"', expr))
            if environments == {env}:
                indexes[old] = len(kept)
                kept.append(q)
        if not kept:
            del d["spec"]["panels"][key]
            continue
        p["spec"]["queries"] = kept
        chart = p["spec"]["plugin"]["spec"]
        if "querySettings" in chart:
            chart["querySettings"] = [
                dict(s, queryIndex=indexes[s["queryIndex"]])
                for s in chart["querySettings"] if s["queryIndex"] in indexes
            ]
    layout = d["spec"]["layouts"][0]["spec"]["items"]
    order = [x["content"]["$ref"].split("/")[-1] for x in sorted(layout, key=lambda x: (x["y"], x["x"]))
             if x["content"]["$ref"].split("/")[-1] in d["spec"]["panels"]]
    d["spec"]["layouts"][0]["spec"]["items"] = grid(order)
    d["spec"]["display"]["description"] = ("DCU" if env == "dcu-pd" else "A3") + " 主机网关。按采集环境归属；按指标采样含义确定请求范围。"
    return normalize_dashboard(d, project)


def grid(keys, offset=0):
    return [{"x": (i % 2) * 12, "y": offset + (i // 2) * 8, "width": 12, "height": 8,
             "content": {"$ref": "#/spec/panels/" + k}} for i, k in enumerate(keys)]


def blank_dashboard(name, title, project, variables=None):
    return {"kind": "Dashboard", "metadata": {"name": name, "project": project}, "spec": {
        "display": {"name": title, "description": "按指标采样含义确定请求范围。数据源：test4 VictoriaMetrics。"},
        "duration": "1h", "refreshInterval": "15s", "variables": variables or [], "panels": {},
        "layouts": [{"kind": "Grid", "spec": {"items": []}}]}}


def add(d, key, title, qs, unit, meaning):
    env = PROJECTS[d["metadata"]["project"]]
    shared = d["metadata"]["name"] == "monitoring-health"
    scope = "test4，共享于 DCU / A3；不表示某一个推理集群的资源。" if shared else ("DCU" if env == "dcu-pd" else "A3") + " 环境；实例、节点或 rank 分线展示，复制数据不直接相加。"
    desc = (
        "指标含义\n" + meaning + "\n\nY 轴单位\n" + unit +
        "\n\n曲线与范围\n" + scope
    )
    if d["metadata"]["name"] == "gateway-requests":
        desc += "\n\n源口径\n画像包含流式及非流式。网关首增量仅包含观测到有效增量的流式请求；Token 和总耗时在请求结束时计入。"
    d["spec"]["panels"][key] = panel(title, qs, unit, desc)
    items = d["spec"]["layouts"][0]["spec"]["items"]
    ref = "#/spec/panels/" + key
    if not any(x["content"]["$ref"] == ref for x in items):
        owned = [x for x in items if x["content"]["$ref"].split("/")[-1].startswith("extra-")]
        base = [x for x in items if x not in owned]
        start = max((x["y"] + x["height"] for x in base), default=0)
        keys = [x["content"]["$ref"].split("/")[-1] for x in owned] + [key]
        d["spec"]["layouts"][0]["spec"]["items"] = base + grid(keys, start)


def derived(env, path, scale=1, schema="v1"):
    if derived_schema(path) == "request-metrics-v2":
        schema = "request-metrics-v2"
    labels = '{environment=' + json.dumps(env) + ',schema=' + json.dumps(schema) + ',path=~' + json.dumps(path) + '}'
    value, valid = "monitoring_chart_value" + labels, "monitoring_chart_valid" + labels
    return f'({value} and ({valid} == 1) and (min_over_time({valid}[$__interval]) == 1) and (count_over_time({valid}[$__interval]) >= ($__interval / 5)) and (time() - timestamp({value}) < 15)) * {scale}'


def quantiles(q, metric, group, legend, origin=None):
    bounds = BOUNDS[q.environment][metric]
    return [(q.histogram_quantiles(metric, bounds, group, origin), legend + " {{perses_quantile}}")]


def gateway_requests(project):
    env = PROJECTS[project]
    d = blank_dashboard("gateway-requests", "请求吞吐与画像", project)
    q = Queries(env, "aigate", origin="aigate_profile_counter_start_time_seconds")
    group = "environment,backend,model"
    legend = "后端 {{backend}} · {{model}}"
    add(d, "extra-routed", "已路由请求速率", [(q.rate("aigate_requests_routed_total", group=group), legend)], "请求 / 秒", "进入所选后端/模型的请求速率；与入口到达、生成结束处于不同阶段。")
    add(d, "extra-ended", "请求结束结果", [(q.rate("aigate_requests_ended_total", group=group + ",outcome"), legend + " · {{outcome}}")], "请求 / 秒", "画像记录的全部结束结果，按 outcome 分线；完成、拒绝、取消分别展示。")
    known = {}
    for field in ("prompt_tokens", "completion_tokens"):
        known[field] = q.rate("aigate_usage_known_total", ',field="' + field + '"', group=group)
    add(d, "extra-tokens", "输入与输出 Token 吞吐", [
        (f'({q.rate("aigate_" + f + "_total", group=group)}) and on({group}) (({known[f]}) > 0)', legend + " · " + label)
        for f, label in (("prompt_tokens", "输入"), ("completion_tokens", "输出"))], "Token / 秒", "已知 usage 的 Token 数增长速率；usage 完全缺失时不把源初始化零值解释为零吞吐。")
    for key, metric, title, unit in [
        ("first", "first_increment_seconds", "首个有效输出耗时", "秒"),
        ("duration", "request_duration_seconds", "网关请求总耗时", "秒"),
        ("input", "prompt_tokens", "输入 Token 长度", "Token"),
        ("output", "completion_tokens", "输出 Token 长度", "Token"),
    ]:
        qs = quantiles(q, "aigate_" + metric, group, legend, "aigate_profile_group_start_time_seconds")
        add(d, "extra-" + key, title, qs, unit, "先检查每个来源的计数、桶完整性和生命周期，再合并直方图计算分位数。首个有效输出是网关观测点；请求总耗时不是各阶段分位数之和。")
    cache = ratio(q.rate("aigate_cached_tokens_total", group=group), q.rate("aigate_cache_prompt_tokens_total", group=group), group)
    cache += f' and on({group}) (({q.rate("aigate_cache_usage_known_total", group=group)}) > 0)'
    add(d, "extra-cache", "网关缓存 Token 比例", [(cache, legend)], "%", "已知缓存 usage 中，缓存命中 Token 增量除以对应输入 Token 增量。不是请求命中率，也不是 SSD 命中率。")
    missing = q.rate("aigate_usage_missing_total", group=group + ",field")
    good = q.rate("aigate_usage_known_total", group=group + ",field")
    add(d, "extra-usage", "Usage 缺失比例", [(ratio(missing, f'({missing}) + ({good})', group + ",field"), legend + " · {{field}}")], "%", "按输入/输出字段分别计算缺失次数 /（已知次数 + 缺失次数）；没有结束样本时留空。")
    return d


def backend(project):
    env = PROJECTS[project]
    d = blank_dashboard("backend-diagnostics", "后端诊断", project)
    if env == "dcu-pd":
        q = Queries(env, "sglang-prefill")
        # Keep scheduler/rank identity; no sum across TP/DP/CP duplicates.
        for role in ("prefill", "decode"):
            q = Queries(env, "sglang-" + role)
            legend = "{{node}} · DP {{dp_rank}} / TP {{tp_rank}} / EP {{moe_ep_rank}}"
            add(d, "extra-queue-" + role, role.capitalize() + " 运行与排队请求",
                [(q.gauge("sglang:" + m), legend + " · " + label) for m, label in (("num_running_reqs", "运行"), ("num_queue_reqs", "排队"))],
                "请求", "调度器当前运行与排队请求，按原始 rank 分线，未对复制 rank 求总和。")
            add(d, "extra-kv-" + role, role.capitalize() + " KV Token 占用", [(q.gauge("sglang:token_usage") + " * 100", legend)], "%", "源调度器 token_usage，反映 KV Token 池占用，不是整卡显存占用。")
        add(d, "extra-latency-samples", "时延窗口样本数", [(derived(env, "nodes.(prefill|decode).percentiles." + m + ".samples", schema="latency-v2"), "{{path}}") for m in ("ttft", "itl", "e2e")], "样本", "当前有效窗口中的直方图样本数，帮助识别少样本分位数。")
    else:
        q = Queries(env, "vllm-a3")
        legend = "{{node}} · engine {{engine}}"
        add(d, "extra-wait-reason", "等待请求原因", [(q.gauge("vllm:num_requests_waiting_by_reason"), legend + " · {{reason}}")], "请求", "capacity 为等待调度容量；deferred 为 KV 传输等临时约束。来源是后端完整请求集合。")
        add(d, "extra-preemptions", "抢占速率", [(q.rate("vllm:num_preemptions_total"), legend)], "次 / 秒", "引擎发生抢占的计数速率，用于判断调度/KV 容量压力。")
        add(d, "extra-sleep", "引擎运行状态", [(q.gauge("vllm:engine_sleep_state"), legend + " · {{sleep_state}}")], "状态 0 / 1", "awake=1 表示唤醒；weights_offloaded/discard_all 对应睡眠级别状态，按源标签分别展示。")
        for metric, title in [
            ("request_queue_time_seconds", "排队时延"), ("request_prefill_time_seconds", "Prefill 时延"),
            ("request_decode_time_seconds", "Decode 时延"), ("request_inference_time_seconds", "Inference 时延"),
            ("request_time_per_output_token_seconds", "每请求 TPOT"),
        ]:
            add(d, "extra-" + metric, title, quantiles(q, "vllm:" + metric, "environment,node,instance,engine", legend, "vllm:" + metric + "_created"), "秒",
                "后端原生请求直方图，包含源记录的全部请求。TPOT 是每请求输出 Token 平均耗时，与 ITL 分布含义不同。各阶段分位数不可直接相加。")
    return d


def host_additions(d):
    env = PROJECTS[d["metadata"]["project"]]
    if env == "a3-vllm":
        q = Queries(env, "node-a3", ',node=~"$node"', origin="node_boot_time_seconds")
    else:
        q = Queries(env, "node-prefill", ',node=~"$node"', origin="node_boot_time_seconds")
        q.labels = q.labels.replace('job="node-prefill"', 'job=~"node-prefill|node-decode"')
    legend = "{{node}}"
    add(d, "extra-load", "主机负载", [(q.gauge("node_load" + n), legend + " · " + n + "m") for n in ("1", "5", "15")], "任务数", "1/5/15 分钟系统负载，包含可运行及不可中断等待任务；不是百分比。")
    iowait = q.rate("node_cpu_seconds_total", ',mode="iowait"')
    add(d, "extra-iowait", "CPU I/O 等待", [(f'100 * avg by(environment,node) ({iowait})', legend)], "%", "CPU 处于 iowait 状态的平均比例；结合磁盘延迟判断存储等待。")
    total, available = q.gauge("node_memory_MemTotal_bytes"), q.gauge("node_memory_MemAvailable_bytes")
    add(d, "extra-memory-ratio", "内存占用比例", [(f'100 * (1 - ({available}) / ({total}))', legend)], "%", "1 − MemAvailable/MemTotal，含可回收内存的可用量估算。")
    total, free = q.gauge("node_memory_SwapTotal_bytes"), q.gauge("node_memory_SwapFree_bytes")
    add(d, "extra-swap", "Swap 已用与总量", [(f'(({total}) - ({free})) / 1024^3', legend + " · 已用"), (f'({total}) / 1024^3', legend + " · 总量")], "GiB", "Swap 总量为零表示没有配置 Swap；绝对容量显示有效零。")
    fs = ',fstype!~"tmpfs|devtmpfs|overlay|squashfs"'
    add(d, "extra-fs-free", "文件系统剩余容量", [(q.gauge("node_filesystem_avail_bytes", fs) + " / 1024^3", "{{node}} · {{mountpoint}}")], "GiB", "普通用户可用空间，按挂载点分线，不对重复挂载累计。")
    if env == "dcu-pd":
        gpu = Queries(env, "dcu-prefill", ',node=~"$node",device=~"$device"')
        gpu.labels = gpu.labels.replace('job="dcu-prefill"', 'job=~"dcu-prefill|dcu-decode"')
        # up is a target metric and has no device label.
        gpu.labels = gpu.labels.replace(',device=~"$device"', '')
        used, total = gpu.gauge("dcu_memory_used_bytes", ',device=~"$device"'), gpu.gauge("dcu_memory_total_bytes", ',device=~"$device"')
        sample = Queries(env, "dcu-prefill")
        sample.labels = sample.labels.replace('job="dcu-prefill"', 'job=~"dcu-prefill|dcu-decode"')
        gate = f' and on(job,instance) (({sample.s("dcu_sample_success")} == 1) and (time() - {sample.s("dcu_sample_timestamp_seconds")} < 15))'
        add(d, "extra-vram-total", "DCU 显存总量", [(f'({total}) / 1024^3' + gate, "{{node}} · {{device}}")], "GiB", "每张设备实际显存容量，配合已有已用显存曲线判断余量。")
        add(d, "extra-vram-ratio", "DCU 显存占用比例", [(f'100 * ({used}) / (({total}) > 0)' + gate, "{{node}} · {{device}}")], "%", "每卡已用显存 / 总显存，要求 DCU 采样成功且新鲜。")
    else:
        dev = ',device!~"lo|loop.*|ram.*|veth.*|docker.*|br-.*"'
        add(d, "extra-iops", "磁盘 IOPS", [(q.rate("node_disk_" + m + "_completed_total", dev), "{{node}} · {{device}} · " + label) for m, label in (("reads", "读"), ("writes", "写"))], "次 / 秒", "每设备完成的读/写操作速率；不累加逻辑盘和底层物理盘。")
        qs = []
        for direction, op, label in (("read", "reads", "读"), ("write", "writes", "写")):
            seconds, count = q.rate("node_disk_" + direction + "_time_seconds_total", dev), q.rate("node_disk_" + op + "_completed_total", dev)
            qs.append((f'1000 * ({seconds}) / (({count}) > 0)', "{{node}} · {{device}} · " + label))
        add(d, "extra-disk-latency", "磁盘平均读写延迟", qs, "ms", "累计读写用时增量 / 完成操作数；无 I/O 时分母为零，留空。")
        add(d, "extra-disk-busy", "磁盘 I/O 忙碌率", [(q.rate("node_disk_io_time_seconds_total", dev) + " * 100", "{{node}} · {{device}}")], "%", "设备处于 I/O 活动状态的时间比例，不等同于带宽利用率。")
        add(d, "extra-net-errors", "网络错误与丢包", [(q.rate("node_network_" + m + "_total", dev), "{{node}} · {{device}} · " + label) for m, label in (("receive_errs", "接收错误"), ("transmit_errs", "发送错误"), ("receive_drop", "接收丢包"), ("transmit_drop", "发送丢包"))], "次 / 秒", "网卡接收/发送错误及丢包计数速率。")
        add(d, "extra-tcp-retrans", "TCP 重传速率", [(q.rate("node_netstat_Tcp_RetransSegs"), legend)], "段 / 秒", "主机 TCP 重传段速率，作为网络异常排查线索。")
        add(d, "extra-clock-offset", "系统时钟偏移", [(q.gauge("node_timex_offset_seconds") + " * 1000", legend)], "ms", "内核 timex 报告的时钟偏移。")
        d["spec"]["panels"]["extra-clock-offset"]["spec"]["plugin"]["spec"]["yAxis"].pop("min", None)
        add(d, "extra-clock-sync", "系统时钟同步状态", [(q.gauge("node_timex_sync_status"), legend)], "1 = 已同步", "内核 timex 同步状态；零表示未同步，采集缺失留空。")


def cache_additions(d):
    env = PROJECTS[d["metadata"]["project"]]
    if env == "dcu-pd":
        for key, path, title, unit in [
            ("hicache-ranks", "nodes.$role.resources.hicache_rank_ratio..*", "HiCache 各 rank 占用", "%"),
            ("segments", "mooncake.resources.segments..*", "Mooncake 各 segment 容量", "GiB"),
            ("segment-ratio", "mooncake.resources.segment_ratio..*", "Mooncake 各 segment 占用", "%"),
        ]:
            add(d, "extra-" + key, title, [(derived(env, path), "{{path}}")], unit, "使用现有有效性标记的派生值；逐 rank/segment 展示，不累加复制容量。")
        q = Queries(env, "mooncake")
        add(d, "extra-store-rate", "Store 查询与命中速率", [(q.rate(m), label) for m, label in (("total_get_nums_", "总查询"), ("valid_get_nums_", "有效查询"), ("mem_cache_hit_nums_", "内存命中"), ("file_cache_hit_nums_", "SSD 命中"))], "次 / 秒", "Store 查询及命中计数速率；不表示模型请求命中，也不证明物理 SSD I/O。")
    else:
        q = Queries(env, "vllm-a3")
        for prefix, title in (("prefix_cache_", "本地前缀缓存"), ("external_prefix_cache_", "外部前缀缓存")):
            add(d, "extra-" + prefix, title + "查询与命中", [(q.rate("vllm:" + prefix + suffix), "{{node}} · {{engine}} · " + label) for suffix, label in (("queries_total", "查询"), ("hits_total", "命中"))], "Token / 秒", "前缀缓存按 Token 统计的查询/命中速率；外部缓存指跨实例 KV 共享。")


def overview_additions(d):
    env = PROJECTS[d["metadata"]["project"]]
    s = '{environment="' + env + '"}'
    fresh = lambda metric: f'{metric}{s} and (time() - timestamp({metric}{s}) < 15)'
    add(d, "extra-scrape-duration", "指标采集耗时", [(fresh("scrape_duration_seconds"), "{{job}} · {{instance}}")], "秒", "每次抓取指标所耗时间；接近 scrape timeout 时需检查源端或网络。")
    add(d, "extra-sample-age", "采集样本年龄", [(f'time() - timestamp(up{s})', "{{job}} · {{instance}}")], "秒", "最近 up 观测距当前查询时刻的年龄。超出查询回看范围时源序列消失，留空。")
    add(d, "extra-derived-valid", "派生指标有效性", [(f'sum by(schema) (monitoring_chart_valid{s} and (time() - timestamp(monitoring_chart_valid{s}) < 15))', "{{schema}} · 有效字段"),
        (f'count by(schema) (monitoring_chart_valid{s} and (time() - timestamp(monitoring_chart_valid{s}) < 15))', "{{schema}} · 已观测字段")], "字段数", "按 schema 展示当前有效及已观测派生字段数。无请求造成部分字段无效属于正常口径，不是服务故障率。")


def shared_health(project):
    d = blank_dashboard("monitoring-health", "共享监控服务 · test4", project)
    d["spec"]["display"]["description"] = "test4，共享于 DCU / A3。两项目读取同一组公共服务指标；公共源现有 environment 标签为 dcu-pd。"
    q = Queries("dcu-pd", "victoriametrics", origin="process_start_time_seconds")
    q.labels = q.labels.replace('job="victoriametrics"', 'job=~"victoriametrics|vmagent"')
    add(d, "extra-up", "VM / vmagent 采集状态", [(q.s("up") + ' and (time() - timestamp(' + q.s("up") + ') < 15)', "{{job}}")], "1 = 正常", "公共服务监控端点的抓取状态，不代表推理服务可用。")
    add(d, "extra-cpu", "监控服务 CPU", [(q.rate("process_cpu_seconds_total"), "{{job}}")], "CPU 核", "进程 CPU 用时增长率，1 表示占用约一个逻辑 CPU 核。")
    add(d, "extra-memory", "监控服务常驻内存", [(q.gauge("process_resident_memory_bytes") + " / 1024^3", "{{job}}")], "GiB", "VM / vmagent 进程实际常驻内存。")
    vm = Queries("dcu-pd", "victoriametrics", origin="process_start_time_seconds")
    agent = Queries("dcu-pd", "vmagent", origin="process_start_time_seconds")
    add(d, "extra-data", "VM 数据容量", [(vm.gauge("vm_data_size_bytes") + " / 1024^3", "{{type}} · {{partition}}")], "GiB", "按 VM 原始数据类型及分区展示存储容量，保留标签避免隐藏不同组成。")
    add(d, "extra-disk", "VM 磁盘余量", [(vm.gauge(m) + " / 1024^3", label) for m, label in (("vm_free_disk_space_bytes", "空闲空间"), ("vm_free_disk_space_limit_bytes", "停止写入阈值"))], "GiB", "VM 所在文件系统空闲量及配置的最低余量阈值。")
    add(d, "extra-readonly", "VM 存储只读状态", [(vm.gauge("vm_storage_is_read_only"), "只读状态")], "1 = 只读", "1 表示 VM 存储当前处于只读状态，0 表示未处于该状态。")
    add(d, "extra-ingest", "VM 写入速率", [(vm.rate("vm_rows_inserted_total", group="environment,job,type"), "{{type}}")], "样本 / 秒", "VM 接收的指标样本写入计数速率，按输入类型汇总。")
    add(d, "extra-pending", "vmagent 发送积压", [(agent.gauge("vm_persistentqueue_bytes_pending") + " / 1024^2", "{{path}}")], "MiB", "持久发送队列等待传输的字节数，持续增长说明发送落后。")
    add(d, "extra-dropped", "vmagent 数据丢弃速率", [(agent.rate("vm_persistentqueue_bytes_dropped_total") + " / 1024^2", "{{path}}")], "MiB / 秒", "持久队列丢弃数据的字节速率；有效零表示窗口内未记录丢弃。")
    add(d, "extra-parse", "监控数据解析错误", [(q.rate("vm_protoparser_parse_errors_total"), "{{job}} · {{type}}")], "次 / 秒", "公共监控组件解析输入指标数据时记录的错误速率。")
    return d


def build(snapshot):
    from dashboard_reorg import migrate
    if any(d['metadata']['name'] == 'backend-performance' for d in snapshot['dashboards']):
        from align_dashboards import align
        return align(migrate(snapshot)[0])[0]
    by_key = {(d["metadata"]["project"], d["metadata"]["name"]): d for d in snapshot["dashboards"]}
    result = {"projects": [], "datasources": [], "dashboards": []}
    ds = next(x for x in snapshot["datasources"] if x["metadata"]["name"] == "victoriametrics")
    for project, env in PROJECTS.items():
        if project == "xpu-monitoring":
            # XPU has its own generator and integration policy. Preserve its
            # prepared resources rather than applying DCU/A3 additions to them.
            for kind, documents in snapshot.items():
                result[kind].extend(copy.deepcopy(d) for d in documents
                                    if d['metadata'].get('project', d['metadata']['name']) == project)
            continue
        owner = "DCU" if env == "dcu-pd" else "A3"
        result["projects"].append({"kind": "Project", "metadata": {"name": project}, "spec": {"display": {"name": owner + " 监控"}}})
        result["datasources"].append(clean_resource(ds, project))
        names = ("overview", "hosts-dcu", "cache-store") if env == "dcu-pd" else ("a3-overview", "a3-hosts", "a3-cache")
        for name in names + ("gateway", "gateway-generation"):
            source = by_key.get((project, name), by_key.get(("dcu-monitoring", name)))
            if source is None:
                raise ValueError("Missing seed dashboard " + name)
            d = split_gateway(source, project) if name == "gateway-generation" else normalize_dashboard(source, project)
            if name in ("overview", "a3-overview"):
                overview_additions(d)
            elif name in ("hosts-dcu", "a3-hosts"):
                host_additions(d)
                if name == "a3-hosts":
                    from npu_panels import extend
                    d = extend(d)
                    from host_cpu_panel import extend as materialized_cpu
                    d = materialized_cpu(d)
            elif name in ("cache-store", "a3-cache"):
                cache_additions(d)
            result["dashboards"].append(d)
        result["dashboards"] += [gateway_requests(project), backend(project), shared_health(project)]
    from remove_idle_thresholds import remove_panels
    from dcu_bottlenecks import default_configure
    result['dashboards'] = [default_configure(remove_panels(d)) for d in result['dashboards']]
    from align_dashboards import align
    return align(migrate(result)[0])[0]


def validate(resources):
    projects = [d['metadata']['name'] for d in resources['projects']]
    assert projects and len(projects) == len(set(projects)) and set(projects) <= PROJECTS.keys(), projects
    counts = collections.Counter()
    for d in resources["dashboards"]:
        project, name = d["metadata"]["project"], d["metadata"]["name"]
        assert project in projects, project
        counts[project] += 1
        assert name.startswith("a3-") is False or project == "a3-monitoring"
        env = "dcu-pd" if name == "monitoring-health" else PROJECTS[project]
        for key, p in d["spec"]["panels"].items():
            env = PROJECTS[project] if name == "monitoring-health" and key.startswith("overview-") else ("dcu-pd" if name == "monitoring-health" else PROJECTS[project])
            for item in p["spec"]["queries"]:
                query = item["spec"]["plugin"]["spec"]["query"]
                assert no_request_filter(query) == query, (project, name, key, "request filter")
                placeholder = (project == 'xpu-monitoring' and
                               (name == 'hosts-xpu' or (name == 'cache-store' and key in ('p8', 'p9'))) and
                               query == 'vector(0) unless on() vector(0)')
                assert placeholder or set(re.findall(r'environment="([^"]+)"', query)) == {env}, (project, name, key, "environment")
                assert current_request_schema(query) == query, (project, name, key, "outdated request schema")
            for settings in p["spec"]["plugin"]["spec"].get("querySettings", []):
                assert 0 <= settings["queryIndex"] < len(p["spec"]["queries"])
        refs = [x["content"]["$ref"].split("/")[-1] for layout in d["spec"]["layouts"] for x in layout["spec"]["items"]]
        assert len(refs) == len(set(refs)) and set(refs) == set(d["spec"]["panels"])
        from dashboard_reorg import RETIRED
        assert name not in RETIRED[project], (project, name, 'retired dashboard')
        variables = {v['spec']['name'] for v in d['spec'].get('variables', [])}
        for p in d['spec']['panels'].values():
            assert not p['spec']['plugin']['spec'].get('yAxis', {}).get('label'), (name, 'axis label')
            desc = p['spec']['display'].get('description', '')
            assert 'Y 轴单位' not in desc and '曲线与范围' not in desc
            for q in p['spec'].get('queries', []):
                used = set(re.findall(r'\$([A-Za-z_]\w*)', q['spec']['plugin']['spec']['query'])) - {'__interval'}
                assert used <= variables, (project, name, used - variables)
    for project in projects:
        names = {d['metadata']['name'] for d in resources['dashboards'] if d['metadata']['project'] == project}
        required = {'backend-performance', 'accelerator-resources', 'gateway', 'gateway-requests', 'gateway-generation', 'monitoring-health'}
        required |= {'backend-prefill', 'backend-decode', 'a3-hosts', 'a3-cache'} if project == 'a3-monitoring' else {'backend-prefill', 'backend-decode', 'cache-store', 'hosts-xpu' if project == 'xpu-monitoring' else 'hosts-dcu'}
        assert required <= names, (project, required - names)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--bounds", type=Path, default=ROOT / "histogram_bounds.json")
    parser.add_argument("--output", type=Path, default=ROOT / "projects")
    args = parser.parse_args()
    BOUNDS.update(json.loads(args.bounds.read_text()))
    source = json.loads(args.snapshot.read_text()) if args.snapshot else read_resources(args.output)
    resources = build(source)
    validate(resources)
    from dashboard_reorg import write_resources
    write_resources(resources, args.output)
    print(json.dumps({"projects": len(resources["projects"]), "dashboards": len(resources["dashboards"]),
                      "panels": sum(len(d["spec"]["panels"]) for d in resources["dashboards"])}))


if __name__ == "__main__":
    main()
