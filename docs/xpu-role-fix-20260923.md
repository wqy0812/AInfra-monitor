# XPU 角色及缓存监控修复

2026-09-23 根据两台机器当前 sglang.launch_server 进程参数校正角色：xpu-1（122.209.21.33）是 Prefill，启用 hierarchical cache；xpu-2（122.209.21.34）是 Decode，禁用 radix cache。此前监控沿用相反映射，查询迁移一致性检查未发现实际角色与采集标签不一致。

## 修改

- sglang-prefill 抓取 .33:8501，sglang-decode 抓取 .34:8501。Prefill 既有白名单现在正确保留回载、淘汰、操作耗时等指标。
- Node Exporter 和 XPU Exporter 保持物理节点及端口不变，修正 role 标签。
- 修正 XPU 看板及生成器的角色与节点映射，缓存图固定查询 .33 的 Prefill 原始指标。
- “缓存操作速率”改为“回载操作速率”，与 load_back_duration_seconds_count 的定义一致。
- 移除主机缓存容量/占用和 CPU HiCache 独立命中率两个空占位图；没有可核实原生数据，不用硬件 L3 内存代替。XPU 缓存剩 8 图，三平台仍共 30 张看板、303 图。

Exporter、推理启动参数、业务路由均未修改。vmagent 经配置检查后 SIGHUP 热加载，不重启采集服务。历史错误角色标签数据没有重写；跨修复时刻的历史窗口会包含旧角色数据，评估新映射应选修复后的时间范围。新抓取的指标不能补回过去被白名单丢弃的样本。

## 验收及发布保护

本地 34 项测试覆盖当前拓扑、采集白名单、缓存来源、占位清理及生成器。30 张看板在本地隔离 Perses 通过 1920×1080 布局与筛选检查，使用合成数据。生产核验通过：六个 XPU 采集目标按新角色连续两分钟健康，缓存原始指标已进入 VM，平均耗时在无操作时仍留空；发布前后各 850 项 VM/Perses 代理查询对照通过，线上读回与模板一致且重复生成不变。额外六项实时角色查询确认 P/D 主机、各 8 张卡和后端排队分别对应正确节点。核验时 Prefill 命中率为 88.2897%，回载及淘汰速率为有效零，平均耗时为空。

准备期间 monitoring-api 被外部操作更新。第一次热加载后的指纹保护检测到变化，自动恢复旧采集配置；确认仅 API 更新、看板和其他服务未变、API 可用后，保留该更新并刷新发布基线继续。本任务没有回滚或替换该 API。

新增远程证据目录：`/data2/monitoring/perses/evidence/xpu-role-fix-20260923`。其中保存看板快照、采集配置前后版本、服务指纹、并发变更记录、原生指标、查询核验及运行时文件日志。所有远程操作通过 SSH MCP。

看板及采集配置回退命令（经 SSH MCP 执行）：

```sh
python3 /data2/monitoring/perses/evidence/xpu-role-fix-20260923/xpu_role_release.py rollback --evidence /data2/monitoring/perses/evidence/xpu-role-fix-20260923
```

回退会核对并发修改，并按同目录 runtime-install.json 自动恢复生成器及模板；要求当前内容仍等于本次写入值。恢复前重新核实实际 P/D 拓扑，避免重新引入已失效映射。
