> 后续已扩展为全部 18 项原生替换，当前入口见 [原生记录规则运维](native-recording-rules.md)。本文保留当时实验范围与证据；`vmalert_recording_trial.py` 依赖的预计算实现已于 2026-10-16 删除，原文用 `git show 42312a9:experiments/vmalert_recording_trial.py` 查阅。

# Issue #4：原生记录规则替代预计算的评估

评估日期：2026-10-11。范围是 [issue #4](https://github.com/wqy0812/AInfra-monitor/issues/4) 中的记录规则建议；本次没有改变生产服务、数据源、看板、catalog 或加速开关，也没有向 GitHub 发布评论。服务器上没有新增目录。

## 结论

值得继续采用 **vmalert 执行计算与回填，加速 API 保留精确匹配和正确性保护** 的方向。直接把面板表达式换成普通记录指标，尚不符合“关闭加速不改变数值和空白”的要求。本次完成了真实 VM/vmalert 的本地实验；它是可行性证据，不是上线实现，也不证明性能优于现有预计算。

VictoriaMetrics 的记录规则由独立的 **vmalert** 组件执行，通过 remote write 保存到 VM；不是在现有 VM 进程里加一份规则文件就会生效。官方组件支持定时计算和显式 replay 回填。[官方说明](https://docs.victoriametrics.com/victoriametrics/vmalert/)

## 当前基线

- 通过 SSH MCP 只读检查 `test4`：VM 镜像标签 `monitoring-vm:1.151.0`，API 健康为 `ok`；126 个加速任务，未禁用分组，任务无错误。
- 线上容器内和本地 catalog 的 SHA-256 一致：`b09fe4feb091a9a7934047dc56d2ab5cc2aa1c06d3597a00685c18ee6c3e6a54`。
- catalog 包含 18 个表达式：4 个 CPU、6 个 DCU 直方图、8 个 A3 直方图。步长为 5、15、20、60、120、600、3600 秒。
- 当前预计算模块为 716 行。它还实现完成计数、持久化待写批次、读回校验、缺口回源、历史失效、精确表达式匹配与图例顺序；这些职责不由一条普通 recording rule 自动替代。

## 可复现实验

新增两个仅供本地实验的文件：

- [`vmalert_recording_rules.py`](../experiments/vmalert_recording_rules.py)：从当前 catalog 生成 7 个 group、126 条规则；保留原表达式，只展开步长和全选变量；规则名包含 panel/revision 身份摘要及步长。
- `vmalert_recording_trial.py`（已删除）：创建临时 VM/vmalert 容器，导入现有合成数据，运行原生 replay，比较原表达式、记录指标以及现有预计算。结束后删除本次容器和网络，保留本地规则与证据。

```sh
.venv/bin/python --version
pyenv version
docker pull victoriametrics/victoria-metrics:v1.151.0
docker pull victoriametrics/vmalert:v1.151.0
.venv/bin/python experiments/vmalert_recording_trial.py \
  --output evidence/issue4-recording-rules-new-run
```

`--output` 必须是新目录。脚本使用 Docker 自动分配的 loopback 端口和临时存储，使用 `docker cp` 传入规则，不需要修改 Docker Desktop 文件共享配置。不接入生产 VM。

本次固定 VM/vmalert 版本均为 1.151.0，镜像 ID、catalog 指纹、时间窗口、原始差异及每项检查均写入 `report.json`。生成的规则通过原生 `-dryRun`；replay 使用真实原表达式和 remote write，共导入 1,223,636 个源样本。短步长检查正常、零流量、缺样、过期、down、reset、缺桶、消失、恢复；600/3600 秒本次只验证长窗口正常数据，没有把短窗口空结果冒充这些步长的故障测试。

## 验证结果

最终证据：[`report.json`](../evidence/issue4-recording-rules-20261011-r4/report.json)，同目录保留 `rules.json`、`dry-run.log`、`replay.log` 和 `vm.log`。

| 检查 | 数量 | 结果 |
| --- | ---: | --- |
| 直接查询普通记录指标 vs 原表达式 | 936 | 334 项不一致，不能直接采用 |
| 对齐 replay 起点并使用精确时间戳过滤 | 936 | 数值、标签、时间点和空白全部一致 |
| 当前 catalog 的全部有限筛选组合 | 378 | 全部一致，包括交集为空的筛选 |
| 同步长 instant query vs range query | 126 | 全部一致 |
| 原生记录值 vs 当前预计算输出 | 126 | 全部一致，当前 API 确认走了 126 次加速路径 |
| 未做排序的序列返回顺序 | 126 | 63 项不同 |
| 使用现有读端排序后的序列顺序 | 126 | 全部一致 |

本地镜像 ID：VM `sha256:6d164540a04f49ba4e696cbdb70f9fee78be1e94b8f2a1292743a0b1ab8275bd`；vmalert `sha256:e519f14c31f4d68015702e684a0fc3f1b4e559aa985d8f4462768f07f55b05c9`。原生 replay 成功退出，`guarded_semantics_passed=true`。这些通过项只涵盖实验范围，不包括原生实时调度、进程重启、部分写入、生产负载或浏览器绘制。

本地启动阶段的两次 Docker 适配失败保留在首轮及 `r2`；时间起点未对齐导致的语义失败保留在 `r3`，没有把它们记成成功。最终实验容器与网络已清理。

## 已实测的语义差异

### 普通记录指标会填补原表达式的空白

原表达式在 5 秒 CPU 缺样场景返回 24 个点，普通记录指标返回 26 个点：两个节点各自缺失的一个点被旧值填上。记录规则保留了值，但普通指标选择器还有回看最近样本的语义。

以下读法在本次回填实验中恢复了原来的缺口；还需去掉 recording rule 新增的指标名，保持原始标签：

```metricsql
label_del((recorded_metric and (timestamp(recorded_metric) == time())), "__name__")
```

这不是说 vmalert 永远不处理过期序列。1.151.0 的实时 `exec` 会根据上次结果写 `StaleNaN`；`execRange` 回填只转换查询返回的样本，因此不能用实时行为推断回填后的断档语义。[固定版本源码](https://github.com/VictoriaMetrics/VictoriaMetrics/blob/v1.151.0/app/vmalert/rule/recording.go)

### 步长、回填起点和顺序仍须保留

- 每种 `$__interval` 都参与有效性判断，不能把一份 5 秒规则的输出简单降采样成其余六种口径。
- group 的 `interval` 与查询的 `step` 是两个参数。原生 instant query 默认 step 为 5 分钟；实验明确设置每个 group 的 `params.step`，并使用 `-datasource.queryStep=0`，保持原查询口径。[参数说明](https://docs.victoriametrics.com/victoriametrics/vmalert/#common-mistakes)
- 初次语义实验的 replay 起点未对齐 600/3600 秒，导致 36 项精确结果不匹配；改为所有步长的最小公倍数对齐后重测。初始失败保留在 `r3`，最终证据在 `r4`。
- VM 返回记录指标的序列顺序与当前 API 不总是一致，会影响图例/颜色。现有 `result_rows` 排序可作为读端适配，顺序也单独比较，不能只比较标签和值的集合。

### 完成与缺失的区别仍需设计

没有记录值，可能是业务空白，也可能是尚未执行、执行失败、服务停机或写入未可见；有部分值也不证明一个时间点的全部序列已写齐。`timestamp()` 过滤只能拒绝旧值，不能证明完整性。

当前实现先写值并读回，之后才发布完成计数，查询侧也核对计数。单独再加一条 `vector(1)` 心跳规则，不能证明另一条业务规则计算和写入成功；分开计算的计数规则同样不能直接当作原子提交。本次没有实现或验证替代协议，也没有故障注入验证原生实时写入链路。

因此应保留 catalog 精确匹配、未知表达式/参数回源、最近 60 秒回源、缺口回源、revision 隔离和历史失效。记录值的覆盖证明需要下一步专门设计；不应直接删掉 `monitoring_perses_complete`。

## 替换范围与收益边界

| 部分 | 判断 |
| --- | --- |
| 定时计算、规则配置、显式历史 replay | 可以交给 vmalert；本次已验证 replay 的计算与写入 |
| 多步长、有限筛选、精确格点读取 | 可以保留为生成器和薄读端适配；本次覆盖当前 catalog |
| 完成标记、写入可见性、停机缺口、历史修正 | 尚需设计与验证，不能视作已被记录规则替代 |
| 查询响应缓存、合并相同请求、准入控制 | 按实际收益决定保留范围，vmalert 不提供当前 API 的等价接口 |
| Perses 前端补丁 | 独立问题；记录规则不替代资源缓存、插件加载、时间继承和后台暂停 |

收益主要是减少自建计算调度和回填维护，而不是减少必须计算的表达式数。完整保持当前口径仍有 126 条规则；按 group interval 计算，理论约 6.185 次表达式求值/秒，不能把“原生”直接当作减负或加速证据。现有 worker 会按负载补算，并在在线查询期间让出执行机会；vmalert 的并发与资源限制需要实际测量。

建议下一步实现 CPU 小范围候选：保留当前读取 API 和精确回源规则，将生产结果来源接到 vmalert，并完成覆盖协议、停机/失败/部分写入测试。通过后在新旧结果历史覆盖一致的条件下，对受影响面板执行 1920×1080 冷/热缓存、各时间范围及页面/旁路查询延迟对照，再决定是否迁移 DCU/A3。原表达式全量回源不是当前预计算的性能基线。

初次评估未执行该浏览器性能矩阵、生产负载对照或发布，不据此关闭 issue，也不声称更快。后续已完成 [CPU 小范围候选](vmalert-cpu-candidate.md)，包含完整性读取协议、原生实时/故障验证和本地 1080p 功能对照；生产部署和完整性能准入仍未执行。
