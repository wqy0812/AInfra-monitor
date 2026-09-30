# 发布与历史资料索引

当前操作入口见 [项目 README](../../README.md)、[Perses README](../../perses/README.md) 和 [部署目录说明](../../deploy/README.md)。本目录集中历史过程；文中日期、镜像、看板数量、状态和回退条件只对应当次发布。

## 已归档正文

| 范围 | 记录 |
| --- | --- |
| 监控迁移、指标口径、采集接入，2026-09-13 至 09-23 | [Monitoring 历史](monitoring-history.md) |
| Perses 初装、项目拆分和旧版看板，2026-09-14 至 09-22 | [Perses 历史](perses-history.md) |
| XPU 缓存看板，2026-09-30 | [清理与补充](../xpu-cache-refresh-20260930.md) |
| XPU 缓存查询，2026-09-23 | [发布记录](xpu-cache-20260923.md) |
| 流停顿方向监控，2026-09-23 | [发布记录](stream-direction-20260923.md) |
| 缓存查询与页面审核修复，2026-09-24 | [发布记录](review-fixes-20260924.md) |
| 历史查询 summary，2026-09-24 | [发布记录](history-summary-20260924.md) |

原 `deploy/<批次>/STATUS.md` 保留归档链接，历史脚本、manifest 和发布载荷维持原路径。本轮不创建 Perses `tooling/`、`tests/`、`legacy/` 分层，也不搬动被当前代码导入的历史辅助模块。

## 专项说明与后续发布

| 范围 | 入口 |
| --- | --- |
| 监控查询恢复，2026-09-29 | [连接池隔离、压缩与资源调整](../query-isolation-20260929.md) |
| API 审核修复，2026-09-30 | [修复、发布与验收](../review-fixes-20260930.md) |
| 原生请求指标口径 | [metric-scope-v2](../metric-scope-v2-20260915.md) |
| 主机 CPU 聚合 | [设计与验证](../host-cpu-materialization-20260916.md) |
| NPU 采集 | [接入说明](../npu-20260916.md) |
| XPU 环境、硬件、主机和角色 | [环境](../xpu-environment-2026-09-21.md)、[接入](../xpu-20260921.md)、[硬件](../xpu-hardware-20260922.md)、[主机](../xpu-hosts-20260922.md)、[09-23 角色记录](../xpu-role-fix-20260923.md)、[09-29 当前角色修复](../xpu-role-fix-20260929.md) |
| Perses 分类与指标对齐 | [重组](../perses-dashboard-reorg-20260923.md)、[对齐与 Mooncake](../perses-alignment-mooncake-20260923.md) |
| 跨看板时间继承与历史缓存 | [实现说明](../time-navigation-cache-20260924.md)、[发布与回退](../../deploy/time-navigation-20260925/README.md) |
| Perses 查询加速 | [运维说明](../../deploy/perses_acceleration/README.md)、[本轮状态](../../deploy/perses_acceleration/STATUS.md) |

## 执行证据的本地位置

以下 9 个报告已从发布源码目录迁入被忽略的 `evidence/`，迁移保持文件内容和 SHA-256 不变。检出 Git 不会获得这些报告；文档中的历史结论也不替代新一轮验收。

| 原批次 | `evidence/<批次>/` 中的文件 |
| --- | --- |
| `history-summary-20260924` | `complete.json` |
| `review-fixes-20260924` | `monitoring-complete.json`、`web-complete.json` |
| `stream-direction-20260923` | `monitoring-complete.json`、`perses-complete.json`、`perses-proxy-verification.json`、`web-complete.json` |
| `xpu-cache-20260923` | `monitoring-complete.json`、`web-complete.json` |

看板与数据源定义、指标目录、测试样本、发布输入 manifest/panels、版本锁和校验和继续由 Git 管理；按用途区分输入和结果，不按扩展名批量删除。
