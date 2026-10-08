# Perses 查询加速运维

当前仓库发布清单见 [STATUS](STATUS.md)，统计与回源规则见 [设计说明](../../docs/perses-query-acceleration.md)。实际服务器健康和配置须重新读取。旧批次状态与根因记录位于 [发布归档](../../docs/releases/README.md)。

所有升级执行 [停机窗口流程](../../docs/maintenance-window-upgrade.md)。日常 API/看板升级做相关回归和启动检查；加速实现、查询合并或专用数据源切换才执行相应覆盖、正确性与性能专项。失败保留现场与原始证据并修复当前版本，工具没有回退命令，不因发布失败自动关闭加速组。

## 数据与查询

`monitoring/perses_acceleration_catalog.json` 固定 18 个目标表达式及 7 个步长；`perses/acceleration_state.json` 保存已准入合并和分组。未知表达式、未覆盖历史、实时尾部及无效区间按原表达式回源。专用数据源不覆盖默认 VM 数据源。

预计算保留原式的逐序列/分组有效性、缺样、重置和直方图检查。有限的角色、节点筛选从结果过滤，不能放宽查询守卫。每个时间点同时保存数值与完成标记；空结果也必须有完成标记才能证明已计算。

VM 导入完成不等于读回可见。任务冻结 `perses-pending-*.json`，依次写入并核对数值与完成标记；等待时释放计算机会，120 秒仍不一致记为失败。只有完整读回验证才推进 `perses-acceleration-watermarks.json`。批次最多 60 点、单次执行限时 12 秒，重启复用待处理文件，不能清空水位来规避错误。

封存历史的完整响应可在允许缓存时保留 30 秒，最多 128 项/16 MiB；旧缺口和实时尾部不进入该缓存。管理状态变更使缓存失效并隔离在途请求。`/health` 显示水位、积压、计算错误和等待可见状态；未到首个计算点的长步长任务不算积压。

## 日常操作

- API 镜像构建/加载后通过 `python3 deploy/release.py container monitoring-api IMAGE --evidence DIR` 更新。需更新 catalog/配置时在构建输入中携带当前文件，核对运行参数；保留现有 state 挂载和 `PERSES_ACCELERATION_CATALOG` 配置。
- 然后运行 `python3 deploy/release.py acceleration-ready --evidence DIR`：检查同一容器/镜像、三环境处理健康、正常调度与任务积压，最多 90 秒、连续三次失败退出。输出 `api-readiness.json`，失败另存原始错误。该检查不替代专项覆盖/性能验收。
- 管理工具 `admin.py` 支持分组 `enable` / `disable`、有理由的时间区间 `invalidate`、有界补算及临时调度。默认状态目录 `/data2/monitoring/state`；并发操作使用文件锁。失效区间回源，历史数据不删除。发布工具不会自动调用 disable。
- 原始历史被补写或修正时，用 `invalidate --start UNIX --end UNIX --panel ID --reason TEXT` 标记对应区间。管理操作必须针对实际问题，不是旧版本回退替代品。

容器、看板、镜像、生成器、加速批次分别使用新证据目录。后台运行将 stdout/stderr 保存到当批日志；连接中断后读取报告与实际状态，不能盲目重放写操作。

## 预计算分组发布

统一入口 `python3 deploy/release.py acceleration ACTION --group cpu|dcu|a3 --evidence DIR`；`--catalog FILE` 默认使用仓库当前 catalog，必须与被验证 API 一致。按 CPU → DCU → A3 串行处理，每批完成后才获取下一批的新快照：

1. `prepare`：锁定表达式和资源，创建非默认专用数据源，面板仍保持原路径。
2. `audit`：验证所有支持步长的完整 12 小时覆盖、完成标记、有限筛选、24 小时跨历史和非整秒窗口；60/120 秒绘图步长分别做允许/禁用缓存的 41 对固定 12 小时对照。
3. `impact`：按相同负载位置交替执行独立非目标查询，保留完整性能样本及健康记录。
4. 将匹配 catalog SHA 的 `materialized-browser.json` 放入本批目录；`apply` 核对全部准入结果和并发编辑，只切换目标面板，逐项读回并同步生成器。
5. `observe`：正常调度下核验模型健康、组覆盖及实时水位，最多 90 秒；通过后继续下一批。

目标性能标准为相同窗口、步长、缓存模式的 41 对完整样本中，新查询中位耗时严格更低，报告使用 `faster-median-v1`。相等、变慢或不完整都不通过；正确性误差和非目标 5% 门槛保留。旧批次的单次豁免不适用于新发布，固化豁免的发布代码已删除。

DCU 原式可能在 12h/5s 整窗遇到明确的 VM 内存限制。正确性比较会保留整窗错误，再按原始时间网格分段对照；未知错误仍终止。分段结果只证明逐点一致，性能对照仍使用完整窗口，不能把整窗内存错误当性能通过。

同级证据目录中的未完成发布保持阻塞；不得修改基线、删除日志或用旧成功报告跳过。`audit --resume` 只复用指纹和固定窗口均一致的未发布证据。工具只读识别历史回退完成记录，以避免重放旧批次，不生成新回退记录。

## 查询合并及辅助工具

`python3 deploy/release.py merges audit|resume|apply --evidence DIR` 保留完整快照、表达式/资源指纹、并发检查、逐项日志和读回。合并保持原曲线及有效性；JSON 顺序的代理压测不等于浏览器首屏。

| 工具 | 保留用途 |
| --- | --- |
| `admit_when_ready.py` / `continue_materialized.py` | 等待完整覆盖、串行准入及续办已授权批次；不跳过中途失败 |
| `backfill_recovery.py` | 有界补算监督与恢复正常调度；它不代表性能通过 |
| `merge_recheck.py` / `viewport_audit.py` / `non_target_audit.py` | 实际首屏、目标与非目标复验，保留固定版本与完整样本要求 |
| `environment_probe.py` / `backfill_probe.py` / `probe_memory_window.py` | 只读覆盖、旧历史、筛选和内存窗口诊断；报告不代替性能准入 |
| `browser_*.cjs` / `local_browser.py` / `local_proxy.py` / `synthetic.py` | 1080p 浏览器、有限本地代理与合成测试；合成结果不冒充生产性能 |
| `generator_transaction.py` | 生成器字节日志、并发编辑检查和同步；共用的查询说明来自 `docs/perses-query-acceleration.md` |

自动准入/续办仍可读取旧 `shadow-started.json` / `shadow-observation.json`，新流程使用通用容器发布结果及 `api-readiness.json`。为补算监督准备目录时另保存匹配的 catalog。使用 `--observation PRIOR_DIR` 时只允许相同镜像的已通过就绪报告，且仍核验当前健康。等待时长是上限，不是稳定性验收时长。

历史 `release_api.py`、影子监视、DCU 特定恢复、单次豁免发布器和固化批次压测已退役，原文检索见 [退役清单](../../docs/releases/retired-code.md)。
