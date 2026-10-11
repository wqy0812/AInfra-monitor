# 指标口径与历史边界

本页保留当前接口需要解释的统计规则。面板定义与单位见 [图表说明](../perses/METRICS_GUIDE.md)，采集配置见 `deploy/scrape.yml`；部署时的节点、进程和瞬时数值不能代替实时核验。

## 请求与网关统计

后端使用原生请求指标，不强制筛选流式请求；精度测试的非流式请求可以影响吞吐和时延分布。计数与直方图先按来源验证重置、缺采、桶与 count 的一致性，再合并互斥的流式分组；复制 rank 不直接相加。无请求时样本数为零、分位数为空；来源不完整或分母未知时留空。

| 指标 | 范围 |
| --- | --- |
| 后端请求、Token、队列、TTFT、ITL、E2E | 原生整体统计，保留完整来源身份 |
| 网关请求量、画像、Token、总耗时、结果、错误、阶段 | `request_scope="all"` |
| 网关首增量、首输出等待、流停顿 | `request_scope="streaming"` |
| 独立非流式到达数 | `request_scope="nonstreaming"` |

后端请求派生字段使用 `request-metrics-v2`，普通资源与缓存沿用各自 schema。新口径的历史从其启用点积累，不混读旧 `request-streaming-v1` 或旧请求 `v1/latency-v2`。画像首增量只统计实际有效流式增量，其样本数不等于总请求数；跨计数生命周期的汇总与趋势规则见 [项目 README](../README.md)。实现见 `monitoring/request_scope.py`。

## A3 缓存贡献

Prefill 的 `vllm:prompt_tokens_by_source_total` 分为显存命中 G（`local_cache_hit`）、外部 KV 命中 M（`external_kv_transfer`）和本地计算 C（`local_compute`）。先累计四个 Prefill 实例约 60 秒内的增量，令 T＝G＋M＋C。

| `nodes.prefill.cache_60s.effective` 字段 | 计算 |
| --- | --- |
| `ratio` | (G＋M) / T |
| `device` | G / T |
| `storage` | M / T |

三类来源必须完整且唯一，实例、引擎和模型身份一致；三类之和与总输入一致，两类命中之和与缓存输入一致。四个实例必须齐全，窗口不能跨重置、缺采或来源变化。T＝0 时保留零 Token 数、比例留空；有输入而零命中时比例为有效零。Mooncake 表示外部 KV 的 Token 贡献，不拆内存/SSD。原 `cache_60s.ratio/external_ratio` 各自使用原分母，不改义、不相加。

该口径使用 `vllm-prefill-source-v1`。已记录的启用边界为 **2026-10-08 10:42:55 +08:00**（`1791427375`）；接口的 `cache_effective_since` 和字段 `since` 从持久化状态读取，运行时以它们为准。启用前留空，启用后需要积累完整窗口；`effective_cache` 保留独立断线标记。不回填或清空旧历史，不重置既有处理水位。实现见 `monitoring/a3_effective.py`。

## A3 采集完整性

当前适配器按 Prefill 4 个端点、Decode 16 个端点校验；端点与引擎缺失、过期、重复或异常增加都不能形成有效总量。

已确认旧 Decode 仅采集前 4 个端点时，实际有 16 个引擎。完整采集确认边界为 **2026-09-28 14:19:30 +08:00**（`1790576370`），不是声称第一条新端点样本恰在此刻到达。旧历史保留，不乘倍数补算；更早拓扑未经核实，不推定全都漏采。latest/history 的 `collection_coverage` 返回预期数量、确认边界和解释。实现见 `monitoring/a3.py`，面板说明由当前生成器维护。

## A3 主机 CPU 聚合

`monitoring/host_cpu.py` 复用 VM 的原 CPU 忙碌率和 I/O 等待表达式，由 API 对新增时间点计算并保存 `monitoring_chart_value/valid{schema="host-cpu-v1"}`。`nodes.<role>.cpu`、`cpu_iowait` 带节点标签，history 和 Perses 读取已保存的聚合值。

保持一分钟 rate、idle/iowait 扣除、guest/guest_nice 排除、逐序列重置及采样完整性检查。查询失败只令本批 CPU 无效；导入失败不推进批次水位。`host_cpu.computed_at` 是计算时间点，不是原始抓取时间。聚合启用前留空，不读取旧 CPU `v1` 或自动回填；原始 CPU 历史仍保留。

## 缓存容量与 XPU 边界

Mooncake Store 的内存/SSD 容量表示登记对象与配额，不等于物理文件占用或 SSD I/O。Store 查询命中率、模型 Token 命中率和本地/外部前缀命中率使用不同分母，不能互换。缺少分层查询计数时不推算内存/SSD 命中率。

XPU 最近批次 `cache_hit_rate` 可能在空闲时保持旧值；一分钟 Token 命中率从同一调度器的计算/复用计数得到，无工作量或缺源时留空。HiCache pending 空转次数与墙钟占比按 rank 分线，不是设备整体空闲率、请求数或缓存 I/O 耗时。设备 KV 池的可用量包括可回收部分，不能标成纯空闲，也不能乘 TP rank 数。

已记录 XPU 角色标签在 **2026-09-29 23:46:25 +08:00** 修正；此前错误角色历史没有改写，跨边界查询须保留这一限制。当前角色按采集配置和实际进程核验。没有当前来源的 CPU 缓存容量、独立命中率或回载/淘汰指标不补零；旧时序出现过计数不证明当前实例仍提供它们。

## 流停顿方向

| 网关 gauge（秒） | history 字段 | 含义 |
| --- | --- | --- |
| `aigate_stream_backend_wait_max_seconds` | `gateway.backend_wait_max_seconds` | 当前有效、未结束的流累计 Read 等待及尚未返回的 Read 耗时 |
| `aigate_stream_write_active_max_seconds` | `gateway.write_active_max_seconds` | 当前 Write 至 Flush 返回前的持续时间 |

两项按环境取当前请求最大值，不求和，也不是整个查询范围的峰值；它们可以来自不同请求。后端等待从首次读取响应体开始，排除路由、连接、响应头和写出耗时；有效 SSE 增量重置累计等待，心跳、usage 不重置。结束或未知流退出后端等待统计，最终写出仍计时。

有效空闲为零，缺失、过期、失败抓取、来源不完整或跨重启窗口留空；新指标不以旧字段回填。这些指标只定位阻塞方向，不能单独断定模型故障或客户端不读，短于抓取周期的阻塞可能没有被采到。网关接口及诊断见 [流异常说明](../../aigate/docs/STREAM_INCIDENTS.md)。
