# XPU 缓存与存储看板清理及补充

2026-09-30 00:01（北京时间）发布。调查开始于 09-29，证据批次名沿用 `xpu-cache-refresh-20260929`。只修改 XPU 的 `cache-store` 看板及其生成器和服务器资源副本；采集配置无需变化，服务及模型容器均未重启。

## 删除五张当前无数据源的图

删除 CPU→XPU 回载 Token 速率、XPU→CPU 淘汰 Token 速率、平均回载耗时、平均淘汰耗时和回载操作速率。

这些指标并非历史上从未出现：最近七天的旧 Prefill 实例中，回载和淘汰计数确有增长；但当前 .34 Prefill 的原生 `/metrics` 没有对应指标，最近一小时也没有样本。旧时序保留，只移除当前看板中的无源图表；不将缺失解释为零或未发生相关操作。

## 当前六张图

| 图表 | 原始指标与统计口径 |
| --- | --- |
| 采集状态 | `up`，保留真实 0/1 |
| 最近批次前缀缓存命中率 | `cache_hit_rate`，TP0/PP0；最后一次上报批次的复用 /（计算 + 复用），空闲时可能保持旧值 |
| Prefill 计算与缓存复用速率 | `realtime_tokens_total` 的 `prefill_compute`、`prefill_cache` 一分钟速率；同一调度器口径，TP0/PP0 两条线 |
| 一分钟前缀缓存命中率 | 上述一分钟速率计算的 Token 加权比例；无工作量或任一来源缺失时留空 |
| HiCache 预取等待空转频率 | `hicache_scheduler_idle_with_pending_total` 一分钟速率；八个 rank 分线，不相加 |
| HiCache 预取等待空转时间占比 | `hicache_scheduler_idle_with_pending_seconds_total` 一分钟速率 ×100；八个 rank 分线 |

初次发布由八张旧图删除五张、增加四张，形成七张。随后按用户要求去重：`backend-prefill` 已有“Prefill KV 池占用（%）”，同一设备池的 Token 数量图从缓存看板删除，同时移除空分组；保留 Prefill 诊断中的占用率图。缓存看板现为六张，全站仍为 30 张看板、301 张图。已有复用速率改为计算/复用两条线，批次命中率标题改为准确口径。

## 语义依据和边界

通过 SSH MCP 读取当前 Prefill 容器安装的源码，位于 `/root/miniconda/envs/python310_torch25_cuda/lib/python3.10/site-packages/sglang/srt/`：

- `managers/scheduler_metrics_mixin.py:182`：批次命中率分母是 `log_input_tokens + log_hit_tokens`；同一批次分别递增实时计算和复用 Token 计数。
- `managers/scheduler_runtime_checker_mixin.py:28`：`num_used_tokens = max_total_num_tokens - (available_size + evictable_size)`。因此不能标为全部已占用 KV，差值也不能标成纯空闲。它们不表示 CPU HiCache 容量，不乘 TP rank 数。
- `managers/scheduler.py:1936`、`:1959`：只有 HiCache L3 预取导致没有可运行批次、且没有在途 overlap 批次时才统计空转 tick 和墙钟耗时。
- `metrics/collector.py:512`：两个等待计数在初始化时显式创建零值。零表示未观察到该状态，不是采集缺失；不等于 XPU 硬件整体空闲率，也不是请求数、预取操作数或缓存 I/O 耗时。

当前没有原生 CPU 缓存容量、CPU 独立命中率及回载/淘汰传输指标，不新增占位图。不把硬件显存或主机内存当成缓存专用容量。

查询使用现有 `Queries` 的来源健康、新鲜度、窗口样本完整性和计数重置校验；比值无分母留空。原始命中率限定有效区间，不把无效值截成有效数。

## 初次发布验收与证据

- 21 项相关测试通过，包括生成幂等性、当前指标白名单、历史指标排除、文档一致性及加速查询资源计数。
- 发布前、发布后，10 条真实查询均有数据；Perses 代理与 VM 原始响应的标签、时间戳、数值一致。容量为 207,232 Token；验收点不可回收占用 128、空闲或可回收 207,104，窗口命中率约 30.77%；等待图各有八条有效零序列。这些是验收瞬时值，不是长期结论。
- 发布采用在线快照、并发编辑检查、原子文件替换、逐项读回。前后其余看板和数据源完全一致，监控四个服务的容器身份及启动时间未变。失败仅留证，不回退。
- 本机 Chrome/Playwright 使用真实线上数据、1920×1080，滚动验收全部七张图；结果和截图位于本地证据目录。

服务器新增目录：`/data2/monitoring/perses/evidence/xpu-cache-refresh-20260929`，包含前后快照、原始指标、旧指标历史查询、发布前后验证及文件变更计划。使用 `PYTHONDONTWRITEBYTECODE=1` 执行，未创建额外 `__pycache__` 子目录。临时浏览器认证文件验收后删除。

本地证据：`evidence/xpu-cache-refresh-20260929/`，不随源码入库。

## 同日 KV 图去重

用户指出设备 KV 池在其他看板已有展示后，核对线上 `backend-prefill/core-kv` 为“Prefill KV 池占用（%）”。缓存看板新增的 Token 数量图与该占用率图覆盖同一设备池，按用户要求删除 `cache-store/kv-pool` 和“设备 KV 池”空分组，保留 Prefill 诊断的占用率图。同步本地生成器、资源、文档及服务器两个运行时文件；其他资源和服务身份不变。

21 项相关测试通过。缓存看板剩余 7 条查询以及 Prefill 诊断占用率查询均有真实数据，Perses 代理与 VM 结果一致。本机 Chrome/Playwright 1920×1080 验证六张图均渲染、有数据、无横向溢出和页面错误，已确认删除图和空分组不再存在。全站为 301 张图、367 条查询。

复用上述证据目录，未新增服务器目录；此次证据文件以 `kv-dedup-` 开头。临时浏览器认证文件验收后删除。
