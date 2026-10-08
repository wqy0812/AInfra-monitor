# monitoring 工作区审核（2026-10-08）

审核基线为 `5185015`，范围是 monitoring 当前未提交的代码、配置、测试和生成资源，以及新增的 A3 发布说明。仅审核，未修改业务代码、提交或部署；本次没有远程操作或服务器目录新增。

发现两个问题，建议修复后再提交。

后续状态（2026-10-08 11:31，北京时间）：用户授权修复、提交和部署后，下面两项均已修复，相关回归和线上验收通过。原始发现与复现记录保留；完成记录见 [本次部署](../../deployments/2026-10-08/monitoring-review-fixes.md)。

## 发现

### P1：Perses 运行时安装漏掉新增依赖，安装后生成器无法导入

- 位置：`perses/align_dashboards.py:16`、`perses/a3_cache.py:38`；缺失的安装项位于 `perses/reorg_runtime.py:7-11`。
- 新代码依赖 `a3_mooncake.py`，但 `reorg_runtime.MODULES` 没有包含该文件。使用现有安装器生成独立运行目录后，导入 `align_dashboards` 立即抛出 `ModuleNotFoundError: No module named 'a3_mooncake'`。即使本地源码目录运行成功，也不能证明安装后的运行时可用。
- 已有回归 `perses/test_dashboard_reorg.py::test_installed_generator_imports_topology_without_source_checkout` 直接复现。另在本地临时目录复制实际 MODULES、执行 `sync()` 后单独导入，得到相同错误。
- 影响：通过此安装流程更新或新建生成器运行目录，会阻断共享看板生成/对齐入口。此结论不表示已经核查线上运行目录缺少该文件。
- 建议：将 `a3_mooncake.py` 纳入运行时安装清单，并通过已有独立运行目录回归。

### P2：总容量为零时放行了已用容量大于零的不一致观测

- 位置：`monitoring/a3_store.py:33-38`。
- 新观察器对 `reason == '总容量为零'` 一律保留 used/total。共享的 `capacity()` 先判断 total 为零，再判断 used 大于 total，因此 `used=32, total=0` 不会进入超容量分支。
- 本地合成复现：保持内存层为 `32/128`，将 SSD 最后一个采样改为 `used=32, total=0`，`a3_store.observe(a3.decode_export(rows), 170)` 返回 `status='ok'`、`error=None`，并保留 SSD 的 `used=32, total=0, ratio=None`。
- 影响：配额/挂载变更等产生不一致采样时，容量曲线和派生历史仍会保留不可能的已用/总量组合，且采集状态显示正常，与非法容量留空的约定不符。现有 zero 测试只覆盖 `0/0`，oversized 测试只覆盖总量大于零。
- 建议：仅将 `used=0, total=0` 作为合法零容量；`used>0, total=0` 应清空受影响层、返回 partial/error，并补充这一边界用例。

## 修改内容

1. **A3 Prefill 缓存 Token 贡献**：新增 `monitoring/a3_effective.py`，读取四个 Prefill 实例的原生三类 Token 计数，按连续约 60 秒增量合计。总输入 T=本地计算 C+显存命中 G+外部 KV 命中 M；整体、显存、Mooncake 比例分别为 `(G+M)/T`、`G/T`、`M/T`。校验实例/引擎/模型、计数分区、重置和缺采；原 `ratio/external_ratio` 不改义。
2. **独立历史边界**：`api.py` 加入新序列和查询，使用 `vllm-prefill-source-v1` schema、持久化的启用时间和独立 `effective_cache` 断线标记。启用前不写新序列，历史响应保留空值，不重置原处理水位。
3. **Mooncake 容量**：新增 `monitoring/a3_store.py`，独立读取 Master 的内存/SSD 已用量、总量及 up 状态，不依赖 vLLM 引擎健康。容量不是物理 SSD I/O；没有分层查询计数时保留不可用原因。
4. **采集与看板**：扩展 `mooncake-a3` 原生采集目录为 90 个指标名，并通过 `perses/a3_mooncake.py` 统一生成选定面板。当前 A3 缓存看板为 15 图：5 张引擎图及 10 张 Mooncake 图；引擎面板与 HEAD 完全一致。相对 HEAD，Mooncake 新增 segment、对象平均大小、写入清理、暂存量四图，移除客户端、请求速率、失败速率、内存比例四图。发布记录的 42→15 描述中间已发布状态，本次 Git 净差异是 15→15。
5. **说明与证据**：同步面板描述、指标覆盖历史基线、新增测试及 10 月 5 日/8 日发布记录。`source-manifest.json` 的三个 after 摘要与当前对应源码一致；未据此推断当前在线状态。

## 本次验证

- 相关离线回归：**102 passed, 24 skipped**。范围为 A3、effective/store、历史断线、API 边界、实时回放、主机 CPU、相关 Perses 生成与文档。
- 独立运行时安装回归：**5 passed, 1 failed**，失败为上面的 P1。
- 独立本地 VictoriaMetrics：**51 passed**，其中 22 项是依赖真实 VM 的查询/历史集成场景，其余为同文件中的普通回归。临时 VM 容器和网络均已清理。
- P2 通过额外合成输入直接复现；未添加测试或修复代码。
- `git diff --check` 通过。未执行全量 Python、浏览器或线上验收；以上为源码、本地回归及独立 VM 证据。

复现命令：

```sh
.venv/bin/python -m pytest -q perses/test_dashboard_reorg.py
bash scripts/test_local_vm.sh -q tests/test_a3_effective.py tests/test_a3_store.py tests/test_a3_cache_history.py perses/test_a3_mooncake.py
```
