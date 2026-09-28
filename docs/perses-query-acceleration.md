# 查询加速与原有口径（2026-09-26）

本页单独维护，`project_coverage.py --docs-only` 只在 [图表指标说明](../perses/METRICS_GUIDE.md) 中生成入口链接，不覆盖本文。

本次候选覆盖 14 个查询合并图及 18 个预计算图，业务图表仍为 303 个。具体上线范围由 [发布配置](../perses/acceleration_state.json) 和 [发布状态](../deploy/perses_acceleration/STATUS.md) 记录；未通过性能准入的项目保留原查询。

合并只减少请求数。TTFT、ITL、E2E 仍分别显示 P50/P95/P99；`perses_order` 仅用于维持原图例与颜色顺序。网关 stage 的完整性检查按阶段独立生效；Mooncake 各 operation 保留独立曲线和有效性检查。

预计算直接复用冻结的原表达式，CPU、直方图、rank、engine、分位数、单位及缺样规则不变，不平均各 rank 的 P95/P99。1080p 实测支持步长为 5、15、20、60、120、600、3600 秒，每种步长单独计算。已完成的空结果保持业务空白；失败、未完成、历史覆盖缺口及最近 60 秒回到原始表达式。未识别表达式、筛选或参数也回源，保留图表编辑能力。

独立存储为 `monitoring_perses_value`、`monitoring_perses_complete`，不写 `monitoring_chart_*`，不改变 latest/history API。原始历史修正前必须通过 [运维说明](../deploy/perses_acceleration/README.md) 作废相应区间；不做近 30 天全量回填。
