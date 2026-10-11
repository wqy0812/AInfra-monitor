# 原生记录规则运维

CPU 4 项、DCU 6 项、A3 8 项，7 种步长共 126 条规则由官方 vmalert 1.151.0 执行。API 仅负责读取、精确匹配、完整性检查和原始 VM 回源，不读取旧 `monitoring_perses_value/complete`。业务 `monitoring_chart_*` 处理不变。

## 发布

稳定入口 `python3 deploy/release.py acceleration --help`。先使用 `api-image COMMIT --base-image SHA --evidence DIR` 构建提交中的 API；`acceleration build --api-image SHA --vmalert-image SHA --evidence DIR` 从同架构、不可变本地镜像构建启动器镜像，不下载依赖。官方 vmalert 镜像由单独审核的载入步骤提供。

维护窗口停止旧 API，再 `acceleration publish --image SHA --evidence DIR` 启动原生服务，`container monitoring-api SHA --evidence DIR` 替换 API。后续原生镜像替换也使用同一 publish 入口。启动器必须管理每次进程启动，不允许直接复用旧配置运行 vmalert。

默认监听 `127.0.0.1:18432`，VM 仍为 18428、API 18430。原生容器 1 CPU、768 MiB，挂载现有 `/data2/monitoring/state:/state`；VM 历史和 vmagent 发送缓冲不变。所有证据目录在运行入口前显式创建。

完成 `acceleration check --evidence DIR`、`acceleration-ready --evidence API_DIR`，以及 `acceleration audit --filters --evidence DIR`。该入口将当前验收源码通过标准输入送入已安装 API 的 Python 环境，只读执行，不替换容器文件。最后一项比较全部表达式与有限筛选，并要求所有短步长规则在最近一分钟产出过完整记录；单个格点缺失允许回源，覆盖比例单独报告；`--require-full` 进一步要求所有 126 项覆盖。`audit --coverage-only` 只刷新覆盖，不宣称执行了数值对照；覆盖在对照结束时重新读取，避免用冷启动初始缺口判失败。长步长首次尚未到期和补算中的历史允许回源，不能宣称已经覆盖。

2026-10-11 用户明确：性能只记录，无论快慢均替换。旧版“严格更快”准入不再适用。发布失败保留当前证据并修复当前版本，不回退。

## 重启与补算

每次启动、每次 replay 都在持久目录加锁分配递增代次和唯一 publication，规则指标名绑定表达式、revision 和 step。最高可见代次必须独立完整；不完整即回源，不跨代次拼凑，不回读旧预计算。数量为零的完整结果保留为空，时间戳守卫防止填充缺样。

启动器每次恢复自动 replay 最近 12 小时已封闭区间；左界向前对齐所有步长共同格点，最多额外不足一小时。官方 replay 每次请求 60 点、并发 1，单次最长 1 小时，失败保留日志；查询持续回源。较老历史不自动回填。

`deploy/perses_acceleration/admin.py backfill` 请求新的最近 12 小时补算；`--start EPOCH --end EPOCH --panel ID` 可指定封闭区间和面板，每个请求最多 12 小时（另含格点对齐）。失败请求不自动反复重试；再次提交分配新身份。中断的人工请求在下次启动后继续处理。旧并行 A3/优先级操作明确报错。

修改原始历史前先 `invalidate --start ... --end ... --panel ... --reason ...`。修改完成后 `repair --start ... --end ... --panel ...`：新代次覆盖全部请求格点并经过读取验证，才解除原始请求边界完整覆盖、且期间未改变的失效记录（格点对齐向前扩展的部分不解除）。更大修复拆成明确的 12 小时请求，跨请求的大失效区间须相应拆分；不能删除全部失效记录。

健康状态 `perses_acceleration` 报告 backend、rule_count、启动器新鲜度、原生求值错误和 replay 状态。完整性在每次读取时验证；进程存活不等于历史完整。代次文件、请求记录和旧状态均不得清空。旧结果仅保留为历史证据并自然过期。
