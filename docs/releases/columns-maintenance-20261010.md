# 合并看板的查询维护兼容修复（2026-10-10）

修复看板合并后查询维护流程仍按旧公开 ID 定位资源的问题，同时提交此前已部署的看板收敛改动。审核原始发现见 [审核记录](../../reviews/2026-10-10/monitoring-code-review.md)。

## 行为

- catalog、物化 query ID、revision 和生成器清单继续使用原逻辑分区身份；现有预计算和历史水位无需迁移。
- 查询精简、查询合并、A3 revision 更新、非目标探针及浏览器验收定位实际公开看板和带分区前缀的面板键。候选、资源摘要、整页测量和 API 写回保持公开结构。
- 生成器事务将公开变更映射回原分区文件；冻结文件检查、整张看板并发检查、写入日志和失败保留继续生效。
- `dashboard_columns.py` 纳入查询维护事务的安装与来源摘要。修复故障保留测试的最小夹具隔离，并增加完整合并看板的响应丢失回归。

## 本地验证

177 项相关回归通过，无失败或跳过：包括源/合并快照的四批查询改写、catalog 一致性、A3 revision 准备和发布、公开资源写回、源文件安装、并发修改拒绝、响应丢失保留、生成器独立安装及原有发布边界。

Node.js 22.23.2 语法检查、21 个相关 Python 文件的 Python 3.7 语法兼容检查及 `git diff --check` 通过。未改变查询算法、采集或存储，本次不运行真实 VM 语义/性能矩阵或前端界面验收。

## 发布与验收

通过 SSH MCP 确认实时目标 `test4`。发布前读取在线资源：3 个项目、21 张看板、249 个面板与本地公开资源完全一致；三个环境健康为 ok。核对 70 个运行时模块及分区资源的发布前 SHA-256，均与上一版本或看板收敛发布包一致。

代码提交 `0b9b20f` 已推送至 `origin/main` 并部署到 test4。完整源码安装在 `/data2/monitoring/releases/columns-maintenance-20261010/source/`，后续可从该目录使用 `python3 deploy/release.py`。固定源码归档 SHA-256 为 `777a01447050d955692caf9da781b12b57fdafa4b6dd2a9c1ff55657f526a2f1`。

通过统一 `runtime apply` 入口同步生成器，日志记录 79 项文件安装/退役检查；实际字节变化仅为 `dashboard_columns.py`、`query_acceleration.py`、`acceleration_catalog.py`、`acceleration_publication.py`、`query_slimming.py` 五个文件。每项原始字节和安装字节保存在 `runtime-install.json`，安装前重新核对发布前摘要与线上资源，安装后逐项核验。

验收结果：

- 线上 3 个项目、21 张看板、249 个面板的完整资源与部署前一致；从线上快照重新生成再合并，与当前公开资源一致。
- 18 个 catalog 目标均可从公开看板定位。以原 catalog 生成的 A3 候选仅变更 8 个 A3 条目，保留全部逻辑 ID 及其他组原条目；三批已发布网关改写均幂等。
- 使用真实快照运行实际 A3 revision 准备函数，在本次 `evidence/rehearsal/` 中生成候选，并对生成器副本执行计划与安装。验证 8 个面板的源文件写回，其中两个正确定位到 `model-monitoring`；未创建旧公开入口。演练禁止线上资源写入，候选未发布、实际 catalog 和物化水位未变更。
- 三个环境健康为 ok，验收时数据年龄约 6–11 秒；加速状态无 state error、禁用分组或任务错误。
- Perses 版本为 `0.54.0-perf.3`，数据库健康，未登录资源读取返回 401。

此次只安装维护工具和生成器，无需重启服务，不触发数据源切换、查询候选发布或历史重算。首次解包检查因 Git 归档的顶层 `source` 目录没有末尾斜杠而停止，尚未写入运行时；修正解包校验后继续安装并验收，无回退。

服务器新增 `/data2/monitoring/releases/columns-maintenance-20261010/`，以及其 `source/` 完整源码树和 `evidence/` 证据树；`evidence/rehearsal/` 下包含运行时副本、分区项目文件和 A3 批次证据。`evidence/release` 是指向本次源码 `perses/` 的暂存链接。运行时正式目录仍为 `/data2/monitoring/perses/release/`。

核心证据为服务器 `evidence/before.json`、`runtime-before-hashes.json`、`runtime-install.json`、`resources-after.json`、`health-after.json` 和 `acceptance.json`；本地副本及测试日志位于忽略目录 `work/columns-maintenance-20261010/`。发布记录在部署后以单独文档提交补充，部署的业务源码对应 `0b9b20f`。
