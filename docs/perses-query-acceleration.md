# 查询加速与原有口径

本页单独维护，`project_coverage.py --docs-only` 只在 [图表指标说明](../perses/METRICS_GUIDE.md) 中生成入口链接，不覆盖本文。

当前仓库记录 14 个查询合并面板及 18 个加速面板，共 249 个业务面板。具体上线范围由 [发布配置](../perses/acceleration_state.json) 和 [发布状态](../deploy/perses_acceleration/STATUS.md) 记录；线上状态需重新核查，历史发布记录不作为新批次准入。

合并只减少请求数。TTFT、ITL、E2E 仍分别显示 P50/P95/P99；`perses_order` 仅用于维持原图例与颜色顺序。网关 stage 的完整性检查按阶段独立生效；Mooncake 各 operation 保留独立曲线和有效性检查。

预计算直接复用冻结的原表达式，CPU、直方图、rank、engine、分位数、单位及缺样规则不变，不平均各 rank 的 P95/P99。1080p 实测支持步长为 5、15、20、60、120、600、3600 秒，每种步长单独计算。已完成的空结果保持业务空白；失败、未完成、历史覆盖缺口及最近 60 秒回到原始表达式。未识别表达式、筛选或参数也回源，保留图表编辑能力。

独立存储为 `monitoring_perses_value`、`monitoring_perses_complete`，不写 `monitoring_chart_*`，不改变 latest/history API。原始历史修正前必须通过 [运维说明](../deploy/perses_acceleration/README.md) 作废相应区间；不做近 30 天全量回填。

查询瘦身及分批准入流程见 [面板查询瘦身](query-slimming.md)。当前清单保留 15 项网关 `rewrites`；原发布依据知悉性能失败后的单独授权，未修改常规准入门槛。A3 保留原物化表达式及 catalog；取舍、未完成性能矩阵和后续比较要求由该说明维护。候选生成本身不代表已发布，更换 revision 仍需核验相等覆盖与实际收益。
