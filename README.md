# 当前 Perses 分类

DCU/XPU/A3 各 10 张专业看板，共 303 图；非缓存看板按公共核心指标和平台专属诊断对齐，A3 已拆分 Prefill/Decode，运行概览已删除，主机与加速卡分开。A3 Mooncake 原生指标已接入。详见 [对齐与 Mooncake 发布记录](docs/perses-alignment-mooncake-20260923.md)。XPU 角色修复见 [修复记录](docs/xpu-role-fix-20260923.md)。[历史发布记录](docs/releases/README.md) 中的旧分类、数量和免登录说明不代表当前配置。

# 独立监控

2026-09-25 已部署跨看板时间继承、Perses 后台/固定窗口暂停自动刷新，以及历史 API 多键缓存与在途请求合并。Perses 当前为 `0.54.0-perf.3`；本机 1080p 浏览器验收和线上 API/资源核验通过，行为、验证范围和回退见 [发布记录](deploy/time-navigation-20260925/README.md) 与 [实现说明](docs/time-navigation-cache-20260924.md)。

XPU 节点环境与初始部署拓扑见 [2026-09-21 环境记录](docs/xpu-environment-2026-09-21.md)，当前角色映射以 [2026-09-23 修复记录](docs/xpu-role-fix-20260923.md) 为准。

监控故障统一修复当前版本，不回退；发布脚本失败时保留现场并停止，详见 [故障处理约定](deploy/README.md)。

## 仓库范围

本仓库保存监控服务源码、测试、部署及校验脚本、采集配置、Perses 看板定义和上游版本校验清单。`perses/projects/<project>/dashboards/*.json` 是现行需要发布的看板定义，顶层 `perses/dashboards/` 保留历史基线；`vendor/releases.json`、`vendor/manifest.json`、校验和与 `perses/image-lock.json` 用于固定及校验依赖，均应入库。

发布脚本读取的 `manifest.json`、`panels.json`、source/image/release lock、指标目录和看板生成输入继续入库。执行结果 `*complete.json`、代理验证报告及运行快照集中保存到 `evidence/<发布批次>/`，不随源码跟踪；不能按 JSON 扩展名或 manifest 文件名一概忽略。

下载的二进制与镜像归档、镜像检查快照、运行数据、凭据、缓存、`evidence/`、`work/` 和跨项目临时补丁不入库。`tests/fixtures/e2e-20260914-1759.json` 仅包含时延直方图标签、计数及边界时间，用于复现桶混合错误；它不包含请求正文、响应正文或凭据，应随测试保留。`tests/browser_request_rate.cjs` 依赖被忽略的历史回放证据及同级 Web 文件，是本地验收工作记录，不属于检出后可运行的回归测试集。

[归档发布记录](docs/releases/README.md) 描述当时状态，`evidence/` 及跨项目工作记录路径仅供本地查阅，不随仓库提供。历史发布脚本中的镜像标签、检查点、验收文件和统计口径有版本约束；提交源码不表示当前候选已经部署或经过线上验收。原生指标口径见 [口径说明](docs/metric-scope-v2-20260915.md)。

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

本项目 Python 回归不要求同级 code-eval。使用 pyenv 管理的 Python 3.11+ 和项目 `.venv`，安装测试依赖后运行：

```sh
.venv/bin/python -m pip install -r requirements-test.txt
.venv/bin/python -m pytest -q
```

`pytest.ini` 收集 `tests/`、`perses/` 下的 Python 测试，排除本地工作产物。跨项目用例位于 `tests/integration/`，标记为 `cross_project`，默认明确跳过；仅在需要兼容性验证且已准备对应 code-eval 依赖时启用：

```sh
CODE_EVAL_ROOT=../code-eval .venv/bin/python -m pytest tests/integration --run-cross-project -q -rs
```

可省略 `CODE_EVAL_ROOT` 使用同级目录，或指定兼容的历史检出。缺少归档发布脚本会明确跳过，不计作通过；模块存在但导入/行为错误仍报失败。集成测试自动把数据目录指向 pytest 临时目录。完整测试入口和外部条件见 [测试说明](tests/README.md)。

部署校验工具另需 `python -m pip install -r requirements-tools.txt`（PyYAML，仅用于采集配置解析，不加入 monitoring-api 运行依赖）。`deploy/gateway_monitor_release.py` 的发布目录必须同时包含本仓库的 `deploy/check_gateway_monitor_candidate.py`，放在发布根目录并调用 `api` 模式；该校验只允许 VM 的 GET 查询，不依赖 code-eval 或写入 VM。

## 验证与故障处理

`python3 scripts/validate_live.py` 按同一发布目录的 `deploy/scrape.yml` 验证目标集合（当前配置为 26 个）、两节点共 16 张 DCU 卡、数据年龄以及 1/6/24/720 小时查询。目标以重标记后的 job、instance、environment 匹配，检查重复、缺失、多余、失败及过期观测；当前支持 static_configs 和 replace 重标记，不支持的发现/重标记方式会显式拒绝。`scripts/fault_check.py` 在独立临时目录和 18528/18529/18531 回环端口运行真实 VM/vmagent，验证存储中断后的补传，最后回收临时进程，不停止生产服务。

页面显示源不可用、待发送字节和 VM 存储余量。VM 在空闲空间不足 20 GiB 时停止写入，vmagent 缓冲有上限；缓冲耗尽会丢失较旧待发送数据，需要按实测增长预留容量。短时实测资源和容量不等于 24 小时稳定性结论。

如需备份新的 VM 数据，使用官方 `vmbackup-prod` 的快照一致性备份；恢复到一个空目录后再切换独立存储实例。测试环境当前未启用自动备份，不保留旧监控迁移数据及回退容器。参考：[VM 单机与备份](https://docs.victoriametrics.com/victoriametrics/single-server-victoriametrics/)、[vmagent 缓冲和采集](https://docs.victoriametrics.com/victoriametrics/vmagent/)、[node_exporter](https://github.com/prometheus/node_exporter)。

## 历史记录

历次指标迁移、采集接入、发布验收和回退说明统一从 [发布归档索引](docs/releases/README.md) 查阅。部署脚本和 Perses 工具的文件路径本轮保持不变。
