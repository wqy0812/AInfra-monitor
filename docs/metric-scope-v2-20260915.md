# 原生请求指标改造与发布

日期：2026-09-15，北京时间。代码已修改、服务已部署并完成线上数据验收；未执行 Git 提交。

## 最终口径

正常客户端以流式为主，后端使用原生统计，不强制筛选 `is_streaming=true`。接受精度测试期间非流式样本混入，对 TTFT、ITL 等延迟分布产生影响。

| 指标 | 当前口径 |
| --- | --- |
| A3 / vLLM 请求数、Token、队列、TTFT、ITL、E2E | 原生整体统计；无流式标签仍参与 |
| DCU / SGLang 请求数、Token、队列、TTFT、ITL、E2E | 原生统计；合并兼容的流式分组，保留来源身份及异常检查 |
| DCU ITL | 按输出批次间隔 / 新增 Token 数计算的平均间隔，未区分流式 |
| 网关请求量、画像、用量、总耗时、生成结果、错误、阶段和最老请求 | 包含流式及非流式，`request_scope="all"` |
| 网关首增量、首输出等待、流停顿及流观察未知 | 仅实际流式输出，`request_scope="streaming"`；非流式不补首增量 |
| 非流式到达数 | 保留独立计数，`request_scope="nonstreaming"` |

新后端请求派生字段采用 `request-metrics-v2`，资源和缓存仍采用 `v1`。新独立水位从发布时开始写入，不迁移、不重建旧请求历史，不回退到旧 `request-streaming-v1` 或旧请求 `v1/latency-v2`。底层旧文件保留作为回退材料。

网关使用 `counters-metric-v2.json` 和 `events-v2-*.jsonl`。画像 API 路径、`gateway-profile-v1` 结构保留，声明 `request_scope="all"` 和 `scope_policy="metric-v2"`。一般计数和首增量共享新的计数生命周期，但首增量样本数独立。

## 修改与部署范围

| 部件 | 部署结果 |
| --- | --- |
| dcu1 aigate | 22:17:23 完成切换；仍选择 `dcu`，route_version=3 |
| A3-gate aigate | 等待评测自然结束及网关空闲，22:17:35 完成切换；仍选择 `a3`，route_version=3 |
| test4 monitoring-api | 初次切换后补充跨升级画像边界修正，最终版 22:25:24 完成切换 |
| test1 code-eval-web | 22:18:41 完成静态文件版本切换；只覆盖 `app.js`、`index.html`、`request-profiles.js` |
| Perses | 更新 9 张看板的查询及说明，保留两个项目、16 张看板、172 个面板及布局 |

模型服务、评测引擎、路由、维护状态和配置保持原样。网关两侧的受保护容器/Pod 身份与启动状态验证一致；Web 切换时评测引擎 ID 和启动时间不变，维护模式仍为 false。

所有远程连接、命令和传输均通过 SSH MCP。发布前已告知新增目录。自动审批拒绝了保存完整 Docker 元数据的最初方案，实际发布仅持久化必要身份、镜像、时间和哈希；运行配置只在进程内读取并传回 Docker，原容器保留供回退。

### 最终版本

- 两套网关二进制 SHA-256：`676da0c48946b87ee9ac0906210993ae102d7fa5c681c1efa03d1ec3e9cf562a`。
- monitoring-api 镜像：`sha256:160ae0fbe57dbc6f744cd653317fecc386100eb1658e344b72cd3b3ece83a78b`。
- monitoring-api 容器：`e40f6d46b56ec62861c2e5b6f49b0f84dc885c8f4f71b3d0a611ac943d1f90a1`。
- code-eval-web 镜像：`sha256:20194b114ca7cb3f33ce416f22edef06879278acdc68efb0de67520dc7f33981`。
- code-eval-web 容器：`9383d1ffab4f1d509aee43271e5613d7f63dcd46360f0312074c62616c310fa1`。
- monitoring-api 的 15 个运行源码文件与最终发布目录逐项 SHA-256 一致，已同步 `/data2/monitoring/release/monitoring`。
- Web 实际 HTTP 返回的三个静态文件哈希与浏览器验证版本一致。

## 验证结果

- 网关：`go test -race -timeout 60s ./...`、`go vet ./...` 通过。
- 监控与看板：157 项 Python 回归通过；平台画像接口 5 项回归通过。
- 独立 VM：140 项通用查询语义检查通过；另覆盖首增量/总耗时不同样本集合、首增量与全量起点的标签匹配、跨升级旧样本排除。
- 1920×1080 Playwright：DCU/A3 切换、统计范围说明、全量画像、首增量限定、JSON 导出和页面错误检查通过；未测试窄屏。目视检查了桌面截图。
- 真实 VM 的只读候选回放确认 A3 无标签指标可以计算，空闲请求量为有效零、无样本分位数为空。
- 两套网关分别发送 1 次非流式和 1 次流式短请求，均返回 HTTP 200。每套增量均为：请求数 2、总耗时样本 2、首增量样本 1、非流式请求数 1、输入 Token 16、输出 Token 6，与响应 usage 一致。
- 另各发送一次短流式请求验证成熟采样窗口，固定验收时刻 `1789482575` 的 8 项延迟图都有非零测量，VM 与 Perses 代理一致。
- 更新看板执行 288 组 VM/Perses 范围查询对照，全部一致。其中 61 组为空，不把相同空结果解释为有数值；上述 8 项另有非零验收。
- 最终两环境最近一小时画像的计数状态均为 `ok`，首增量均值有效；monitoring-api 健康状态为 `ok`。

固定验收窗口的 P95（秒，仅用于证明查询有真实数值，不是模型性能结论）：

| 环境 | 后端 TTFT | 后端 ITL | 后端 E2E | 网关首增量 |
| --- | ---: | ---: | ---: | ---: |
| DCU | 0.39 | 0.098 | 0.59 | 0.4875 |
| A3 | 0.2425 | 0.04875 | 0.285 | 0.975 |

### 跨升级边界修正

旧网关首增量序列与新版沿用相同指标名和 streaming 标签。原画像查询会将新全量生命周期之前的旧样本标为 `missing_lifecycle`，使最近一小时首增量为空。

修正后按每个采集来源的新 all-request 计数起点排除旧样本；起点之后真正缺少生命周期的记录仍保留异常判断。已用独立 VM 的跨升级夹具和真实 VM 最近一小时查询验证，没有迁移或补写历史。

## 证据与回退

本地验收证据：[work/metric-scope-v2-20260915/evidence](../../work/metric-scope-v2-20260915/evidence/)。桌面截图和发布工具位于该专题目录。

远端目录：

- dcu1：`/data4/aigate/evidence/metric-scope-v2-20260915`，候选二进制在 `/data4/aigate/releases/metric-scope-v2-20260915`。
- A3-gate：`/var/lib/ai-gate/evidence/metric-scope-v2-20260915`，候选二进制在 `/var/lib/ai-gate/releases/metric-scope-v2-20260915`。
- test4：`/data2/monitoring/releases/metric-scope-v2-20260915`；最终 API 修正在其 `profile-boundary-fix` 子目录。
- test1：`/data2/code-eval/releases/metric-scope-v2-20260915`。

旧网关二进制路径保存在各自 `baseline.json` 的 `old_path`。回退前通过 SSH MCP 重查在途、当前二进制和路由，DCU 恢复原 current 指向，A3 恢复 Deployment 的 binary hostPath；不回退管理状态或改变维护模式。

API 最新修正前的容器为 `monitoring-api-metric-v2-rollback-1789482314`。通过 SSH MCP 执行最终修正目录内 `container_release.py api rollback <最终修正目录>`，可撤回本次边界修正。初次原生指标发布前的旧容器为 `monitoring-api-metric-v2-rollback-1789481872`；完整旧口径回退还需要恢复对应看板查询与持久化源码，并按依赖顺序执行。

Web 旧容器为 `code-eval-web-metric-v2-rollback-1789481913`；通过 SSH MCP 执行其发布目录内 `container_release.py web rollback <发布目录>`。

看板逐项旧定义和候选保存在 `panel-journal.json`。回退时先确认当前 spec 与本次候选一致，再按 project/name 恢复旧 spec；不覆盖并发编辑。旧 VM 数据未删除，测试夹具使用独立存储并已清理。
