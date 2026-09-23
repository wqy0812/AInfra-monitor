# Perses 图表重组 · 2026-09-23

DCU、A3、XPU 的运行概览已取消，所有保留图表按观测位置迁入专业看板。配置以此次线上快照为基线；原有 292 图保留 286 图，仅退役三个项目各两张已从网关源码移除的画像索引图。各图查询表达式、单位换算、有效性门控、分位数、数值格式保持一致。

## 当前入口

- [DCU 监控](http://122.247.53.162:18431/projects/dcu-monitoring)：10 张看板。
- [A3 监控](http://122.247.53.162:18431/projects/a3-monitoring)：9 张看板。
- [XPU 监控](http://122.247.53.162:18431/projects/xpu-monitoring)：10 张看板。

| 看板 | 资源 ID | 内容 |
|---|---|---|
| 网关请求流量与质量 | gateway-requests | 流量与结果、延迟、Token 与 Usage |
| 网关在途请求诊断 | gateway-generation | 在途阶段、等待首输出、后端更新与写持续时间 |
| 网关画像存储健康 | gateway | 队列、写入错误、丢弃与存储量 |
| 后端请求性能 | backend-performance | 原概览请求速率、Token 吞吐、TTFT/ITL/E2E及共享时延样本数 |
| Prefill / Decode 诊断 | backend-prefill / backend-decode | DCU/XPU 按角色划分调度、KV 池与传输 |
| 后端引擎诊断 | backend-diagnostics | A3 公共调度与性能、Prefill、Decode |
| 主机资源 · Node Exporter | hosts-dcu / a3-hosts / hosts-xpu | 主机 CPU、内存、磁盘、网络和系统状态 |
| 加速卡资源 | accelerator-resources | DCU/NPU/XPU Exporter 硬件指标，节点与设备筛选 |
| 缓存与存储 | cache-store / a3-cache | HiCache、Mooncake、前缀与外部缓存 |
| 采集与监控健康 | monitoring-health | 原概览采集链路与共享 VM/vmagent 服务分区 |

网关与后端的延迟观测点保持独立。P/D 请求量不能相加为唯一请求量，TTFT 不等同于纯 Prefill 阶段耗时。共享 VM/vmagent 继续使用其实际 `dcu-pd` 来源，项目采集区域使用各项目自身环境。

标题包含单位，Y 轴保留刻度及原数值格式；提示删除“曲线与范围”“Y 轴单位”段落标题，保留必要的统计含义。网关 `reading_response` 图例修正为“读取完整响应”，流式/非流式说明同步当前源码；仅修改图例文字，不修改查询。

## 生成与迁移

当前资源在 `perses/projects/`；`perses/dashboard_reorg.py` 实施可重复迁移并生成每个面板的来源、去向和退役原因。`project_split.py` 的现行入口保留重组后的资源，不恢复旧概览；XPU 现行生成保留自身配置，禁止从已重组 DCU 看板推造 XPU 硬件。

`dashboard_reorg.py --snapshot before.json --output projects --evidence DIR` 用于一次性旧结构迁移；`project_split.py` 用于当前结构再生成；`project_coverage.py --docs-only` 同步说明。旧顶层 `perses/dashboards/` 仅为历史定义，不是发布源。

## 验收与证据

本地 28 项回归通过：全量迁移计数、查询保持、布局与变量引用、环境隔离、重复生成、文档同步、删除回退和并发保护。发布前和发布后分别进行 774 组 VM/代理查询对照；窗口空白单独记录，不把空白当作零或故障。

1080p 浏览器验收未完成：SSH MCP 拒绝 `-L` 端口转发，本地 Playwright 无法经允许的 SSH MCP 路径连接线上页面。没有改用命令行 SSH 或直连绕过限制。服务端结构校验、资源读回与查询验收不替代页面视觉验收。

服务器新增证据根目录 `/data2/monitoring/perses/evidence/dashboard-reorg-20260923`，其中 `release/` 和 `release/projects/`（含三个项目及 dashboards 子目录）保存候选脚本与配置。另新增生产模板目录 `/data2/monitoring/perses/release/projects/xpu-monitoring/` 及其 `dashboards/` 子目录。`before.json`、`migration.json`、`semantics.json`、`audit-candidate.json`、`audit-after.json`、`journal.json`、`after.json`、`publication.json` 保存迁移与发布证据。凭据只在服务器读取，不写入这些证据文件。

## 回退

通过 SSH MCP 在 test4 执行：

```sh
python3 /data2/monitoring/perses/evidence/dashboard-reorg-20260923/release/project_release.py rollback --evidence /data2/monitoring/perses/evidence/dashboard-reorg-20260923
```

按日志恢复删除的旧资源、还原修改项、删除本次新增项；检测并发编辑后停止，避免覆盖用户更改。该操作不修改 VM 历史数据、不重启服务。生产生成器的逐文件回退内容保存在 `runtime-install.json`。恢复看板后，通过 SSH MCP 执行以下命令还原对应生成器与资源模板（同样检测并发改动）：

```sh
python3 /data2/monitoring/perses/evidence/dashboard-reorg-20260923/release/reorg_runtime.py rollback --evidence /data2/monitoring/perses/evidence/dashboard-reorg-20260923
```
