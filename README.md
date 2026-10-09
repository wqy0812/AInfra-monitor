# 独立监控

monitoring 采集 DCU、A3、XPU 的模型、主机、加速卡、网关和缓存指标，使用 VictoriaMetrics 保存时序，由 monitoring-api 提供 latest/history 查询、派生计算与 Perses 查询加速。code-eval 负责消费查询结果，不启动采集，也不保存本项目的监控快照。

本文描述仓库当前配置；线上镜像、健康和数据新鲜度须在操作时重新核验。历次部署结论见 [发布归档](docs/releases/README.md)。

## 架构与配置

| 组件 | 配置与职责 |
| --- | --- |
| VictoriaMetrics / vmagent | test4 中央存储与采集；固定版本见 `vendor/releases.json`，当前采集目标以 `deploy/scrape.yml` 为准 |
| monitoring-api | `monitoring/`；来源 IP 白名单、三环境查询、派生计算与预计算；配置见 `deploy/start_test4.py` |
| 节点采集 | node/DCU exporter 及平台原生 HTTP 指标；采集配置保留各平台真实 job 与标签 |
| Perses | `perses/projects/` 保存 3 个项目、30 张看板、249 个面板；默认 VM 数据源及专用加速数据源分开 |
| 查询加速 | `monitoring/perses_acceleration_catalog.json` 固定 18 个目标表达式；`perses/acceleration_state.json` 记录 14 个合并面板及 CPU/DCU/A3 三组准入 |

test4 配置的接口为 VM `127.0.0.1:18428`、vmagent `127.0.0.1:18429`、API `:18430`、Perses `:18431`。实际监听、代理和访问控制以目标机配置为准。API 拒绝空白、通配符和网段白名单；默认只允许 IPv4/IPv6 回环，显式调用方通过 `ALLOWED_CLIENTS` 指定。

中央数据目录 `/data2/monitoring` 中，`vm/` 保存时序，`buffer/` 保存发送缓冲，`state/` 保存处理水位、待发布批次及管理状态，`release/` 保存程序，`evidence/` 保存执行证据。升级保留历史、缓冲、水位和真实断档。

## 查询与统计

- `/api/monitoring/latest`：当前快照、源观测时间、模型/主机/缓存指标及存储与积压状态。
- `/api/monitoring/history`：按环境查询，支持大于 0、最多 720 小时；默认完整数据，`view=summary` 去除逐卡/逐 rank 资源明细，保留聚合数值、空值和断档。返回的是实际已积累历史。
- `/health`：三环境模型来源、处理进度和错误；缺失必需来源或水位过旧时报告降级。
- `/internal/perses/`：仅对目录中的精确表达式提供预计算查询；未覆盖、无效或未知表达式回源。见 [查询加速口径](docs/perses-query-acceleration.md)。

保留 DP/TP/PP 去重、分层缓存分母、55–65 秒窗口、计数重置、拓扑变化和无流量留空语义。重放原始抓取时间，不插值原始计数；Mooncake 来源变化后重新积累查询窗口。过旧水位从 VM 保留边界分批推进，成功导入并保存水位后记录真实 `retention_gap`。

A3 总体、设备和外部 KV 贡献使用明确的共同分母；启用时间前保留空白，不反推历史。具体口径与生效点见 [A3 贡献口径](docs/releases/a3-effective-cache-20261008.md)、[图表说明](perses/METRICS_GUIDE.md) 和 [指标覆盖清单](perses/METRIC_COVERAGE.md)。覆盖清单是带日期的采集基线，不代表实时健康。

## 开发与发布

本地使用 pyenv 管理的 Python 3.11+ 和项目 `.venv`，运行前核对版本。测试按改动范围选择，入口见 [测试说明](tests/README.md)。查询、统计、采集/导入或存储逻辑变化才触发相关真实 VM 测试；UI 与性能变化执行各自验收。

首次安装使用 `scripts/download.py` 下载并验证固定依赖，`deploy/start.py` / `deploy/start_test4.py` 创建缺失服务，Perses 使用 `perses/install.py` 和 `perses/seed.py`。初始化镜像默认值不代表线上最新镜像；重建时必须重新核验并显式提供本次验证的镜像。

后续升级统一从 `python3 deploy/release.py --help` 进入，支持容器、Perses 镜像、看板、生成器和加速发布。命令在目标主机本地运行，连接与传输遵循工作区 SSH MCP 约束。流程见 [停机窗口升级](docs/maintenance-window-upgrade.md)、[部署工具](deploy/README.md) 和 [Perses 维护](perses/README.md)。新增服务器目录须告知用户。

允许停机；保留版本、所有权、访问控制、并发编辑和读回检查。失败停止后续步骤并修复当前版本，现行工具没有可执行回退入口。每次发布使用新的证据目录，不重用旧报告冒充验收。

## 仓库边界

源码、采集配置、现行看板、版本锁、校验和、必要测试样本与历史构建输入由 Git 管理。历史批次脚本和旧单项目看板基线已退役，原路径与 SHA-256 见 [退役清单](docs/releases/retired-code.md)。仍被生成器导入的辅助模块保留。

镜像归档、二进制、运行数据、凭据、容器快照和执行报告留在忽略的 `evidence/` / `work/`；不能按 JSON 扩展名一概删除。VM 与发送缓冲按实际增长预留容量，恢复数据时使用一致性快照，禁止清空现有存储来解决升级问题。
