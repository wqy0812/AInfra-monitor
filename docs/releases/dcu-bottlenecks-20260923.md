# DCU 瓶颈监控补齐（2026-09-23）

已在 test4 发布 DCU 原始指标采集扩展及 42 个新面板，并精简 DCU/A3/XPU 共 24 张看板的注释。采集周期仍为 5 秒。A3/XPU 白名单未扩展，未修改 monitoring-api、推理配置或重启模型。监控四个容器的 ID 和启动时间发布前后一致。

## 数据与面板

新增 Prefill 计算/缓存吞吐、P/D 队列与阶段耗时及观测样本数、KV 传输耗时/大小/速度/分配等待/失败、KV/SWA 槽位和占比、HiCache D→H 备份与 H→D 回载及丢弃指标。指标直接进入 VictoriaMetrics，通过 Perses 查询。仅扩展 `environment=dcu-pd` 的两个 SGLang keep 规则。

Prefill 吞吐按已核实拓扑为每个独立 DP 组取最小 PP/TP/EP 代表 rank，不相加复制 rank；代表 rank 缺失则对应曲线留空。部署拓扑变化后须更新 `perses/dcu_bottlenecks_inventory.json`。阶段直方图和池容量按原 rank 展示，样本数不是全局去重请求量。

后端 `req_time_stats.py` 已核对：

- `queue_time_seconds`：进入通用等待队列至进入 forward。
- `prefill_forward`：最后一次 forward 进入至 Prefill 完成；包括分块过程，不能当作纯 kernel 时间。
- `chunked_prefill`：自上次分块完成（第一次为 forward 进入）到本次分块完成，存在包含关系。
- `prefill_transfer_kv_cache`：进入 Prefill 传输队列到 KV 传输结束。
- `decode_prepare`：scheduler 收到请求至进入预分配队列。
- `decode_bootstrap`：进入预分配队列至进入传输队列，包含 bootstrap 与分配等待。
- `decode_transferred`：进入传输队列至进入通用等待队列。
- `decode_waiting`：通用等待队列至 forward；`fake_output` 为进入 forward 至预构建完成。

阶段分位数不求和或相减。普通 KV 与 SWA 池分别显示；不把返回 0 的 `kv_cache_memory_usage_gb` 当成真实显存容量。HiCache 字节数包含源端声明的侧路搬运，不是 Mooncake Store 写入量。

## 描述和单位

删除面板描述中的“时间口径”“零值与空白/零值和空白”“项目与源口径”段落。查询的数据有效性保护保持生效。时延默认秒，ITL 毫秒；其他耗时仅在有近 24 小时有效 P95 数据且其中位数小于 0.1 秒时选毫秒。此次 A3 TPOT 有 264 个历史 P95 样本，中位数 0.0095 秒，固定为毫秒；没有 P95 历史的指标保持秒。单位选择保存在 `perses/time_units.json`，缩放与轴标签同步。

当前项目定义是可再生的源码资源；线上发布基于实时快照定向转换，保留线上已有编辑与源代码原本存在的差异，不用生成器全量覆盖线上。

## 验证

- 19 项本地 Perses 回归通过，包含生成稳定性、采集环境隔离、代表 rank 和单位幂等换算。
- 隔离 VictoriaMetrics 的 55 项语义检查通过：正常、无流量、缺样、重置、过期、抓取失败、缺 bucket/count、代表 rank 缺失、零分母、秒/毫秒换算；覆盖 15/60 秒步长。测试容器使用 tmpfs，完成后清理。
- 74 条即时查询与 Perses 代理对照通过；118 组新增查询的 15/60 秒区间对照通过。
- 首轮发布检查发现逐桶校验超出 VM 的 16384 字节查询限制，已改为紧凑的完整族、桶数量、Inf/count、连续性和重置校验，未提高服务端限制。发布脚本新增查询长度和执行预检。
- 即时检查中 10 条查询为空，其中 DCU HiCache 回载时延当时没有有效窗口，A3/XPU 的原有空白也保留。稍后回载计数一分钟增量为 8，说明原始数据持续采集；无样本不补零。
- 1080p 浏览器视觉验收未完成：浏览器直连报 `ERR_EMPTY_RESPONSE`；SSH MCP 明确拒绝 `-L`，不提供端口转发。未采用命令行 SSH 或其他绕过方式。需要浏览器直接访问 Perses 或 SSH MCP 支持转发。

## 发布与回退

服务器新增目录：`/data2/monitoring/releases/dcu-bottlenecks-20260923`，保存配置/看板快照、候选、指标清单、验证和发布日志。所有远程操作均由 SSH MCP 执行。未回填新增指标的历史数据。

执行顺序：上传 `deploy/dcu_bottlenecks_release.py` 与 `perses/{dcu_bottlenecks,project_queries,generate,metric_scope}.py`；准备只读指标和看板快照后执行 `prepare`、`collect`；等待至少 120 秒有效采样，执行隔离验证，再 `publish` 和 `verify`。该日期目录是本次发布记录，不可作为下一次发布的旧快照复用。

本次回退经 SSH MCP 执行：

```sh
python3 /data2/monitoring/releases/dcu-bottlenecks-20260923/dcu_bottlenecks_release.py rollback
```

回退检查当前配置和看板是否仍属于本次候选，遇到并发修改停止；只恢复配置及看板，保留 VM 历史。硬件内存带宽、卡间通信 profiler 和单请求跨网关完整关联仍不属于此次覆盖。
