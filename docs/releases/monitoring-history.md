# Monitoring 历史发布记录

从根 README 归档的 2026-09-13 至 2026-09-23 记录。数量、运行状态和操作步骤均描述当时版本；现行入口见 [项目说明](../../README.md)。正文中的代码路径仍相对于原项目根目录。

## 请求画像生命周期计数（2026-09-13）

`/api/request-profile/metrics` 使用 `counter_method=lifecycle-v2`。`profile_counters.py` 根据已有全局/分组生命周期标记分段计算；恢复计数器的正常重启沿用原生命周期。查询只读取 VM 摘要，不下载或保存完整原始历史，不新增指标标签。

同一生命周期使用可信边界基准；新生命周期从零计入首个观测。分布、总数、均值和分位数统一计算，并在每个来源/生命周期与汇总层校验累计桶。`quality.counter_status` 为 `ok` 或 `incomplete`，具体原因在 `counter_issues`；矛盾分布列入 `invalid_histograms` 并清空其分布、均值及分位数。缺少边界和生命周期证据时保持空值，不推断为零。速率窗口跨生命周期或抓取失败时留空。

生命周期时间戳按毫秒归一化；单次最多 256 个生命周期、每个 VM 摘要结果最多 20,000 条序列，沿用查询服务全局并发和 20 秒期限。覆盖生命周期标记上线前的旧观测可能显示统计不完整；此次不删除历史，也不假造旧数据。

2026-09-13 发布镜像 `monitoring-api:profile-lifecycle-20260913`。38 项本地回归和 6 个独立临时 VM 场景通过，真实异常窗口由 3/2 修正为 4/4。操作与验证见同级 code-eval/evidence/profile-lifecycle-20260913/；远端源码同步到 `/data2/monitoring/release/monitoring`。仅更新查询服务及 Web 的未知到达数显示，未重启 VM、vmagent、网关、模型或评测引擎。

## Mooncake 内存 / SSD 原生监控（2026-09-14）

新增采集 `master_allocated_file_size_bytes`、`master_total_file_capacity_bytes`、`mem_cache_hit_nums_`、`file_cache_hit_nums_`。`replay.py` 在原始抓取时间上计算 `ssd_capacity` 与 `tier_query_60s`；分层查询命中率为最近约 60 秒内该层命中增量 / Store 查询总增量。它不表示模型 token 命中、读取成功或物理 SSD I/O。SSD 容量是后端配额口径。

旧容量及查询成功比例字段保持兼容；新指标缺失不影响旧查询窗口。新曲线独立保留有效性和降采样断档，首次发布前无数据，不补零、不重置水位。实时与历史 API 共用 VM 数据，平台及请求画像上下文已同步接入。

已发布 `monitoring-api:mooncake-ssd-20260914`，vmagent 使用热加载；没有重启 VM、vmagent、推理服务或评测引擎。细节及同样本验收见同级 code-eval 的 `docs/Mooncake内存与SSD监控部署验收_20260914.md`。

## test4 迁移与 A3 采集（2026-09-14）

VictoriaMetrics、vmagent、monitoring-api 已迁入 test4；原 VM 数据、派生水位与缓冲一并复制。test2 的旧监控容器、监控专用镜像及 `/data2/monitoring` 已按后续指令删除；test2 的评测 worker 未重启。test1 的 Web 和引擎仅替换监控地址并重建，维护模式保持原值。

新增 `vllm-a3` 的 8 个目标（A3-1、A3-2 各 `7100–7103/metrics`）和 `node-a3` 的 2 个目标（各 `9100/metrics`），均为 5 秒采集。分别保留 `vllm:.*` 与 `node_.*` 原始指标。A3 的标签为 `environment=a3-vllm`、`node=a3-1|a3-2`，`instance` 区分主机与端口；原有目标保持 `environment=dcu-pd`。环境标签现按目标设置，避免全局外部标签覆盖 A3。

该迁移阶段只增加采集与 VM 存储；后续 A3 页面与派生统计已上线，见下方“DCU / A3 实时监控切换”。当前原有 10 个及新增 10 个目标均已观测到 `up=1`。用户随后要求停止进一步数据核对，未继续进行性能或长期稳定性验收；历史查询的旧 2 秒门槛未通过，已观测查询约 2.4–6.3 秒。迁移摘要位于本地 `evidence/migrate-test4-20260914/README.md`（不入库）。

## Perses 看板（2026-09-14）

访问 [DCU 监控看板](http://122.247.53.162:18431/projects/dcu-monitoring)，包含运行概览、主机与 DCU、HiCache 与 Mooncake、网关基础状态，共 33 个面板。免登录可编辑，来源限制为用户指定的 `122.0.0.0/8` 及回环；通过 Perses 代理读取本机 VM，原有监控组件保持运行。部署、数据口径和回退步骤见 [Perses 说明](../../perses/README.md)，验收位于本地 `evidence/perses-20260914/README.md`（不入库）。

## DCU / A3 实时监控切换（2026-09-14）

平台实时监控已支持 DCU / A3 切换；A3 采用与 DCU 对齐的 6 张精简图。两环境的实时、历史查询及平台 SSE 通过 `environment=dcu-pd|a3-vllm` 选择，默认 DCU。A3 的四实例计数和直方图按原始观测计算；缺失、重置及断档留空。A3 独立派生水位从上线开始推进，不回填旧历史。

Perses 新增 A3 运行概览、主机、缓存三张独立看板，原有 DCU 看板保留。入口、指标口径、测试、发布和保护状态位于本地 `evidence/a3-switch-20260914/README.md`（不入库）。

## 双环境请求画像（2026-09-14）

请求画像查询接受 `environment=dcu-pd|a3-vllm`，默认 DCU。统计、模型来源、采集覆盖、质量及生命周期发现全部按环境隔离；不通过 backend 标签推断来源环境。

A3 画像抓取以独立 `aigate-a3` 配置使用独立凭据，经目标重标记保存 `job=aigate,environment=a3-vllm`，保留 instance/node 标识；5 秒周期和原 VM 保留期不变。配置见 deploy/profile_a3_scrape.yml。A3 新联合桶可能缺少窗口起点基线，此时按已有生命周期规则显示 incomplete/null，不补零。现场原始验证及校时记录在 evidence/profile-environments-20260914/。


## DCU 时延修复与历史重算（2026-09-14）

DCU 的 E2E、TTFT、ITL 改为保留完整来源和流式标签，逐序列校验计数后合并窗口增量。VM 返回顺序和重复分块不影响结果；重置、缺失、矛盾桶和未知上界留空。无请求时 samples 为 0，分位数为空。窗口仍为 55–65 秒，20 秒断档失效。

仅 DCU 时延的 26 个字段使用 `monitoring_chart_value/valid{schema="latency-v2"}`；其他字段和 A3 继续使用 `v1`。平台实时、历史、请求画像上下文及 Perses 均读取修正结果，缺失不回退到旧时延。旧派生值和原始观测保留。

重算工具为 `python -m monitoring.latency_rebuild --state <独立进度文件>`，必须在具有 Python 3.11 和项目依赖的环境运行。自动发现 30 天范围内最早的原始时延观测并固定截止时间，以 5 分钟批次及 80 秒预热重放。检查点带代码指纹和文件锁；只有 VM 确认写入后推进，不得修改指纹跳过版本校验。续跑必须使用原镜像及原检查点；计算代码变化需创建新检查点并核对重复区间结果。历史补算不会修改正常服务水位。

本次固定历史范围为 2026-09-13 04:33:50 至 2026-09-14 19:02:00，共 27,699 个五秒点、462 批，之后通过独立实时写入器衔接正式服务。A3 实时重放只在需要发布的时间点计算分位数，预热阶段保留窗口状态；默认完整重放不变。实时循环按每轮开始时间安排 5 秒周期，计算耗时不再额外叠加固定 5 秒等待。

发布与验收详情位于本地 `evidence/latency-v2-20260914/README.md`（不入库）。

## 原生请求指标（2026-09-15，已部署）

正常业务以流式为主，后端监控直接使用原生指标，不强制筛选 `is_streaming=true`。接受精度测试的非流式请求混入并影响延迟分布。A3 无流式标签的指标正常使用；DCU TTFT、ITL、E2E 都保留源端的完整样本。DCU ITL 按输出批次间隔及新增 Token 数平均。

计数和直方图保留完整来源身份，先逐序列验证重置、缺失、桶与 count 一致性，再合并互斥的流式分组。复制 rank 不直接相加；未知采样或计数异常仍留空，无请求窗口的样本数为零、分位数为空。

请求派生字段使用 `schema="request-metrics-v2"`，资源和缓存维持 `v1`。使用独立派生水位，从新口径上线时开始写入，不迁移、不重建旧请求历史，也不回退读取旧口径。

网关一般请求指标使用 `request_scope="all"`；首增量和流观察指标保留 `streaming`；独立非流式到达数保留 `nonstreaming`。画像查询以新的 all-request 计数起点隔离升级前的旧首增量序列。

完整口径、测试、部署与回退说明见 [metric-scope-v2-20260915.md](metric-scope-v2-20260915.md)。前次仅流式发布记录保存在 [local-latest-20260915.md](local-latest-20260915.md)，不代表当前口径。

## A3 NPU 硬件监控（2026-09-16）

复用 A3-1 `122.209.21.24:8082/metrics` 和 A3-2 `122.209.21.25:8082/metrics` 的既有 `npu-exporter`，以 `job=npu-a3`、`environment=a3-vllm` 每 5 秒采集。保留 exporter 自带时间戳，采集 `npu_.*` 和 `machine_npu_nums`，每节点 16 个芯片 ID。

[A3 · 加速卡资源](http://122.247.53.162:18431/projects/a3-monitoring/dashboards/accelerator-resources) 对齐 DCU 六项硬件图表：利用率、显存已用、温度、功耗、显存总量和显存占比。支持节点与 NPU 芯片筛选；HBM 的 MiB 转为 GiB，不使用 KV Cache 代替整芯片显存。功耗按 exporter 原始芯片 ID 展示，不相加为整机功耗。有效零保留，超过 15 秒的源观测、失败抓取及非法值留空。

本次只热加载 vmagent 采集配置并更新 A3 主机看板；没有重启中央监控、推理或网关服务。实现与回退见 [NPU 发布说明](npu-20260916.md)。

## XPU 接入（2026-09-21）

已新增 `xpu-pd` 推理采集与 monitoring-api 支持，以及 [XPU Perses 项目](http://122.247.53.162:18431/projects/xpu-monitoring)。XPU 网关画像也已接入；HiCache、缓存层级和硬件先留空。发布、验收与回退见 [XPU 接入记录](xpu-20260921.md)。

流停顿方向监控新增 `points[].gateway.backend_wait_max_seconds` 与 `write_active_max_seconds`，两者分别按环境取当前最大读取等待和当前连续写出时长；页面和 Perses 展示这两项，旧字段保留。有效空闲为零，指标缺失留空，不以旧字段回填。完整口径和验证说明见 [流停顿方向监控](stream-direction-monitoring-20260923.md)。
