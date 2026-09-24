# 当前 Perses 分类

DCU/XPU/A3 各 10 张专业看板，共 303 图；非缓存看板按公共核心指标和平台专属诊断对齐，A3 已拆分 Prefill/Decode，运行概览已删除，主机与加速卡分开。A3 Mooncake 原生指标已接入。详见 [对齐与 Mooncake 发布记录](docs/perses-alignment-mooncake-20260923.md)。XPU 角色修复见 [修复记录](docs/xpu-role-fix-20260923.md)。下文历史部署记录中的旧分类、数量和免登录说明不代表当前配置。

# 独立监控

2026-09-25 已部署跨看板时间继承、Perses 后台/固定窗口暂停自动刷新，以及历史 API 多键缓存与在途请求合并。Perses 当前为 `0.54.0-perf.3`；本机 1080p 浏览器验收和线上 API/资源核验通过，行为、验证范围和回退见 [发布记录](deploy/time-navigation-20260925/README.md) 与 [实现说明](docs/time-navigation-cache-20260924.md)。

XPU 节点环境与初始部署拓扑见 [2026-09-21 环境记录](docs/xpu-environment-2026-09-21.md)，当前角色映射以 [2026-09-23 修复记录](docs/xpu-role-fix-20260923.md) 为准。

## 仓库范围

本仓库保存监控服务源码、测试、部署及校验脚本、采集配置、Perses 看板定义和上游版本校验清单。`perses/projects/<project>/dashboards/*.json` 是现行需要发布的看板定义，顶层 `perses/dashboards/` 保留历史基线；`vendor/releases.json`、`vendor/manifest.json`、校验和与 `perses/image-lock.json` 用于固定及校验依赖，均应入库。

下载的二进制与镜像归档、镜像检查快照、运行数据、凭据、缓存、`evidence/`、`work/` 和跨项目临时补丁不入库。`tests/fixtures/e2e-20260914-1759.json` 仅包含时延直方图标签、计数及边界时间，用于复现桶混合错误；它不包含请求正文、响应正文或凭据，应随测试保留。`tests/browser_request_rate.cjs` 依赖被忽略的历史回放证据及同级 Web 文件，是本地验收工作记录，不属于检出后可运行的回归测试集。

下文带日期的发布记录描述当时状态，`evidence/` 及跨项目工作记录路径仅供本地查阅，不随仓库提供。历史发布脚本中的镜像标签、检查点、验收文件和统计口径有版本约束；提交源码不表示当前候选已经部署或经过线上验收。当前原生指标口径见本文末节。

## 项目结构与运行

监控在 test4 独立运行，模型、主机、DCU 和 Mooncake 指标通过 HTTP 采集。评测平台只提供现有页面和查询适配；评测引擎不再启动指标采集，不写 SQLite 监控快照，也不生成任务监控归档。

用户于 2026-09-13 明确取消 24 小时等待、旧历史兼容及本次迁移回退容器保留。当前只查询 VictoriaMetrics；旧监控快照和一次性历史缓存已清理。任务、响应、判题数据属于 code-eval，不属于本项目。

## 运行结构

| 位置 | 组件 | 接口 | 资源限制 |
| --- | --- | --- | --- |
| test4 | VictoriaMetrics 1.151.0 | 127.0.0.1:18428 | 2 CPU / 2 GiB |
| test4 | vmagent 1.151.0 | 127.0.0.1:18429 | 0.5 CPU / 512 MiB |
| test4 | monitoring-api | :18430，来源 IP 白名单 | 1 CPU / 512 MiB |
| dcu1/dcu2 | node_exporter 1.12.1 | 127.0.0.1:19100 | 0.5 CPU / 256 MiB |
| dcu1/dcu2 | DCU exporter | 节点地址:19500，来源 IP 白名单 | 0.5 CPU / 256 MiB |

DCU exporter 的 `/metrics` 提供本地 SMI 数据，`/node-metrics` 代理本机 node_exporter。SMI 每 5 秒采样、3 秒超时，失败及过期不输出设备数值。设备访问采用显式设备映射和只读 `/opt/hyhal` 驱动挂载；没有 SSH 密钥、Docker Socket 或 privileged。旧 Docker 的 clone3 兼容采用额外限制性过滤器，保留原 seccomp 限制。

test4 查询地址为 `http://122.247.53.162:18430`；评测平台 Web 和引擎通过 `MONITOR_QUERY_URL` 使用该地址。

中央数据目录 `/data2/monitoring`：`vm/` 保存 30 天原始与派生时序，`buffer/` 为最大 5 GiB 的发送缓冲，`state/` 只保存派生计算进度，`release/` 为代码和固定版本二进制，`evidence/` 为部署验收证据。没有任务 ID 标签或任务数据副本。

## 查询与统计

A3 主机 CPU 与 CPU I/O 等待由 monitoring-api 按 5 秒周期计算新增聚合点，Perses 和历史 API 直接读取 VM 中的 `host-cpu-v1`，详情见 [CPU 聚合设计与验证](docs/host-cpu-materialization-20260916.md)。已于 2026-09-16 发布至 test4；新聚合历史从上线后积累，原始历史保留。

- `/api/monitoring/latest`：返回兼容页面的快照、源观测时间、rank 明细、DCU/主机及缓存数据，并提供存储余量和发送积压。
- `/api/monitoring/history?hours=1`：支持大于 0、最多 720 小时，约 720 个展示点。新部署的 30 天查询只显示已经积累的数据，不代表已有 30 天观测。
- `/health`：报告处理进度、核心来源状态及错误。
- 查询不通返回不可用，不自动恢复 SSH 或读取旧 SQLite。

历史接口支持 `view=summary`，供 code-eval 实时监控读取已有的节点、缓存、时延和网关聚合曲线。该视图在 VM 的数值、有效性与断档查询中排除 `resources` 逐卡、逐 rank 和存储段明细，同时清理最新补点中的资源明细；聚合数值、采样步长、空值和断档语义保持不变。省略 `view` 或使用 `view=full` 保留完整接口。发布时先更新 monitoring-api，再更新 code-eval Web；源码支持不表示已完成线上发布或性能验收。

`/api/monitoring/history` 同时按所选 `environment` 查询网关原始时序，新增 `points[].gateway`：`stream_idle_max_seconds`（流式输出停顿）、`oldest_age_seconds`（所有在途请求的最大年龄）、该请求的 `backend` / `stage` / `stage_name`，以及 `gap_before`。按当前 Perses 网关生成监控的完整性、新鲜度和生命周期校验读取；有效空闲为零，无效观测为 null。网关时间点与原有派生时间点合并，后端数据缺失不抹掉有效网关点；阶段、后端切换或缺样断线。

两项网关查询分别限时 4 秒，失败时通过响应顶层 `gateway_status` 标记对应字段为 `unavailable`，不丢弃其他成功的监控曲线。正常查询状态为 `ok`，仍可能没有有效观测。沿用历史步长、5 秒缓存和并发上限，不引入持久化回填或新的采集任务；请求速率及 E2E API 字段不变。

网关回归包含 `tests/test_gateway_history.py`；`tests/test_gateway_live_vm.py` 通过 `GATEWAY_TEST_VM_URL=http://127.0.0.1:<端口>` 在独立临时 VictoriaMetrics 中注入合成数据，核对真实查询与现有看板结果。该地址必须属于可丢弃的本地测试实例。

`calculator.py`、`cache_metrics.py`、`monitor_series.py` 的统计算法来自迁移时 code-eval 基线，独立维护。保留 DP/TP/PP 去重、缓存分层分母、55–65 秒窗口、拓扑变化、计数重置和无流量留空。`replay.py` 重放原始抓取时间，不插值原始计数。派生曲线以数值及有效性标志写回 VM，历史降采样保留断档标记。

## 构建、部署及升级

1. `python3 scripts/download.py` 按 `vendor/releases.json` 下载固定版本，并校验官方 SHA256；`vendor/manifest.json` 保存二进制摘要。离线环境传输已校验的 `vendor/bin`。
2. 将本项目同步到目标机 `/data2/monitoring/release`，在 test4 中央机执行 `python3 deploy/start_test4.py`，节点执行 `python3 deploy/start.py node --bind <节点地址>`。脚本只在本机管理容器，不自行 SSH。
3. test4 使用 Docker 28.5.2 静态发行版和 `monitoring-api:test4-20260914`；API 的 Python 3.11.16、依赖及代码来自 test2 发布镜像，迁移时已验证导入。节点继续使用已有 DTK 镜像。重建中央机前需加载迁移镜像或准备兼容运行时。`requirements.txt` 对齐实际部署的 Python 包版本。
4. 已存在服务不会被 `start.py` 覆盖。升级先构建新标签、验证 `/health` 和数据，再用 `deploy/replace.py <本项目容器名> <新镜像>` 替换。工具与 `deploy/container_validation.py` 一起提供：在停服前验证所有权、支持的组件、监听地址、镜像和备份名；启动后在最多 90 秒内要求容器身份、运行状态、重启次数与组件数据连续正常至少 10 秒，之后才删除临时旧容器。失败按原容器 ID 恢复。支持独立 API、VM、vmagent、node/DCU exporter；未知组件或非 host 网络在停服前拒绝。API 要求两环境数据新鲜，VM 验证真实查询，vmagent 验证采集计数推进，exporter 验证必要指标和设备观测。操作仅针对 `monitoring.owner=independent`，保留原 `--driver-readonly` / `--loadavg` 参数。
5. 修改采集配置后调用本机 `POST http://127.0.0.1:18429/-/reload`，检查全部目标 `up` 和源数据。

代码需要 Python 3.11+；节点 exporter 使用 Python 3.10 标准库。当前没有外部通知或 Grafana；已新增 Perses 看板，见 [Perses 部署与运维](perses/README.md)。

本地测试需保留 `monitoring/` 与 [code-eval/](../code-eval/README.md) 同级布局。`tests/test_cache_semantics.py` 会导入 code-eval 的 `app.monitor`、`app.db`、`app.config` 和 归档发布脚本 `deploy/releases/legacy_20260908_20260916/bin/cache_monitor_host.py`，因此完整测试集依赖同级 code-eval 源码及其 Python 依赖；监控服务运行本身不依赖该源码目录。测试环境还需安装 `pytest`、`pytest-asyncio`、`PyYAML`。在 `monitoring` 目录、已安装上述依赖的 Python 3.11+ 环境中执行：

```sh
PYTHONPATH=.:../code-eval/backend python -m pytest tests
```

可将 `python` 替换为已准备好的 `../code-eval/.venv/bin/python`。仅设置 `PYTHONPATH=.` 会导致跨项目用例无法导入 `app`；归档发布脚本按文件路径加载。执行 `PYTHONPATH=.:../code-eval/backend python -m pytest -q` 可一次收集后端、Perses 看板和发布工具的全部 pytest 回归。测试时将 `DATA_DIR`、`HOST_DATA_DIR`、`STATE_DIR` 指向独立临时目录，避免使用实际运行数据。

部署校验工具另需 `python -m pip install -r requirements-tools.txt`（PyYAML，仅用于采集配置解析，不加入 monitoring-api 运行依赖）。`deploy/gateway_monitor_release.py` 的发布目录必须同时包含本仓库的 `deploy/check_gateway_monitor_candidate.py`，放在发布根目录并调用 `api` 模式；该校验只允许 VM 的 GET 查询，不依赖 code-eval 或写入 VM。

## 验证与故障处理

`python3 scripts/validate_live.py` 按同一发布目录的 `deploy/scrape.yml` 验证目标集合（当前配置为 26 个）、两节点共 16 张 DCU 卡、数据年龄以及 1/6/24/720 小时查询。目标以重标记后的 job、instance、environment 匹配，检查重复、缺失、多余、失败及过期观测；当前支持 static_configs 和 replace 重标记，不支持的发现/重标记方式会显式拒绝。`scripts/fault_check.py` 在独立临时目录和 18528/18529/18531 回环端口运行真实 VM/vmagent，验证存储中断后的补传，最后回收临时进程，不停止生产服务。

页面显示源不可用、待发送字节和 VM 存储余量。VM 在空闲空间不足 20 GiB 时停止写入，vmagent 缓冲有上限；缓冲耗尽会丢失较旧待发送数据，需要按实测增长预留容量。短时实测资源和容量不等于 24 小时稳定性结论。

如需备份新的 VM 数据，使用官方 `vmbackup-prod` 的快照一致性备份；恢复到一个空目录后再切换独立存储实例。测试环境当前未启用自动备份，不保留旧监控迁移数据及回退容器。参考：[VM 单机与备份](https://docs.victoriametrics.com/victoriametrics/single-server-victoriametrics/)、[vmagent 缓冲和采集](https://docs.victoriametrics.com/victoriametrics/vmagent/)、[node_exporter](https://github.com/prometheus/node_exporter)。

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

访问 [DCU 监控看板](http://122.247.53.162:18431/projects/dcu-monitoring)，包含运行概览、主机与 DCU、HiCache 与 Mooncake、网关基础状态，共 33 个面板。免登录可编辑，来源限制为用户指定的 `122.0.0.0/8` 及回环；通过 Perses 代理读取本机 VM，原有监控组件保持运行。部署、数据口径和回退步骤见 [Perses 说明](perses/README.md)，验收位于本地 `evidence/perses-20260914/README.md`（不入库）。

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

完整口径、测试、部署与回退说明见 [metric-scope-v2-20260915.md](docs/metric-scope-v2-20260915.md)。前次仅流式发布记录保存在 [local-latest-20260915.md](docs/local-latest-20260915.md)，不代表当前口径。

## A3 NPU 硬件监控（2026-09-16）

复用 A3-1 `122.209.21.24:8082/metrics` 和 A3-2 `122.209.21.25:8082/metrics` 的既有 `npu-exporter`，以 `job=npu-a3`、`environment=a3-vllm` 每 5 秒采集。保留 exporter 自带时间戳，采集 `npu_.*` 和 `machine_npu_nums`，每节点 16 个芯片 ID。

[A3 · 加速卡资源](http://122.247.53.162:18431/projects/a3-monitoring/dashboards/accelerator-resources) 对齐 DCU 六项硬件图表：利用率、显存已用、温度、功耗、显存总量和显存占比。支持节点与 NPU 芯片筛选；HBM 的 MiB 转为 GiB，不使用 KV Cache 代替整芯片显存。功耗按 exporter 原始芯片 ID 展示，不相加为整机功耗。有效零保留，超过 15 秒的源观测、失败抓取及非法值留空。

本次只热加载 vmagent 采集配置并更新 A3 主机看板；没有重启中央监控、推理或网关服务。实现与回退见 [NPU 发布说明](docs/npu-20260916.md)。

## XPU 接入（2026-09-21）

已新增 `xpu-pd` 推理采集与 monitoring-api 支持，以及 [XPU Perses 项目](http://122.247.53.162:18431/projects/xpu-monitoring)。XPU 网关画像也已接入；HiCache、缓存层级和硬件先留空。发布、验收与回退见 [XPU 接入记录](docs/xpu-20260921.md)。

流停顿方向监控新增 `points[].gateway.backend_wait_max_seconds` 与 `write_active_max_seconds`，两者分别按环境取当前最大读取等待和当前连续写出时长；页面和 Perses 展示这两项，旧字段保留。有效空闲为零，指标缺失留空，不以旧字段回填。完整口径和验证说明见 [流停顿方向监控](docs/stream-direction-monitoring-20260923.md)。
