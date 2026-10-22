# 原生记录规则

启用[自动监控](automatic-monitoring.md)后，`native_rules.build(catalog)` 从目录绑定适配模板，来源、节点及预期实例数动态生成。controller 先 dry-run 再发布 `rules.json`，运行服务以 `--rules-file` 只读使用该文件。下文 202 条是旧三环境模板基线，实际规则数以当前目录和 vmalert 读回为准。多节点角色汇总使用独立 `monitoring_scope="role"` 记录，逐节点看板排除该汇总，避免重复计数。

2026-10-11 已批准 D1–D6，现行实现见 [口径评审](native-standard-review-20261011.md)。生产按新系统建设；测试环境迁移不要求复刻旧 Python 的窗口否决、代次发布及自动历史接续。

官方 vmalert 1.151.0 每 5 秒计算固定 1 分钟窗口并写回 VM，指标前缀 `monitoring_native_`。规则来源为 `monitoring/native_rules.py`，当前源码的 202 条规则均直接读取原始指标，不依赖另一条记录规则的写入时序。API 只读计算记录及原始瞬时值并转换 code-eval JSON；请求画像及网关查询仍提供原有只读接口。Perses 使用 `victoriametrics` 数据源，已移除 `/internal/perses` 代理和 `perses-accelerated` 数据源。

A3 Prefill、Decode 的输出与 Decode Token 速率共用 `monitoring_native_backend_output_tokens_per_second`，API 保留 `output_tokens` / `decode_tokens` 两个字段，Perses 两张相关看板读取同一记录；DCU/XPU 的独立 Decode 统计保持不变。此合并将源码规则数从 286 减至 284，不改变统计窗口、来源、单位或历史数值口径。VM 中旧的 A3 `backend_decode_tokens_per_second` 记录保留但不再读取；历史始终使用原本就存在的输出速率记录，缺口不回退到旧别名。部署时须同步更新 API、规则服务、Perses 资源及生成器，发布身份以实际验收为准。

另有 82 条瞬时值记录已从源码移除，规则数进一步减为 202。内存/负载、文件系统容量、调度队列、KV 占用、卡温度/功耗、Store/segment 容量及 XPU 原生命中率由 `monitoring/raw_gauges.py` 统一生成原始查询；API 按来源合并字段并放入同一次 VM 请求，Perses 使用同一查询定义。`monitoring_direct_*` 只是查询响应中的字段别名，既不写入 VM，也不增加新采集。

瞬时值保留原来的 up、新鲜度、DCU 设备采样成功/时间、容量有限非负及比率范围检查，角色、rank、设备、Store/segment 身份和显示单位不变。旧的 82 条规则所写历史保留但排除读取；当前与历史都直接读原始样本，原始数据仍存在时可显示早于记录启用时间的历史。计算记录继续缺口留空，直读不充当其回源。快照仍按原有 15s 读取余量对齐，不混用不同时刻，也不宣称本次调整缩短了整体快照延迟。

## 统计定义

- counter 先按源 `rate`/`increase` 再汇总，固定 1m；重置、启动短窗口和窗口内漏采遵循 VM 原生行为。源相对评估时刻需 up=1 且指标及 up 在 15 秒内新鲜，使用显式 `default_rollup(...[15s])`，不受 5s 查询步长的隐式回看限制，仍尊重 staleness marker。原生 gauge 使用最近新鲜值，不伪称为 1m 平均。
- 可用来源参与计算。采集覆盖单独显示可用/配置实例；这不保证每个指标齐全。直方图和缓存质量检查单独展示，不作为所有数值的否决条件。
- DCU 请求/完成 Token、TTFT/E2E 采用 true/false 流式分区，ITL 为未分区直方图；XPU 使用未分区 tokenizer 数据。不能把总量与分区重复相加。
- DCU/XPU Decode 保留全部独立 DP，以当前配置的 DP=TP、PP=0 为每组代表；Prefill 复制视图选择 PP/TP/EP=0。新增拓扑须更新规则与配置来源数量。
- 比率分母为零、非有限或比例不在 0–1 时留空。Store used/total 分别保留有限非负值，不相加复制容量；API 的单 Store 字段遇多 Store 歧义留空，Perses 按实例展示。
- 原生直方图 P95 落入 +Inf 桶时可能返回最大有限边界；不再保留旧逐桶完整性遮蔽。质量指标检查缺失、+Inf/count 和缓存计数一致性，不保证逐桶单调或全部指标完整。
- XPU 整体前缀命中率仍为框架原生 gauge；不同后端缓存指标的人群、分母和单位保持区分。

## 运维

规则 `eval_delay` 和 VM `latency_offset` 均为 5s，给原始采样到达留一个采集周期；API 读取当前时间减 15s 后对齐的记录，留出各组调度及异步写回余量。快照每 5s 更新，通常滞后约 15–25s，30s 判过期；API 在 `metric_policy` 返回这些参数，code-eval 按同一过期阈值展示。15s 源新鲜度相对记录评估时刻，30s 是快照相对现在的有效期，两者不能混用。网络或采集延迟超过余量时应检查来源和发布审计，不用旧值、回源或回放遮盖。

在目标机仓库根目录执行，远程入口使用 SSH MCP。用已提交源码构建 API，再用固定的官方 vmalert 镜像构建原生服务：

```sh
python3 deploy/release.py api-image COMMIT --base-image sha256:BASE --evidence API_BUILD_DIR
python3 deploy/release.py acceleration build --api-image sha256:API --vmalert-image sha256:VMALERT --evidence NATIVE_BUILD_DIR
python3 deploy/release.py acceleration publish --image sha256:NATIVE --evidence NATIVE_RELEASE_DIR
python3 deploy/release.py container monitoring-api sha256:API --evidence API_RELEASE_DIR
python3 deploy/release.py acceleration check --evidence CHECK_DIR
python3 deploy/release.py acceleration audit --evidence AUDIT_DIR
```

所有证据目录预先显式创建；按停机窗口替换受影响组件，失败留证并修复当前版本。`audit` 在已安装 API 容器中只读比较每条原始表达式和相同时刻的存储记录，分别报告有数据和空结果；空结果不代表真实业务覆盖。

`check` 和容器发布验收除规则健康外，还要求当前 vmalert 进程已有成功发送、无丢弃，并从目标 VM 读回三个环境、两个角色的配置实例数记录。六条记录须在当前进程启动后写入且样本年龄小于 30s；该记录不依赖业务请求或后端 up，因此无流量、来源故障不会阻止验证写入链路。读取使用原始样本时间并禁用查询缓存，旧进程的记录或仅有发送计数不能通过。历史累计发送错误会保留在证据中，恢复后可通过新鲜记录读回验收。自定义 VM 可用 `acceleration check --vm URL` 指定；容器替换使用容器的 `--vm` / `VM_URL` 配置。

首次安装先准备固定镜像，设置 `MONITOR_VM_IMAGE`、`MONITOR_API_IMAGE`、`MONITOR_NATIVE_IMAGE` 为完整本地 image ID，显式配置 `ALLOWED_CLIENTS`，再执行 `deploy/start.py central`。此入口同时启动 VM、vmagent、vmalert、只读 API；已有容器时拒绝首次安装，改用发布入口。

每次重启只开始正常评估，不执行 replay、不写 generation/publication 文件。计算指标启用前或记录缺口保留空白，不以旧口径或原始查询自动填补；瞬时值不依赖记录服务。VM 历史、发送缓冲、旧水位和发布文件保持原样。需要计算记录历史时可另行显式运行官方 replay，提供 `monitoring.recording_runtime --replay-start ISO_TIME --replay-end ISO_TIME` 的两个边界；不能把另一个实时 writer 启在相同记录时间范围。

Perses 发布使用 `dashboards prepare/apply` 和 `runtime apply`，校验并发编辑、真实数据源查询及资源读回。合成回归使用 `experiments/native_standard_probe.py --approved`；旧预计算与候选记录模块及其专属测试已退役，历史实现从 Git 查阅。原先的 Python 延迟重建写入入口已删除。
