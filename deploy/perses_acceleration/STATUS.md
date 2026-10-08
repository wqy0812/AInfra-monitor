# Perses 查询加速：仓库发布清单

本页依据当前 `perses/acceleration_state.json`、`perses/projects/` 与 `monitoring/perses_acceleration_catalog.json`。它不宣称服务器此刻健康；实时状态需读取目标机 `/health` 和资源快照。

| 项目 | 已绑定加速数据源的面板 |
| --- | ---: |
| A3 | 8 |
| DCU | 8 |
| XPU | 2 |
| 合计 | 18 |

发布清单记录 `cpu`、`dcu`、`a3` 三组，以及 14 个查询合并面板。预计算目录包含 18 个目标表达式，支持 5、15、20、60、120、600、3600 秒步长。合并面板数量与专用数据源绑定数量是不同统计。

日常升级按 [停机窗口流程](../../docs/maintenance-window-upgrade.md)。当前工具见 [运维说明](README.md)。新的专项验收只依据当批目录的原始证据；不能把运行时长、旧 PID 或旧成功标记作为通过依据。

此前“CPU 完成、DCU 待发布、A3 排队”等内容属于旧执行阶段，完整正文已移入 [历史执行快照](../../docs/releases/perses-acceleration-20260926.md)，诊断过程见 [历史根因记录](../../docs/releases/perses-acceleration-root-cause-20260927.md)。
