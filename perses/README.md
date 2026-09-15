# Perses on test4

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
