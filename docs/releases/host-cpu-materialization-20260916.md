# A3 主机 CPU 与 I/O 等待聚合（已发布，2026-09-16）

## 问题与范围

A3 两台主机各有 640 个逻辑 CPU，八种 CPU 状态共 10,240 条原始序列。现有 CPU 面板在每次刷新时对完整历史范围逐序列执行 rate、重置、完整性和新鲜度检查，再汇总成两条主机曲线。2026-09-16 只读检查中，默认 1 小时 / 5 秒步长的浏览器 CPU 请求耗时约 4–7 秒；同屏其他请求通常为几十到几百毫秒。CPU 和内存原始样本均正常更新。

本次改变 A3「主机 CPU」与「CPU I/O 等待」两项的计算和读取路径。DCU CPU、NPU、其他主机图表及模型指标继续沿用原逻辑。已发布至 test4；旧查询诊断数据与发布后验收结果分列记录。

## 数据链路

`node_exporter → vmagent → VM 原始数据 → monitoring-api 定时计算 → VM 主机聚合数据 → Perses / history API`

monitoring-api 沿用现有 5 秒循环，每轮仅对水位之后的新增时间点在同一请求中执行原 CPU 忙碌率和 I/O 等待表达式，查询结果每项每台主机一条，共四条。计算仍由 VM 执行，API 负责调度、结果验证及持久化，不把一万多条原始序列导出到 Python，也不在浏览器请求时重新计算完整 CPU 历史。补处理每批最多 60 个五秒点，不重复计算模型指标的 80 秒预热区间。

聚合值使用 `monitoring_chart_value/valid`，CPU 独立标记 `schema="host-cpu-v1"`，忙碌率路径为 `nodes.prefill.cpu` / `nodes.decode.cpu`，I/O 等待路径为 `nodes.prefill.cpu_iowait` / `nodes.decode.cpu_iowait`，新增 `node="a3-1|a3-2"` 标签支持 Perses 节点筛选。现有模型统计水位和导入成功后推进水位的规则不变。

两项原始计算表达式分别与原 A3 CPU / I/O 等待面板在 5 秒查询步长下逐字一致：保留 1 分钟 rate、idle/iowait 扣除、guest/guest_nice 排除、逐序列计数重置和 12 个样本检查、主机启动时间与 up 检查。忙碌率没有改成简单的逐核心百分比平均；I/O 等待仍按逐核心 iowait 的增长率取平均，两项都没有先聚合原始计数再检测重置。通过独立字段标签在一个 VM 请求中合并，两项有效性分别保存。沿用原面板的逐序列有效性判定，不将本次优化描述为新增的全主机拓扑完整性校验。

计算请求设置 `latency_offset=1ms`、`nocache=1`，避免 VM 默认 30 秒查询延迟修正把刚计算的单点查询变为空结果。查询仍有源样本新鲜度与完整性检查。参考 [VictoriaMetrics 查询延迟说明](https://docs.victoriametrics.com/victoriametrics/keyconcepts/#query-latency)。

CPU 查询独立限时 4 秒。查询失败只使本批 CPU 标记无效，模型吞吐、时延、缓存仍可处理和写入。正常零值与无效值由 valid 区分；身份错误、重复输出、非有限值和范围外百分比不发布为有效 CPU。导入失败不推进整个批次水位。

## API 与图表

- `/api/monitoring/latest?environment=a3-vllm`：`nodes.<role>.host_cpu` 提供 `percent`、`iowait_percent`、`status`、`field_status`、`query_status`、`computed_at`、`window_seconds=60` 和 `schema`。`computed_at` 是计算时间点，不冒充原始采集时间。外层最新快照的过期限制仍有效。
- `/api/monitoring/history?environment=a3-vllm`：`points[].nodes.<role>.cpu` 和 `cpu_iowait` 直接读取已保存的聚合值；`gap_before` 保留降采样区间的缺样标记，不回退到旧 A3 CPU `v1`。
- `/health`：新增 `environments.a3-vllm.host_cpu`，分别报告查询状态与各主机数据可用性。未改动原有 DCU 总体健康判断。
- Perses 只更新 `a3-monitoring/a3-hosts` 的 `p0` 与 `extra-iowait` 两个面板查询、数据来源说明；保留布局、单位、线型、图例和节点筛选。面板读取有效且未过期的聚合点；较大步长要求整个区间 valid 完整，避免跨无效点连线。

## 历史与发布

新聚合历史从发布后正常处理的时间点开始积累。原始 CPU 历史仍在 VM 中，本次不删除、不改写、不补零、不自动回填。发布后的主机 CPU 图在聚合数据起点之前留空；回填旧历史需要单独限定区间和计算资源。

发布应先更新 monitoring-api，确认两台主机的两项指标均产生有效 `host-cpu-v1` 点且循环保持更新，再从最新线上看板快照仅替换 p0 与 extra-iowait 查询和追加数据来源说明。`perses.host_cpu_panel.extend` 保留其他面板及既有说明。上线前需检查实时源码差异，不能把本工作区所有尚未发布改动一起覆盖。更新图表需要使用当前已配置的 Perses 认证。

保留 API 旧镜像和看板快照可分别回退；派生数据本身无需删除。本地验证之外的正式发布和线上验收结果见下节。

## 验证

`tests/test_host_cpu.py` 覆盖原表达式一致性、节点身份与坏值、schema 隔离、计算区间上限、查询失败隔离、写入失败水位、API 历史读取和缓存。

`tests/test_host_cpu_vm.py` 在独立 VM 1.151.0 中验证正常、零值、抓取失败、缺样、重启、计数器重置、过期和无数据；覆盖 5/15/60 秒展示区间和全部/单节点筛选，并验证最近单点查询与新旧 schema 隔离。测试只接受 `HOST_CPU_TEST_VM_URL=http://127.0.0.1:<port>`，必须指向可丢弃实例。

候选合并表达式在 test4 原始数据上执行了 3 次只读单时间点查询，均返回四条有效序列，耗时分别为 0.9204、0.9101、0.6694 秒。该测量只验证后台新增点计算的可行性，未写入生产派生数据，也不是浏览器读取新聚合历史的耗时。

最终本地回归结果：监控完整测试集及项目看板回归共 **384 项通过**，其中包含独立 VM 1.151.0 的 CPU/I/O 等待和网关查询场景；临时 VM 容器已停止并自动移除。看板结构比较确认仅两项查询与对应数据来源说明变化。证据在本地 `work/host-cpu-20260916/validation.json` 与 `pytest.log`（不入库）。

## 正式发布结果

已于 2026-09-16 北京时间约 16:03 切换 API，随后更新两个面板。使用既有 admin 账号完成 Perses 管理，认证及角色配置保留。

- 镜像：`monitoring-api:host-cpu-20260916-v1`，镜像 ID `sha256:2531eb2b89f77451d56ef39f363a6aae157ace3116c11b605bde38ba1bf73eb7`。
- 候选容器读取正式 VM，拦截全部导入请求；DCU/A3 完整周期分别约 1.467/1.151 秒，通过后切换正式容器。
- 正式查询对照区间：北京时间 16:03:35–16:05:55。两项指标各核对 58 个主机时间点，最大绝对差分别为 4.75e-12 和 6.98e-15。
- 经 Perses 代理、5 次交替顺序的最近 1 小时查询：主机 CPU 中位数 4.148288 秒 → 0.005190 秒；CPU I/O 等待 0.504245 秒 → 0.004727 秒。新聚合数据仅在上线后存在，尚未回填，所以这不是相同完整历史数据量下的长期性能证明。
- A3/DCU 的 1、24、720 小时历史 API 均返回数据；CPU 两项实时值和有效性正常。
- 1920×1080 正式浏览器验收：管理员登录、两项曲线、A3-2 节点筛选和恢复全部节点通过。浏览器捕获到的新 CPU 请求约 22–150 毫秒，I/O 等待约 255–428 毫秒；这些是有限次请求完成时间，不是精确图表绘制时间。
- 18 个项目看板/数据源资源核对通过，仅 A3 主机看板的 p0、extra-iowait 查询与说明改变；Perses、VM、vmagent 容器 ID 和启动时间保持原值，认证文件和配置保持原值。
- 最近 3 分钟的四条聚合指标有效性最小值均为 1；这只是一项短期观测，不表示长期稳定性测试。
- 用户确认保持当前版本后停止追加测试和调整。

远端发布、备份和证据目录：`/data2/monitoring/releases/host-cpu-20260916-v1`。本地主要验收副本：`evidence/host-cpu-20260916/`（不入库）。旧容器保留为 `monitoring-api-local-latest-rollback-1789545813`。

发布脚本为 `deploy/host_cpu_release.py`，依赖同目录的 `local_latest_release.py` 和 `host_cpu_panel.py`。回退时通过 SSH MCP 在该远端发布目录执行 `python3 host_cpu_release.py rollback`，用 `PERSES_PASSWORD` 环境变量提供当前管理员密码。脚本恢复这两个面板及旧 API 容器，保留 VM 原始和已产生的派生历史，并恢复发布前源码和生成器。并发修改会阻止覆盖。回退材料已保留，本次未执行线上回退演练；临时 bearer 文件已在收尾时清理。
