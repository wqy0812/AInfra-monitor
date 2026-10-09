"""Rebuild metric coverage and panel descriptions from project resources."""
import argparse
import collections
import json
import re
from pathlib import Path
from project_split import PROJECTS, ROOT, read_resources


def family(name, histograms):
    for suffix in ("_bucket", "_count", "_sum", "_created"):
        if name.endswith(suffix) and name[:-len(suffix)] in histograms:
            return name[:-len(suffix)]
    return name


def dependencies(environment, expression):
    # Source mapping for existing monitoring_chart_value calculators. These are
    # indirect dependencies, not additional queries or claims of raw coverage.
    if "monitoring_chart_value" not in expression:
        return set()
    names = set()
    a3 = environment == "a3-vllm"
    mappings = {
        ".requests": ["vllm:request_success_total"] if a3 else ["sglang:num_requests_total"],
        ".decode_tokens": ["vllm:generation_tokens_total"] if a3 else ["sglang:realtime_tokens_total"],
        ".output_tokens": ["vllm:generation_tokens_total"] if a3 else ["sglang:generation_tokens_total"],
        ".cpu": ["node_cpu_seconds_total"],
        ".percentiles.ttft": ["vllm:time_to_first_token_seconds"] if a3 else ["sglang:time_to_first_token_seconds"],
        ".percentiles.itl": ["vllm:inter_token_latency_seconds"] if a3 else ["sglang:inter_token_latency_seconds"],
        ".percentiles.e2e": ["vllm:e2e_request_latency_seconds"] if a3 else ["sglang:e2e_request_latency_seconds"],
        ".kv_usage": ["vllm:kv_cache_usage_perc"],
        ".hicache": ["sglang:hicache_host_used_tokens", "sglang:hicache_host_total_tokens"],
        ".segments": ["segment_allocated_bytes", "segment_total_capacity_bytes"],
        ".segment_ratio": ["segment_allocated_bytes", "segment_total_capacity_bytes"],
        "mooncake.capacity": ["master_allocated_bytes", "master_total_capacity_bytes"],
        "mooncake.ssd_capacity": ["master_allocated_file_size_bytes", "master_total_file_capacity_bytes"],
        "mooncake.query_60s": ["total_get_nums_", "valid_get_nums_"],
        "mooncake.tier_query_60s": ["total_get_nums_", "mem_cache_hit_nums_", "file_cache_hit_nums_"],
    }
    for text, metrics in mappings.items():
        if text in expression:
            names.update(metrics)
    if ".cache_60s" in expression:
        names.update(["vllm:prefix_cache_queries_total", "vllm:prefix_cache_hits_total",
                      "vllm:external_prefix_cache_queries_total", "vllm:external_prefix_cache_hits_total"] if a3 else
                     ["sglang:prompt_tokens_total", "sglang:cached_tokens_total", "sglang:prefill_effective_tokens_total"])
    if ".resources.queue" in expression:
        names.update(["vllm:num_requests_running", "vllm:num_requests_waiting"] if a3 else ["sglang:num_running_reqs", "sglang:num_queue_reqs"])
    return names


def omission(name, job):
    if name.endswith(("_info", "_created")) or name == "flag":
        return "版本/配置/初始化元信息；不单独作趋势图。"
    if name.startswith("aigate_") and any(x in name for x in ("prefix_", "token_pairs", "message_count", "tool_count", "request_bytes")):
        return "请求结构及前缀重现明细，本次按关键指标范围不单独展示。"
    if name.startswith(("go_", "process_")):
        return "运行时明细；已有进程 CPU/常驻内存覆盖常用资源判断。"
    if name.startswith(("node_", "vm_", "vminsert_", "vmselect_")):
        return "组件/内核细项；本次保留负载、容量、延迟、错误及可靠性相关关键图。"
    if name.startswith("vllm:") and any(x in name for x in ("estimated_", "mm_cache_", "request_params_", "iteration_", "max_num_", "computed_tokens", "prompt_tokens_by_source", "prompt_tokens_cached", "prompt_tokens_total")):
        return "细粒度工作量或参数分布；核心吞吐、缓存及请求长度由现有图表覆盖。"
    return "未选为独立关键图；保留原始采集供按需查询。"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--before", type=Path)
    parser.add_argument("--docs-only", action="store_true", help="Regenerate descriptions using current projects and the saved historical coverage catalog.")
    args = parser.parse_args()
    resources = read_resources(ROOT / "projects")
    if args.docs_only:
        if args.inventory or args.before:
            parser.error("--docs-only cannot be combined with --inventory/--before")
        coverage = json.loads((ROOT / "metric_coverage.json").read_text())
    else:
        if not args.inventory or not args.before:
            parser.error("--inventory and --before are required unless --docs-only is used")
        before = json.loads(args.before.read_text())
        metrics = args.inventory.read_text()
        rows = json.loads(metrics)["data"]["result"]
        histograms = {x["metric"]["__name__"][:-7] for x in rows if x["metric"]["__name__"].endswith("_bucket")}
        targets = collections.defaultdict(set)
        for x in rows:
            m = x["metric"]
            targets[(m.get("environment", ""), m.get("job", ""), family(m["__name__"], histograms))].add(m["__name__"])
        coverage = []
        for (env, job, name), names in sorted(targets.items()):
            direct, indirect, previous = [], [], []
            for stage, documents in (("before", before["dashboards"]), ("after", resources["dashboards"])):
                for d in documents:
                    for pid, p in d["spec"]["panels"].items():
                        expression = " ".join(x["spec"]["plugin"]["spec"]["query"] for x in p["spec"]["queries"])
                        if 'environment="' + env + '"' not in expression:
                            continue
                        identity = d["metadata"]["project"] + "/" + d["metadata"]["name"] + "#" + pid
                        raw = any(re.search(r"(?<![A-Za-z0-9_:])" + re.escape(n) + r"\{", expression) for n in names)
                        derived = name in dependencies(env, expression)
                        if raw or derived:
                            (previous if stage == "before" else direct if raw else indirect).append(identity)
            selected = bool(direct or indirect)
            coverage.append({"environment": env, "job": job, "metric_family": name, "metric_names": sorted(names),
                "status": ("已覆盖" if previous else "新增覆盖") if selected else "未展示",
                "method": "查询或校验依赖" if direct else "派生计算间接覆盖" if indirect else "未选",
                "panels": sorted(set(direct + indirect)),
                "reason": "" if selected else omission(name, job)})
        (ROOT / "metric_coverage.json").write_text(json.dumps(coverage, ensure_ascii=False, indent=2) + "\n")
    descriptions = {}
    guide = ["# Perses 图表指标说明", "", "按项目和看板定位；请求总量包含流式与非流式。首增量、首输出等待及流停顿按源端实际可观测样本统计；非流式请求数是总请求中的一个子集。", ""]
    guide += ["查询合并、预计算和回源规则见 [查询加速与原有口径](../docs/perses-query-acceleration.md)。", ""]
    for d in resources["dashboards"]:
        project, name = d["metadata"]["project"], d["metadata"]["name"]
        descriptions.setdefault(project, {})[name] = {k: p["spec"]["display"].get("description", "") for k, p in d["spec"]["panels"].items()}
        guide += ["## " + project + " / " + d["spec"]["display"]["name"], "",
                  "[打开看板](http://122.247.53.162:18431/projects/" + project + "/dashboards/" + name + ")", ""]
        for p in d["spec"]["panels"].values():
            guide += ["### " + p["spec"]["display"]["name"], "", p["spec"]["display"].get("description", ""), ""]
    (ROOT / "panel_descriptions.json").write_text(json.dumps(descriptions, ensure_ascii=False, indent=2) + "\n")
    (ROOT / "METRICS_GUIDE.md").write_text("\n".join(guide))
    report = ["# Perses 关键指标覆盖清单", "", "历史采集基线：2026-09-15 test4 采集清单及当时迁移前后看板对照。下表和 metric_coverage.json 保留该次采集的指标范围，不代表当前在线目标或最新覆盖数量；2026-09-16 新增 NPU 等指标未计入该基线。按 environment、job 和指标族计数；同一族在多个 job 出现会分别记录。直方图的桶、sum、count 合为一族，校验依赖不等同于单独展示数值。", "",
              "| 环境 | 原已覆盖 | 新增覆盖 | 未单独展示 |", "|---|---:|---:|---:|"]
    report.insert(4, 'A3 Mooncake 基线已于 2026-10-05 按当前 Master 端点更新：当时 88 个原生指标族均有展示。2026-10-08 按用户要求精简缓存看板 27 图，原始采集保留；覆盖清单保留历史基线，当前图表以图表说明为准。其他来源仍沿用上述历史基线。'
                     '2026-10-09 按用户要求精简 77 图：删除与数值图重复的比例图、重复或数据质量类图、分位数样本数图及大部分采集内部图，网关结束结果合并为一图，网关画像存储看板并入采集健康；规则见 panel_trim.py，原始采集不变。')
    for env in sorted({x['environment'] for x in coverage}):
        counts = collections.Counter(x["status"] for x in coverage if x["environment"] == env)
        report.append(f'| {env} | {counts["已覆盖"]} | {counts["新增覆盖"]} | {counts["未展示"]} |')
    report += ["", "## 关键覆盖", "", "新增网关吞吐/耗时/长度/usage、后端队列和阶段时延、缓存分层容量及请求量、主机瓶颈、共享监控服务。完整对应关系见 [metric_coverage.json](metric_coverage.json)。", "",
               "## 未展示的取舍", "", "- 版本、初始化及内部生命周期信息主要用于说明或有效性检查。",
               "- 请求结构、前缀重现、Token 联合桶和冷门内核明细保留采集，按本次关键指标范围不单独展开。",
               "- A3 于 2026-09-16 接入 npu-a3：利用率、HBM 已用/总量/占比、温度和功耗，与 DCU 六项硬件图表对齐；KV 占用仍独立展示。",
               "- DCU 当前主机采集未包含 A3 已有的磁盘操作耗时、网卡错误/TCP 重传和 timex；本次不扩展采集器。", "",
               "## 当前代码口径与空白", "", "- 任一环境的请求画像在所选窗口没有有效请求样本时留空；是否有历史观测需按实际查询确认。",
               "- 未出现的错误类别没有序列，错误比例或 usage 比例无分母时留空。",
               "- DCU 输出 Token 吞吐在源 valid 不为 1 时留空，Decode Token 另有有效来源。",
               "- 请求量、画像、Token、总耗时、错误和在途状态包含流式与非流式；首增量、首输出等待、流停顿及流观察未知保留源端实际观测范围。非流式专图展示总请求的一个子集。",
               "- 当前源码的请求派生使用 request-metrics-v2，A3 CPU 使用 host-cpu-v1，其他资源与缓存使用 v1；不回退读取旧请求 schema，不补写历史。此处描述源码，不证明本次修复已经部署。", ""]
    (ROOT / "METRIC_COVERAGE.md").write_text("\n".join(report))
    print(json.dumps({"metric_families_by_source": len(coverage), "status": dict(collections.Counter(x["status"] for x in coverage))}, ensure_ascii=False))


if __name__ == "__main__":
    main()
