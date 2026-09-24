# 时间导航与历史缓存发布

2026-09-25 已通过 SSH MCP 部署至 test4；monitoring-api 和 Perses 正式切换、线上核验均成功。切换前 test1 无活动任务和预留，两个 worker 健康且无活动或排队任务。未修改评测服务。

## 版本与回退

| 组件 | 已部署镜像配置摘要 | 保留的旧容器 |
| --- | --- | --- |
| monitoring-api | `sha256:919eb6c809c629ee736790611cd2176ece0c92915a0873b92fa8d616bfcae613` | `monitoring-api-before-time-navigation-20260925` |
| Perses 0.54.0-perf.3 | `sha256:978402ac5154e3ee2cf8c66f244fc7169be74125ef01a5fe9d28c1e8a24764e7` | `monitoring-perses-before-perf3` |

旧容器均已停止且保留，远端和本机候选均已停止。回退仍只能通过 SSH MCP：

- API：在 `/data2/monitoring/releases/time-navigation-20260925` 执行 `python3 release_api.py rollback`。
- Perses：在 `/data2/monitoring/perses/evidence/time-navigation-20260925` 执行 `python3 image_release.py rollback --evidence /data2/monitoring/perses/evidence/time-navigation-20260925 --lock release-lock-perf3.json`。

API 物料包含本目录脚本和清单、`monitoring/api.py`、`deploy/replace.py`、`deploy/container_validation.py`。`prepare` 从原镜像离线构建，再使用精确镜像的一次性 Python 容器运行 `candidate_api.py`；挂载脚本位置为 `/monitoring/candidate_api.py`，不挂载生产 state、不启动物化循环。`switch` 保留原运行配置和旧容器，失败按容器身份恢复。

Perses 使用 `release-lock-perf3.json`，升级/回退基线为 perf.2。归档 SHA256 为 `8d04801254097dad14a42912586de2b13a7f8ad34958346470382e17f6d1cd37`。连接失败曾导致上传中断；按用户重试指示完成 28 段后续分片，校验旧断点、逐段和完整归档后才载入镜像。发布器将证据目录转为绝对路径，避免 Docker 将相对挂载误认为命名卷；此前误建的两个空命名卷已删除。

## 验收结果

- Python 回归 325 项通过，218 项独立 VM 测试未启用；前端 19 项测试通过。
- 本机 1920×1080 Chrome 验收固定窗口暂停、手动刷新、跨项目时间继承、相对范围刷新、隐藏暂停和恢复补查，浏览器无运行时错误。数据源使用合成响应，可见性通过 DOM 事件注入；不据此推断生产指标或真实系统标签页切换行为。
- 本机镜像 manifest ID 为 `sha256:82da4ef09d5f879abc67453ba74fdbce607c4957736d2d392e13af1c214e7700`，其 RootFS 和 Config 已与锁定归档配置对照一致。浏览器报告保留实际本机 ID，并附归档配置摘要证明。
- 用户明确要求“浏览器验收本机就可以”；使用 `--local-browser-validation`，同时要求精确镜像的本机报告、远端候选 API 证据和此项用户指示。远端浏览器与 1800 秒持续观察未执行，发布报告明确记录为未运行。
- API 候选读取真实 VM：三个环境各 361 点，每个环境 20 个并发等待者共享在途查询。正式切换后完整/摘要结果一致，重复摘要请求结果一致；单次核验缓存请求分别耗时 DCU 0.366 秒、A3 0.395 秒、XPU 0.220 秒，仅为当次测量。
- Perses 候选与正式资源核验均通过：DCU/A3/XPU 各 10 张看板、1 个数据源，与切换前内容一致；正式各项目代理的真实 `up` 查询均成功，各返回 31 条序列。健康接口为 perf.3，数据库正常。
- VM/vmagent 未重启；Perses 切换也未重启 API。最终四个监控容器均运行、重启数为 0。API 总体健康，三个环境处理进度均在 30 秒内。A3 prefill/decode 后端源错误发布前已存在且仍存在，原先正常的数据源继续正常。

## 目录与证据

本次在 test4 新增：

- `/data2/monitoring/releases/time-navigation-20260925`
- `/data2/monitoring/perses/evidence/time-navigation-20260925`，含候选数据子目录 `candidate-data`。

凭据、容器配置快照和完整原始证据仅保留在服务器。API 证据为 `candidate-api.json`、`complete.json`、`post-perses-health.json`；Perses 证据为 `candidate-api-validation.json`、`candidate-proxy-validation.json`、`image-publication.json`、`production-verification.json`、`candidate-cleanup.json`，另存本机浏览器报告与用户验收范围记录。

全部部署及线上核验完成后，下载证据副本再次遇到 SSH MCP `Connection closed`，按约束停止远程操作。本地仅下载了 `image-publication.json`、`candidate-api-validation.json`，位于忽略目录 `evidence/time-navigation-20260925`；其余仍在服务器。该下载失败不影响此前已确认的发布结果，后续补下载需要恢复 SSH MCP 文件传输连接。
