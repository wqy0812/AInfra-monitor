# Perses 历史安装与发布记录

从 Perses README 归档的 2026-09-14 至 2026-09-22 记录。文中的“当前”、数量、免登录状态、入口及回退容器均指当时版本，不能作为现行配置。现行操作见 [Perses 说明](../../perses/README.md)。正文中的代码路径仍相对于原 `perses/` 目录。

## 加载优化上线记录（2026-09-16，0.54.0-perf.2）

已完成 [两批加载优化](../../perses/performance/README.md)：修复重复刷新、内置变量初始化、插件按需加载、静态缓存及失败插件重试；13 个图表的 P50/P95/P99 已合并查询，共用原有效性检查。当时镜像配置摘要为 `sha256:c5a17dc68da543e42b3c78618b0c452ea29328a027ec4439b402f3aa79a15bfa`。
发布以最新线上快照生成补丁，保留既有查询匹配条件和图表说明。16 张看板中仅修改 3 张的 13 个目标面板；项目、数据源及其他面板保持一致。本地 `projects/` 是可再生成的模板，发布仍应以线上快照为准，不能覆盖网页编辑。
1080p 生产首屏查询数：A3 概览 23→12、A3 后端诊断 24→6、DCU 请求画像 26→7；插件资源数分别为 157→28、153→24、153→24。最终每面板 41 组配对冷查询，中位耗时降幅 46.6%～72.8%，各面板降幅中位数 61.1%；52 组范围/步长检查中数值一致。这是查询耗时，不是图表绘制耗时；部分 1 小时窗口为空，24 小时历史已验证。
用户在获知候选转发被服务端禁止后明确指示上线，前置浏览器检查按记录转为生产验证。perf.1 完成 1826 秒、121 轮、1452 个成功请求的持续刷新观察，稳定期缓存始终为 27 项、其中 12 个面板查询。perf.2 只修复失败插件重试，另行通过生产首屏、Retry 故障恢复、变量与手动刷新检查；不将 perf.1 的长时间观察标成 perf.2 的 30 分钟验收。
远端证据目录：`/data2/monitoring/perses/evidence/performance-20260916`。原版与上一补丁容器分别保留为 `monitoring-perses-before-perf1`、`monitoring-perses-before-perf2`；候选容器已停止，VM、vmagent、monitoring-api 未重启。详细结果见 [上线记录](../../evidence/performance-20260916/DEPLOYMENT.md)。

## 当前入口与项目发布（2026-09-15）

- [DCU 监控](http://122.247.53.162:18431/projects/dcu-monitoring)：8 张看板、88 图。
- [A3 监控](http://122.247.53.162:18431/projects/a3-monitoring)：8 张看板、84 图。
- 现行资源为 `projects/<project>/project.json`、`datasource.json` 和 `dashboards/*.json`。下文及顶层 `dashboards/` 保留历史定义；现行初始化、生成与发布入口不再读取旧混合定义。
- 当前使用原生后端指标，不按流式标签过滤；精度测试可能混入非流式。请求派生字段使用 `request-metrics-v2`，资源和缓存仍使用 `v1`。网关一般指标使用 `request_scope="all"`，首增量及流观察使用 `streaming`；旧请求历史不混读。
- 混合网关生成监控已拆为每个项目 17 图；A3 原三张看板已迁移。两个项目各有网关基础状态、请求吞吐与画像、后端诊断及共享监控服务。
- [关键指标覆盖与取舍](../../perses/METRIC_COVERAGE.md)、[完整指标对应关系](../../perses/metric_coverage.json)、[图表说明](../../perses/METRICS_GUIDE.md)。`panel_descriptions.json` 使用 project → dashboard → panel 三级键。

### 维护入口

1. `python3 project_split.py`：以当前 `projects/` 资源生成两项目配置；保留线上基线面板，更新本工具管理的扩展图，并将请求派生字段匹配到当前 schema。首次迁移可用 `--snapshot before.json --bounds histogram_bounds.json`，不得用旧的混合项目布局替换现行双项目基线。
2. `python3 seed.py`：按项目创建缺失资源，保留已存在资源。`generate.py`、`generate_a3.py`、`gateway_generation.py` 的 CLI 均转到双项目生成器；旧的专用发布脚本已阻止直接执行。
3. 对显式更新，先告知并创建新的远端证据目录，然后通过 SSH MCP 依次运行 `project_release.py prepare --evidence DIR`、`check_project_semantics.py DIR`、`project_release.py audit --evidence DIR`、`project_release.py apply --evidence DIR`。候选位于同目录 `projects/`；`--resources` 可显式指定资源目录。更新前保存快照、检查服务指纹和并发编辑，更新后读回及对照查询，异常按资源日志回退。
4. `project_release.py audit-published --evidence DIR` 通过两个项目各自代理核对全部查询；`validate.py`、`compare.py` 的 CLI 转到此模式，必须提供 `--evidence`。
5. `project_coverage.py --docs-only` 从当前项目定义同步面板说明和阅读文档，并沿用已保存的历史覆盖清单，无需本地证据目录。`project_coverage.py --inventory inventory.json --before before.json` 用于重建指标映射；该清单以 2026-09-15 的采集基线为界，不代表当前在线覆盖数量。`annotate_panels.py` 的 CLI 也转到该入口。它只更新本地文档，不写服务器。

本次 11 项本地回归、140 次隔离 VM 语义查询、发布前与发布后各 512 组 VM/代理对照通过。CPU 聚合因并行浮点加法存在约 6e-13 的差异，对照严格要求标签/时间戳相同，数值使用 1e-10 相对与绝对容差。API 查询成功不等于所有图都有数据，空白原因与扩展历史验证单独记录。

本次证据与回退：`/data2/monitoring/perses/evidence/project-split-20260915`。完整旧定义为 `before.json`，逐资源变更为 `journal.json`；回退命令为 `python3 project_release.py rollback --evidence /data2/monitoring/perses/evidence/project-split-20260915`，必须经 SSH MCP 执行。回退检查当前资源是否仍为本次写入内容，不覆盖并发修改；保留证据，不改 VM 数据，不重启服务。

以下为历史部署记录。

入口：http://122.247.53.162:18431/projects/dcu-monitoring 。免登录、可编辑；18431 仅放行 `122.0.0.0/8` 和回环来源。仅监听 `122.247.53.162:18431`，没有 IPv6 监听。该 /8 按用户指定，不等同于 RFC1918 私网。

运行：Perses v0.54.0，Docker host 网络，1 CPU / 1 GiB，非 root UID 65532；systemd 管理 `monitoring-perses.service`，依赖 `perses-access.service`。防火墙失败则启动失败。Docker restart policy 为 no，避免绕过 systemd 启动顺序。未执行整机重启测试；已验证服务重启和实际规则失败阻止启动。

## 文件与首次安装

- 远端根目录 `/data2/monitoring/perses`：`release/` 部署物料，`config.yaml` 配置，`data/` 文件数据库，`evidence/` 验收证据。配置只读挂载；数据目录归属 UID 65532。
- `image-lock.json` 分别记录仓库多架构摘要、amd64 manifest 摘要、Docker config/image ID 和压缩包 SHA256。Docker Desktop containerd 的 image inspect ID 与 test4 经典镜像存储 ID 含义不同；安装使用压缩包内 config 内容的 SHA256 核验后按不可变 ID 启动。
- 本地执行 `docker pull --platform linux/amd64 persesdev/perses:v0.54.0`，用 `docker save --platform linux/amd64` 导出，再 gzip；归档在 `../vendor/perses/`。重新打包须重新计算归档 SHA256，不能复用旧校验值。
- 远程操作一律使用 SSH MCP。使用 SSH MCP SCP 上传单个文件及 dashboards 子目录到远端 release；上传镜像归档到远端根目录。注意递归 SCP 会保留来源目录名，上传后先检查目录结构。
- 在 test4 通过 SSH MCP 执行 `python3 /data2/monitoring/perses/release/install.py`，然后执行同目录的 `seed.py`。首次安装拒绝覆盖已存在的容器、配置或 unit。
- `generate.py` 生成四张看板，共 33 个面板。`seed.py` 仅创建不存在的资源，重跑不会覆盖网页编辑。`update_defaults.py` 是显式更新操作，会备份并替换四张同名看板，不能加入开机流程。

## 查询口径

默认最近 1 小时，每 15 秒刷新。派生指标按 `monitoring_chart_valid`、当前观测年龄和查询步长内完整性过滤；`connectNulls=false`。内置查询步长变量为 `$__interval`，不能写成 `$interval`。长范围查询遇到窗口内任意无效观测会留空，可能比原始细粒度曲线更稀疏。

输出 Token 吞吐的上游有效性当前为 0，故保持空白；Decode Token 吞吐另有有效来源。图表不重算上游无效字段，不补零。延迟在没有有效请求样本的窗口中也留空。Store 查询命中比例不是模型 Token 命中率或 SSD 物理 I/O；SSD 容量表示配额。

主机/DCU 直接读取带来源状态和新鲜度过滤的原始指标；设备以节点和卡号区分。磁盘曲线逐设备展示，不对逻辑盘和物理盘求总和。网关速率使用 1 分钟窗口，要求足够样本、无计数重置、无生命周期变化且采集连续。

数据源为 Perses Prometheus 插件的 HTTPProxy，连接 `127.0.0.1:18428`；允许 query/query_range、标签、series、metadata 读取。非查询端点实测返回 403。免登录用户具有编辑能力，也可以修改数据源，因此此接口限制不是针对受信编辑者的权限隔离。

## 验证和运维

通过 SSH MCP 在 test4 执行：

- `python3 /data2/monitoring/perses/release/validate.py`：所有面板的 15/60 秒步长查询、保护容器及采集状态。
- `python3 /data2/monitoring/perses/release/compare.py`：同时间戳代理/VM/API 对照及非查询端点拒绝。
- `python3 /data2/monitoring/perses/release/check_semantics.py`：独立临时 VM 的有效值、零值、无效值、缺样、过期、生命周期和无流量场景；结束时清理临时容器和目录。

升级前停止 Perses 并备份 data、config 和镜像锁；仅替换本容器，使用 `systemctl restart monitoring-perses` 启动。备份必须保留配置中的加密密钥及数据库。默认初始化不会覆盖用户修改。

回退：先 `systemctl disable --now monitoring-perses.service`，确认容器停止；再 `systemctl stop perses-access.service`，执行 `release/access.sh remove` 移除专用链。保留 data/config/evidence；其他服务、端口规则和 VM 数据不受影响。若要恢复启用，重新启动 monitoring-perses.service 会先重建规则。

本机 Surge 已为 test4 的 18431 端口新增通过现有 Jump-SOCKS 的规则；未扩展其他端口路由。原配置备份在 `/Users/qyw/.codex/backups/perses-20260914/surge-before.conf`。其他客户端需要具有到 test4 的内网/跳板路径。

## A3 独立看板（2026-09-14）

新增 `a3-overview`、`a3-hosts`、`a3-cache`，从现有项目的看板导航切换。`generate_a3.py` 仅生成 A3 定义，`publish_a3.py` 仅创建缺失的 A3 看板并核对服务端默认值；遇到不同的现有定义保留用户编辑。不要用全量 `update_defaults.py` 发布本次 A3 新增。

运行概览和缓存读取 `environment="a3-vllm"` 的派生数值及有效性；主机页读取现有 `node-a3` 原始指标。窗口内缺样或重置留空，前缀缓存使用 Token 分母，外部命中指跨实例 KV 共享。本次不部署 NPU 采集器。完整验收位于本地 `../evidence/a3-switch-20260914/README.md`（不入库）。


## DCU 时延序列切换（2026-09-14）

运行概览的 9 条 E2E / TTFT / ITL 分位数查询改读 `schema="latency-v2"`。仅替换查询中的时延版本，不覆盖用户的其他看板编辑，不重启 Perses。缺失或无效窗口仍留空，其他看板和 A3 保持原版本。旧定义备份在 test4 的 `/data2/monitoring/evidence/latency-v2-20260914/dashboard-before.json`。

## 双网关生成监控（2026-09-14）

新增 [网关生成监控](http://122.247.53.162:18431/projects/dcu-monitoring/dashboards/gateway-generation)，六项图表各两条固定颜色查询：DCU 主机网关为蓝色，A3 主机网关为橙色。按采集 environment 汇总后端、模型、流式及结果维度；DCU 网关转发到 A3 时仍归 DCU 网关曲线。同一请求经过两层网关会分别记录，不能相加作为全局请求量。

图表为生成结束速率、后端错误速率、后端错误率、客户端取消占比、客户端断开占比和未知结果占比。默认 1 小时、15 秒刷新；速率/占比窗口 1 分钟。分母包含全部生成结束结果，不含入口拒绝、发现或探测。未出现类别和无分母时留空；有连续样本的空闲速率为零。

`gateway_generation.py` 仅生成 `dashboards/gateway-generation.json`。每条底层序列检查 1 分钟及绘图步长内的采样、重置和新鲜度，再汇总；任一已出现的参与序列不完整则整个网关汇总留空。门控使用新指标的 `aigate_error_metrics_start_time_seconds`，不回填升级前历史。固定查询颜色遵循 [Perses TimeSeriesChart querySettings](https://perses.dev/plugins/docs/timeserieschart/model/) 模型，并通过当前服务器 API 校验和读回。

`check_gateway_generation.py` 在 test4 启动独立临时 VM（回环 18538、独立临时目录），验证 39 场景后清理；不会向生产 VM 写入数据。`publish_gateway_generation.py DIR` 要求 DIR 包含看板 JSON 和通过的 semantics.json，先核对生产 VM 与 Perses 代理的 24 组查询，再仅创建新看板。相同定义重跑保留，已有不同定义报冲突，不调用全量 update_defaults。

本次发布载荷、旧看板快照和服务指纹保存在 test4 `/data2/monitoring/perses/evidence/gateway-generation-20260914`。本地记录位于 `../evidence/gateway-generation-20260914/README.md`（不入库）。回退只删除本次新建的 gateway-generation 看板；保留其他看板、数据源和 VM 数据。API/数据查询验收已通过；同日重置 CUA 会话后浏览器通道恢复，七个面板的布局、固定颜色和无数据状态已完成目视核验。

已按用户要求移除“距错误指标统计起点时长”面板。统计起点指标继续用于查询有效性过滤；当前看板为六项双网关监控。初始七面板验收记录保留为历史证据。

## 实时等待与流停顿扩展（2026-09-15）

gateway-generation 已扩为 17 项，顶部新增 11 项实时诊断，保留六项原有生成/错误图表。等待仅统计流式；停顿按 ≥5/15/30/60 秒累计分档；最老请求和处理阶段覆盖流式及非流式。

`gateway_live.py` 生成新区域，`publish_gateway_live.py DIR` 按固定面板 ID 合并当前定义，保留已有编辑并读回；旧的 create-only 发布脚本是历史入口，不用于本次扩展。新 Gauge 校验采集、新鲜度、进程起点和 backend 分组数量，避免系列消失后只汇总剩余后端。正常空闲显示零，缺失和跨重启留空，阶段切换不跨线连接。

`check_gateway_live.py DIR` 在 test4 使用隔离 Docker VM、回环 18539 和 DIR/fixture-storage，覆盖带 node/service 标签的 42 个场景，测试结束清理实例及数据；不得指向生产 VM。生产查询对照使用固定历史窗口、禁用查询缓存，避免近期写入与缓存导致两次查询看到不同快照。本次 126 次隔离查询、96 组 VM/代理对照通过，证据位于 `/data2/monitoring/perses/evidence/gateway-live-20260915`。

### 2026-09-15 本地后续调整（未发布）

当前候选 `gateway-generation` 共 18 个面板：流式实时诊断 11 项、流式生成/错误 6 项，另有唯一的 **非流式请求数**。非流式不进入其他 17 项。新面板显示最近 1 分钟新增请求的窗口估算数，两网关分线；正常空闲为零，采集缺失、重启及窗口未满留空。流式处理阶段去掉解析前的 `receiving_request`，`reading_response` 仅指流式请求的错误正文。

网关查询显式筛选 `request_scope`；后端请求曲线读取 `request-streaming-v1`，不回退到旧混合口径。上述早先发布记录描述的是当时线上版本。此次仅修改本地定义和测试，未执行发布脚本、远端 VM 查询或浏览器验收。

### 流式口径同步发布入口（2026-09-15，已发布）

线上核查确认两台 aigate 已暴露 `request_scope="streaming"` 和独立的 `aigate_nonstream_requests_total{request_scope="nonstreaming"}`，但网关生成看板仍为 17 图且查询未筛选 scope。

`publish_gateway_scope.py DIR` 用于本次两张看板的同步：

- `gateway-generation`：合并最新实时区域，新增非流式请求数，同时给原六张生成／错误图补充流式筛选；保留这六图的标题、样式及布局相对位置。
- `gateway`：仅给 aigate 查询补充流式筛选和说明，保留布局与标题。
- 先保存完整旧定义、候选定义和监控服务指纹，要求两类新指标在两个网关均有新鲜采样；校验所有查询在 15/60 秒步长下的 VM 与 Perses 代理结果一致，检测并发编辑后才 PUT。读回核对并确认其他看板及服务容器未变。
- DIR 必须预先创建且没有 `scope-before.json`。须先在隔离 VM 完成 `check_gateway_generation.py` 和 `check_gateway_live.py DIR`，取得两个通过报告。生成指标夹具还覆盖非流式请求数，以及大流量错误 scope 不混入流式图的验证。
- 备份为 DIR/`scope-before.json`；回退仅恢复其中这两张看板的 spec，不恢复其他资源，不修改 VM 数据。

用户明确授权后，已通过 SSH MCP 上传、验证并更新这两张看板。最终为 gateway-generation 18 图、gateway 8 图；生成/非流式计数语义验证 39 组场景、273 次查询，实时状态语义验证 42 组场景、126 次查询，生产 VM/Perses 代理对照 112 组全部通过。读回定义一致，其他看板及监控容器 ID/启动时间不变，隔离测试容器已清理。远端证据和旧定义保存在 `/data2/monitoring/perses/evidence/gateway-scope-20260915`，本地验收报告见 `../evidence/gateway-scope-20260915/`。

最近已落盘的 5 分钟窗口中，两网关等待数及非流式请求数各返回 21 个有效点；最老请求也均有数据，阶段切换处按既有规则留空。即时查询最末端可能因样本尚未齐全而留空，不改为补零。本次完成 API/数据验收，未做浏览器目视验收。早先“本地后续调整（未发布）”段落保留为发布前状态记录。

### 基础状态速率图标签匹配修复（2026-09-15，已发布）

上线后排查发现 `gateway` 的 p1/p5/p6 速率查询在生命周期门控内部仍使用默认 `and`：`aigate_profile_counter_start_time_seconds` 带 `request_scope`，`up` 不带，导致结果恒为空。相同历史时间点，原始请求计数、rate、采样数和起点均有效；将该门控的两处 `and` 改为 `and on(job,instance)` 后，三张图均恢复测量值。

`generate.py`、`publish_gateway_scope.py` 和本地 `dashboards/gateway.json` 已修正。新增 `fix_gateway_rate_matching.py DIR` 只修复 p1/p5/p6，要求 15/60 秒查询窗口中均有至少三个真实数据点，且 VM 与代理一致；保留旧定义、检测并发编辑并读回。常规 scope 发布入口也新增这三图不得全部为空的校验，避免“两个数据通路同为空”被当作完整验收。

用户授权继续修复后，已通过 SSH MCP 上传并发布三条查询修复。15 秒步长各 41 点、60 秒步长各 11 点，VM/代理一致，读回一致；其他看板与监控容器保持不变。证据为 `../evidence/gateway-scope-20260915/rate-matching-publication.json`。

随后检查全部 8 张线上看板、66 图、114 查询，按默认筛选条件对最近已落盘 30 分钟执行 15/60 秒步长共 228 次范围查询。100 条查询有数据，14 条空白已逐项核对：6 条派生查询对应源 valid 不为 1；6 条查询缺少对应错误类别序列；2 条 A3 占比的有效分母在检查窗口内全为零。未发现其他同类标签不匹配导致的整体空结果。详见 `../evidence/gateway-scope-20260915/audit-summary.md`；此次为 API/数据验收，未进行浏览器目视验收。

### 图表指标说明（2026-09-15，已发布）

全部 8 张看板的 66 个图表已准备结构化中文说明：指标含义、Y 轴单位、曲线与范围、时间口径、零值与空白。原生入口为图表标题区域的信息按钮（panel description）。本地完整阅读版见 `METRICS_GUIDE.md`。

`annotate_panels.py BEFORE_JSON PATCH_JSON` 依据当前定义和已核对的指标语义生成说明补丁；`panel_descriptions.json` 只含看板 ID、面板 ID 与说明文字。发布时只修改各 Panel 的 `spec.display.description`，必须验证查询、坐标轴、布局等字段原样保留，并读取服务端定义核对。旧定义备份在 test4 `/data2/monitoring/perses/evidence/gateway-scope-20260915/annotations-before.json`。重新生成看板后，应重新应用对应说明并按新查询口径复核，避免生成过程覆盖注释或沿用过期说明。

用户授权部署后，已通过 SSH MCP 将说明发布到 test4。8 张看板、66 图全部读回一致；逐项验证仅说明字段变化，查询、坐标轴、布局等其他字段保持原样。监控服务容器 ID 和启动时间不变。发布记录见 `../evidence/gateway-scope-20260915/annotations-publication.json`；本次即时备份为 `/data2/monitoring/perses/evidence/gateway-scope-20260915/annotations-deploy-before-1789469600158010494.json`。

浏览器抽查已通过：打开“网关基础状态 → 请求到达速率”的信息按钮，已看到完整中文说明、请求/秒单位和空白解释，弹窗布局可阅读。全部 66 图的内容通过 API 逐项读回；未逐一打开所有图表弹窗。

## 原生指标口径发布（2026-09-15）

本次更新 9 张看板的查询和说明，仍为两个项目、16 张看板、172 个面板，布局不变。`metric_scope.py` 统一处理网关的逐指标范围，后端不增加流式条件。首增量直方图的分组起点采用 `ignoring(request_scope)` 匹配，避免与全量生命周期标签不一致。记录见 [发布说明](../metric-scope-v2-20260915.md)。以上带日期的旧发布记录描述当时状态。

## A3 NPU 硬件监控（2026-09-16）

复用 A3-1 `122.209.21.24:8082/metrics` 和 A3-2 `122.209.21.25:8082/metrics` 的既有 `npu-exporter`，以 `job=npu-a3`、`environment=a3-vllm` 每 5 秒采集。保留 exporter 自带时间戳，采集 `npu_.*` 和 `machine_npu_nums`，每节点 16 个芯片 ID。

[A3 · 主机与 NPU](http://122.247.53.162:18431/projects/a3-monitoring/dashboards/a3-hosts) 对齐 DCU 六项硬件图表：利用率、显存已用、温度、功耗、显存总量和显存占比。支持节点与 NPU 芯片筛选；HBM 的 MiB 转为 GiB，不使用 KV Cache 代替整芯片显存。功耗按 exporter 原始芯片 ID 展示，不相加为整机功耗。有效零保留，超过 15 秒的源观测、失败抓取及非法值留空。

本次只热加载 vmagent 采集配置并更新 A3 主机看板；没有重启中央监控、推理或网关服务。实现与回退见 [NPU 发布说明](../npu-20260916.md)。

## XPU 独立项目（2026-09-21）

新增 [XPU 监控](http://122.247.53.162:18431/projects/xpu-monitoring)，8 张看板、88 个面板。资源在 `projects/xpu-monitoring/`，由 `generate_xpu.py` 基于显式线上 DCU 快照生成；现行双项目生成器不管理此新增项目。缺失项有意留空，详见 [发布与验证记录](../xpu-20260921.md)。

## XPU 卡硬件监控（2026-09-22）

已接入两节点 `:9507` 的 xpu_exporter，6 项卡硬件图表支持节点及卡号筛选，共 16 张 P800。全部 `node_xpu_.*` 原始指标进入 VictoriaMetrics；显存 MiB 转 GiB，百分数保持原值。详见 [发布记录](../xpu-hardware-20260922.md)。
