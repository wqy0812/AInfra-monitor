# 原生记录规则

2026-10-11 已批准 D1–D6，现行实现见 [口径评审](native-standard-review-20261011.md)。生产按新系统建设；测试环境迁移不要求复刻旧 Python 的窗口否决、代次发布及自动历史接续。

官方 vmalert 1.151.0 每 5 秒计算固定 1 分钟窗口并写回 VM，指标前缀 `monitoring_native_`。规则来源为 `monitoring/native_rules.py`，286 条规则均直接读取原始指标，不依赖另一条记录规则的写入时序。API 只读记录并转换 code-eval JSON；请求画像及网关查询仍提供原有只读接口。Perses 使用 `victoriametrics` 数据源，已移除 `/internal/perses` 代理和 `perses-accelerated` 数据源。

## 统计定义

- counter 先按源 `rate`/`increase` 再汇总，固定 1m；重置、启动短窗口和窗口内漏采遵循 VM 原生行为。当前源需 up=1 且指标及 up 在 15 秒内新鲜。原生 gauge 使用最近新鲜值，不伪称为 1m 平均。
- 可用来源参与计算。采集覆盖单独显示可用/配置实例；这不保证每个指标齐全。直方图和缓存质量检查单独展示，不作为所有数值的否决条件。
- DCU 请求/完成 Token、TTFT/E2E 采用 true/false 流式分区，ITL 为未分区直方图；XPU 使用未分区 tokenizer 数据。不能把总量与分区重复相加。
- DCU/XPU Decode 保留全部独立 DP，以当前配置的 DP=TP、PP=0 为每组代表；Prefill 复制视图选择 PP/TP/EP=0。新增拓扑须更新规则与配置来源数量。
- 比率分母为零、非有限或比例不在 0–1 时留空。Store used/total 分别保留有限非负值，不相加复制容量；API 的单 Store 字段遇多 Store 歧义留空，Perses 按实例展示。
- 原生直方图 P95 落入 +Inf 桶时可能返回最大有限边界；不再保留旧逐桶完整性遮蔽。质量指标检查缺失、+Inf/count 和缓存计数一致性，不保证逐桶单调或全部指标完整。
- XPU 整体前缀命中率仍为框架原生 gauge；不同后端缓存指标的人群、分母和单位保持区分。

## 运维

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

首次安装先准备固定镜像，设置 `MONITOR_VM_IMAGE`、`MONITOR_API_IMAGE`、`MONITOR_NATIVE_IMAGE` 为完整本地 image ID，显式配置 `ALLOWED_CLIENTS`，再执行 `deploy/start.py central`。此入口同时启动 VM、vmagent、vmalert、只读 API；已有容器时拒绝首次安装，改用发布入口。

每次重启只开始正常评估，不执行 replay、不写 generation/publication 文件。新指标启用前或记录缺口保留空白，不以旧口径或原始查询自动填补。VM 历史、发送缓冲、旧水位和发布文件保持原样。需要历史时可另行显式运行官方 replay，提供 `monitoring.recording_runtime --replay-start ISO_TIME --replay-end ISO_TIME` 的两个边界；不能把另一个实时 writer 启在相同记录时间范围。

Perses 发布使用 `dashboards prepare/apply` 和 `runtime apply`，校验并发编辑、真实数据源查询及资源读回。合成回归使用 `experiments/native_standard_probe.py --approved`；历史计算模块保留作离线旧口径对照，不被现行 API 或 vmalert 运行入口导入。原先的 Python 延迟重建写入入口已删除。
