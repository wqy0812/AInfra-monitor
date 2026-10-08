# 发布与历史资料

现行操作以 [项目 README](../../README.md)、[部署工具](../../deploy/README.md)、[Perses 维护](../../perses/README.md) 和 [停机窗口升级](../maintenance-window-upgrade.md) 为准。本目录中的日期、镜像、数量、PID、任务 ID、等待条件和回退过程仅描述当时状态。

## 发布记录

| 范围 | 记录 |
| --- | --- |
| 初始迁移、采集与旧口径 | [Monitoring 历史](monitoring-history.md)、[Perses 历史](perses-history.md) |
| Perses 性能与时间导航 | [09-16 性能发布](perses-performance-20260916.md)、[09-25 发布](time-navigation-20260925-deployment.md)、[时间导航设计](time-navigation-cache-20260924.md) |
| 看板分类与平台接入 | [重组](perses-dashboard-reorg-20260923.md)、[对齐与 Mooncake](perses-alignment-mooncake-20260923.md)、[XPU 环境](xpu-environment-2026-09-21.md)、[角色修复](xpu-role-fix-20260929.md) |
| 网关与历史 API | [流停顿](stream-direction-20260923.md)、[summary](history-summary-20260924.md)、[查询隔离](query-isolation-20260929.md)、[实时回放](live-replay-20261002.md) |
| 审核修复 | [09-24](review-fixes-20260924.md)、[09-30](review-fixes-20260930.md)、[10-08](review-fixes-20261008-deployment.md) |
| A3 缓存 | [外部缓存断档](a3-cache-display-20261003.md)、[SSD 监控](a3-ssd-monitoring-20261005.md)、[图表精简](a3-cache-trim-20261008.md)、[共同分母贡献](a3-effective-cache-20261008.md)、[发布材料](a3-effective-cache-20261008-deployment.md) |
| XPU 缓存 | [接入](xpu-cache-20260923.md)、[清理与补充](xpu-cache-refresh-20260930.md) |
| 查询加速执行历史 | [旧状态快照](perses-acceleration-20260926.md)、[根因调查](perses-acceleration-root-cause-20260927.md)；现行配置见 [当前清单](../../deploy/perses_acceleration/STATUS.md) |
| 发布工具清理 | [退役范围与检索方式](retired-code.md)、[原文件摘要](retired-files.json) |

## 口径与设计追溯

[原生请求口径](metric-scope-v2-20260915.md)、[CPU 聚合](host-cpu-materialization-20260916.md)、[NPU](npu-20260916.md)、[XPU 硬件](xpu-hardware-20260922.md) 等记录保留其生效时间和当次证据。当前图表的单位、分母、有效性及真实断档以 [图表说明](../../perses/METRICS_GUIDE.md) 和现行源码为准。查询加速当前口径单独维护于 [设计说明](../perses-query-acceleration.md)。

## 材料与证据

带日期的 `deploy/` 目录保留必要 Dockerfile、manifest、panels 及导航；旧发布脚本不再提供可执行入口。历史正文移动后，原有相对链接已更新。

此前 9 个执行报告位于忽略的 `evidence/`：`history-summary-20260924/complete.json`；`review-fixes-20260924/` 的 monitoring/web complete；`stream-direction-20260923/` 的 monitoring/perses/web complete 和 perses-proxy-verification；`xpu-cache-20260923/` 的 monitoring/web complete。检出 Git 不会获得这些报告。服务器证据和进程仍需操作时核查。
