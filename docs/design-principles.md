# 设计原则

监控是三个仓库中独立的只读观测面：vmagent 采集，VictoriaMetrics 存储，monitoring-api 查询与派生，Perses 展示。平台（[AInfra](https://github.com/wqy0812/AInfra)）只读消费查询结果；网关（[AInfra-gate](https://github.com/wqy0812/AInfra-gate)）通过观测端口暴露 `aigate_*` 指标。

## 三个仓库共用的原则

1. **停机窗口是默认前提。** 按[停机窗口升级](maintenance-window-upgrade.md)处理，不为零中断增加状态机、双写或回退路径。
2. **退役即删除。** 下线的功能连同代码、专属测试和发布输入一起删除，历史从 Git 查。
3. **一个事实只有一个归属。** 时序在 VictoriaMetrics，任务与配置在平台数据库。
4. **测试走线上路径。** 查询、统计、采集和存储逻辑变化时，用真实 VM 测试覆盖。
5. **发布身份是 Git commit 加镜像 ID。** 统一从 `deploy/release.py` 进入，原始证据放忽略的 `evidence/`。
6. **文档只写现状，规则用正向表述。**

新增模块、脚本、指标或流程前先回答：线上谁消费它，它防住哪一次真实发生过的故障，停机窗口能否直接替代它。

## 数据流

| 环节 | 组件 | 规则 |
| --- | --- | --- |
| 采集 | vmagent（`deploy/scrape.yml`） | 保留各平台真实 job 与标签；网关只抓 `aigate_.*`，密钥用 `bearer_token_file` |
| 存储 | VictoriaMetrics | 唯一时序存储；升级保留历史、缓冲、水位和真实断档 |
| 查询与派生 | monitoring-api（`monitoring/`） | latest、history、request-profile；来源 IP 白名单，默认只允许回环 |
| 展示 | Perses | 源定义在 `perses/projects/`，由生成器产出 |
| 消费 | 平台 Web | 只读调用 monitoring-api |

## 监控规则

1. **口径优先于完整。** 保留 DP/TP/PP 去重、分层缓存分母、计数器生命周期分段；缺数据时留空，并在 `quality` 中说明原因，不插值、不反推历史。
2. **一个指标一个定义。** 口径写在 [指标口径与历史边界](metrics-and-history.md) 和 [图表说明](../perses/METRICS_GUIDE.md)；看板、API 和平台页面引用同一个表达式。
3. **看板是生成物。** 修改生成器和 `perses/projects/` 源定义，再经发布工具推送。
4. **加速层可以随时回源。** 预计算只覆盖 catalog 中的精确表达式，其余回源；关闭加速后数值保持一致。扩大加速前，先和减少面板、VictoriaMetrics 记录规则等更简单的方案做实测对比。
5. **脚本按用途收敛为三类：** 生成器、发布、少量语义检查。一次性整理完成后，把结果固化进生成器，再删除脚本和专属测试。
6. **发布只有一个入口。** 新功能复用 `deploy/release.py` 的子命令；发布身份只在 [版本记录](releases/README.md) 更新对应组件。
7. **数据完整优先。** 升级保留现有存储；重算使用独立检查点和代码指纹。

## 变更检查

- [ ] 能说出线上消费方（看板、API 调用方或平台页面）
- [ ] 指标口径只在一处定义
- [ ] 改的是生成器和源定义
- [ ] 跨仓库接口（网关 `/metrics`、平台调用的查询接口）另一侧已同步
- [ ] 一次性脚本已固化并删除
- [ ] 发布只经 `deploy/release.py`，没有新增日期目录
