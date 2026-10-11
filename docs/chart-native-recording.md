# 业务图表原生记录规则

`monitoring/chart_recording.py` 把 16 个数值字段的持久化计算交给 vmalert：

| 环境 | 字段 | 数量 |
| --- | --- | --- |
| A3 | Prefill/Decode 的 CPU 忙碌率、I/O 等待比例 | 4 |
| A3 | Mooncake 内存、SSD 的 used / total / ratio | 6 |
| DCU | Mooncake 内存、SSD 的 used / total / ratio | 6 |

CPU、DCU 容量、A3 容量分为三个组；每组通过 value、valid 两条向量规则一次产出多个字段，共 6 条规则、32 条结果序列；加上现有 Perses 126 条，共 132 条。所有新规则直接查询原始采集指标，不读取 Python 派生指标，也不依赖同组其他规则的写入。每 5 秒计算，延迟 3 秒，保留原 `monitoring_chart_value/valid` 名称、path/environment/schema/node 标签与单位，已有 Perses 表达式及其加速目录无需改写。

## 语义与写入归属

- CPU 复用原表达式及节点身份、唯一结果、有限值和 0–100 范围检查。
- 容量按完整采样时间选择数据，不把较旧 gauge 混入新 scrape。DCU 保留最新 scrape、无业务标签的唯一值语义；A3 拒绝多个新鲜来源。
- 零使用量为有效零；零总容量的比例为空。A3 非零使用量配零总容量、超容量时整对容量留空；DCU 保留合法非负 used/total，仅比例留空，保持各自既有契约。
- 无效值不写 value，valid 为 0；不会把未知值写成可见的零。采集或规则失败保留真实缺口，已有 5 秒样本计数继续阻止跨缺口连线。
- API 的周期性写入明确排除这 16 个字段，历史追赶也不再发布它们。`encode` 的默认完整编码仅供离线兼容校验；实际 Service 始终传入 `exclude_native=True`。
- latest JSON 仍需要业务快照；容量状态说明继续由业务适配层组装。CPU 仅查询当前批次末尾一个点用于 latest，不再对历史追赶区间逐点计算并写回。

## 切换和恢复

先替换 API，使旧字段写入停止，再替换 vmalert。初次启动在现有持久 state 目录创建 `chart-recording-cutover.json`，start 对齐到下一 5 秒格点；全部规则都有 `time() >= start` 守卫。重启保留相同边界，不能删除或重置该文件。旧历史保留原值，新规则不会改写切换前的时间点。API 与 vmalert 版本、写入归属和切换顺序必须一起核验。

启动时官方 vmalert 单独 replay 最近 12 小时与切换后范围的交集；首次安装尚无可回填区间时报告 not_needed。`chart_replay.finished` 只表示进程完成，不等于存储覆盖完整；失败保留日志和状态，当前版本修复，不回退。超过 12 小时的停机不自动补齐更早记录，也不由 Python 恢复写入。

发布工具仍为 `deploy/release.py api-image / container / acceleration / acceleration-ready`，规则检查要求 132 条。运行验收：

```bash
# 在候选 API 镜像内，发布前对比新 MetricsQL 与现有 Python 语义，只读。
python -m monitoring.chart_acceptance
# 发布后核对原生 value / valid 与独立原始重建；报告缺点数。
python -m monitoring.chart_acceptance --stored
```

服务 `/health` 增加 `chart_recording` 字段；原生 runtime 分别报告 perses_rule_count=126、chart_rule_count=6、总规则数、chart_start、chart_replay。就绪检查仍核验实际加载和求值错误。既有 Perses 后台历史 replay 状态单独报告，不与新增业务字段验收混淆。

## 剩余计算

本批不是全部业务预计算迁移完成。仍保留 DCU CPU、请求/Token 速率、延迟窗口、有效缓存分层、Store 查询窗口，以及动态 rank/device/segment 资源路径。前几类具有采样抖动窗口、生命周期/拓扑重置、代表 rank 与缺测传播语义，后者还有动态路径编码；后续迁移需继续做等价表达与真实 VM 对照，不能用固定 rate 窗口直接替换。请求画像及 API 缓存、请求合并不在本批范围。
