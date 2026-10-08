# Perses 三平台对齐与 A3 Mooncake 发布记录

2026-09-23 完成发布。DCU、A3、XPU 各 10 张看板，共 305 个面板。非缓存看板采用相同公共核心指标及布局，平台差异保留在专属诊断区域；缓存保持各平台实际能力。

## 分类与角色

三个项目均包含网关请求流量与质量、网关在途请求诊断、网关画像存储健康、后端请求性能、Prefill 诊断、Decode 诊断、缓存与存储、主机资源、加速卡资源、采集与监控健康。运行概览和 A3 旧 backend-diagnostics 已删除。

| 平台 | Prefill 节点 | Decode 节点 |
| --- | --- | --- |
| A3 | a3-1 / 122.209.21.24 | a3-2 / 122.209.21.25 |
| DCU | dcu1 | dcu2 |
| XPU | xpu-2 | xpu-1 |

A3 当前是独立 P/D 实例，按实际节点固定阶段查询；NPU 硬件指标通过节点映射角色。无需给 exporter 增加 role 标签。公共角色筛选同时匹配原始 node 与派生 role/path，保留原有节点、设备、engine、rank 维度，不跨 rank 求和。上述映射变化时需要更新生成器 NODES 并重新验收。

后端性能核心包含请求速率、输出 Token 吞吐、TTFT、ITL、E2E；阶段核心包含排队及 KV 池使用率。A3 Inference、TPOT 放公共性能扩展，阶段时延保留真实“本实例”定义，不把 TTFT 或 E2E 改称纯阶段计算耗时。主机核心 10 图、加速卡核心 6 图对齐，保留平台额外指标。

原 252 个非缓存面板全部记录处置，产生 259 条迁移记录。原缓存面板逐项保持不变；A3 原 5 图上追加 10 图。原始查询、固定角色拆分的表达式与数值均有独立对照记录。

## A3 Mooncake 采集

中央 vmagent 新增 mooncake-a3 任务，抓取 122.209.21.24:9003/metrics，标签 environment=a3-vllm、node=a3-1、service=mooncake。仅白名单采集 30 项原生 Master 指标，通过配置检查后 SIGHUP 热加载；没有修改 exporter、推理配置、网关路由或 VM 历史数据，没有重启业务和采集服务。

新增图表：内存容量、内存分配比例、文件层容量、Key 数、客户端数、请求速率、失败速率、驱逐尝试与成功速率、驱逐 Key 速率、驱逐字节速率。Master 是共享服务，这组图不随缓存看板的 P/D 角色筛选改变。

保留缺样留空、数据新鲜度、样本数及重启门控。原生接口未提供 DCU 旧版缓存命中计数语义，因此不新增伪命中率。文件层容量为零不能用于推断 SSD 命中情况。

## 验收

- 本地 32 项测试通过；结构、公共核心一致性、变量完整性、单位及重复生成检查通过。
- 544 项线上新旧查询对照通过，覆盖 30 分钟与 24 小时、默认及 P/D 选择。无数据结果仍保留空白。
- 发布前、发布后各 854 项 VM 与 Perses 代理查询核验通过；发布后 152 项为空，未补零。
- 中央采集连续两分钟 up=1，24 个样本，30 项白名单指标全部出现。
- 30 张看板通过本地隔离 Perses、Chrome 1920×1080 布局、角色筛选与脚本检查。使用合成查询数据，不能替代生产曲线视觉验收；生产数据另由上述查询对照验证。
- 线上读回定义与模板一致（规范化 Perses 自动默认字段后比较）；远程生成器再次生成结果一致，不恢复旧看板。

检查中发现的既有 A3 Decode 7100 端口异常不属于本次修改范围，本次未修复或重启该实例。未主动注入生产故障或切换业务角色。

## 证据、目录与回退

所有远程操作通过 SSH MCP。新增服务器证据目录：

`/data2/monitoring/perses/evidence/alignment-mooncake-20260923`

其下新增 `release/`、`release/projects/`，以及 a3-monitoring、dcu-monitoring、xpu-monitoring 各项目与 dashboards 子目录。运行时文件同步到既有 `/data2/monitoring/perses/release`，采集配置位于既有 `/data2/monitoring/release/deploy/scrape.yml`。

证据包括 before.json、after.json、services-before.json、migration.json、alignment-query-checks.json、audit-candidate.json、audit-after.json、collection-accepted.json、browser-checks.json、runtime-verified.json、journal.json、runtime-install.json 及采集配置前后版本。本地副本与截图位于 `work/alignment-mooncake-20260923/`。

发布先更新/创建目标看板，读回和查询成功后按明确清单删除 A3 旧混合看板。逐资源和逐文件回退日志保留旧值；恢复前检测并发变更。需要回退时经 SSH MCP 执行：

```sh
python3 /data2/monitoring/perses/evidence/alignment-mooncake-20260923/release/alignment_release.py rollback --evidence /data2/monitoring/perses/evidence/alignment-mooncake-20260923
python3 /data2/monitoring/perses/evidence/alignment-mooncake-20260923/release/reorg_runtime.py rollback --evidence /data2/monitoring/perses/evidence/alignment-mooncake-20260923
```

前者恢复看板及采集配置并热加载，后者恢复生成器及模板。回退不清除 VM 已采集历史数据。检查并发修改失败时先人工核对，不能强行覆盖。
