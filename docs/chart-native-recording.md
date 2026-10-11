# 图表记录迁移

现行方案见 [原生记录规则](native-recording-rules.md) 和 [已批准的口径变更](native-standard-review-20261011.md)。

旧 `monitoring_chart_*` 与 API 预计算已退出当前读写链路。新规则写 `monitoring_native_*`，使用 role、node、kind、tier、quantile、rank、device、segment 等普通标签。Perses 直接读 VM；API 仅把记录转换为仍在使用的 code-eval JSON 字段，不再生成指标、自动补算或代理 Perses。

旧逐点等价迁移的代码和验收可从 Git 历史查阅。历史数据不删除；新口径未覆盖的时间留空。当前窗口、部分覆盖、reset、直方图和容量变化均经 D1–D6 批准，不以旧兼容行为约束新建生产监控。
