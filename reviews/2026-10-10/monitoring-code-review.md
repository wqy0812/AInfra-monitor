# monitoring 代码审核（2026-10-10）

审核基线为 `22c164d` 加当前未提交改动。重点检查 Perses 看板合并及其生成、发布、加速维护依赖，并检查 monitoring 后端的采集重放、历史查询、查询缓存、加速结果发布和容器替换路径。以下结论来自本地源码、回归和合成复现，不代表全量代码无缺陷或当前线上验收。

发现一个 P2 功能问题，另有一项现有回归失败。建议修复后再提交。

后续状态：用户授权修复、提交、推送和部署后，下述问题已修复，177 项相关回归通过。代码 `0b9b20f` 已推送并部署到 test4，真实资源快照的维护候选和副本安装验收通过。原始发现和当时的失败证据保留；结果见 [维护工具修复](../../docs/releases/columns-maintenance-20261010.md)。

## P2：删除原看板后，加速维护入口无法定位目标面板

- 本次引入位置：`perses/project_release.py:255-257` 将分区源看板加入线上删除列表；`perses/dashboard_columns.py:95-96` 将它们替换为公开合并看板。
- 受影响入口：`deploy/perses_acceleration/query_release.py` 的查询审计、`materialized_release.py:216` 的 A3 revision 更新准备，以及直接针对线上快照的 catalog 构建。
- `query_slimming.prepare()`、`acceleration_catalog.build()` 和 `materialized_release._prepare()` 仍按 `gateway-requests`、`backend-performance` 等原看板名和原面板键查找资源。合并后的快照只含 `gateway-monitoring` / `model-monitoring`，面板键也增加了分区前缀。新增的 `expand()` 仅接入 `project_split.build()`，没有接入上述维护入口。
- 本地用 `columns(read_resources(...))` 构造实际发布结构后，四个查询批次均报 `ValueError: Missing batch panels`。A3 批次缺少 `backend-performance` 下两个直方图面板；同一组源码在合并前可以正常准备八个 A3 改写。直接构建 catalog 报 `StopIteration`。
- 进一步调用实际维护函数，并仅替换线上快照读取、前置环境检查和网络入口，复现 `query_release.audit(..., 'a3-histograms')` 报缺面板，`materialized_release._prepare(..., 'a3', old_catalog)` 在第 216 行报 `StopIteration`。未发起远程请求，也未执行资源写入。
- 影响：现有面板的查询保留回归通过，但文档中仍支持的后续 A3 查询改写与 revision 发布无法启动。这不是当前加速查询已经失效的证据。
- 建议：为维护流程统一建立“逻辑源看板/面板 → 公开看板/带前缀面板”的映射，并同时适配读取、候选变更、写回及并发校验。运行时源文件和物化 query ID 可继续保留原身份。仅对读取做展开不足以保证后续写回到正确公开资源；应补充从合并快照进入维护流程的回归。

最小只读复现（仓库根目录）：

```sh
PYTHONPATH=perses .venv/bin/python - <<'PY'
from pathlib import Path
from project_split import read_resources
from dashboard_columns import apply
from query_slimming import prepare

source = read_resources(Path('perses/projects'))
public = apply(source)
print('合并前 A3 改写数：', len(prepare(source, 'a3-histograms')[1]))
prepare(public, 'a3-histograms')
PY
```

预期当前输出：合并前改写数为 8；合并后抛出缺少两个 `backend-performance` 面板的异常。

## 回归失败：故障保留测试夹具未适配新增转换

`tests/test_forward_repair.py::test_dashboard_write_response_loss_keeps_candidate_and_journal[project_release]` 稳定失败，独立执行也可复现。

该测试构造了仅含 `host` 看板的最小资源，并替换了 `validate()`。新增 `project_release.apply()` → `columns()` 会在模拟 PUT 之前抛出 `AssertionError: missing section source`；测试原本预期的 `OSError('PUT response lost')` 没有发生。因此本次不能声称该故障保留回归通过，也不能据此推断真实发布的日志保留已经损坏。建议使用完整的合并看板夹具，或在该单元测试中明确隔离合并转换，同时保留真实发布失败场景的覆盖。

## 验证范围

使用项目 `.venv` 的 Python 3.12.14。两组不同文件的测试合计 **313 passed、1 failed、14 skipped**；单独重跑上述失败用例仍失败，计数不重复累计。跳过项要求可丢弃的真实 VictoriaMetrics，本次未启动此环境。

```sh
.venv/bin/python -m pytest -q \
  perses/test_dashboard_columns.py perses/test_project_split.py \
  perses/test_release_scope.py perses/test_dashboard_reorg.py \
  perses/test_documentation.py tests/test_release_entrypoints.py \
  tests/test_release_datasources.py tests/test_query_rewrite_release.py
# 87 passed

.venv/bin/python -m pytest -q \
  tests/test_api_boundaries.py tests/test_runtime_boundaries.py \
  tests/test_history_cache.py tests/test_history_summary.py \
  tests/test_replay_boundaries.py tests/test_counter_boundaries.py \
  tests/test_perses_acceleration.py tests/test_query_client.py \
  tests/test_acceleration_unit.py tests/test_deployment_tools.py \
  tests/test_forward_repair.py
# 226 passed, 1 failed, 14 skipped
```

`git diff --check` 通过。没有修改业务代码、提交、推送或部署；没有远程操作或服务器新增目录。新增文件仅为本审核记录，原有未提交改动保留。
