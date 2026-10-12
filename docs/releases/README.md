# 发布身份与历史检索

当前操作见 [项目 README](../../README.md)、[部署工具](../../deploy/README.md)、[Perses 维护](../../perses/README.md) 和 [停机窗口升级](../maintenance-window-upgrade.md)。本页版本来自既有发布记录，**不代表本次重新核验了线上状态**。

## 最近保留的版本记录

| 范围 | 记录日期 | 版本身份 | 证据位置 |
| --- | --- | --- | --- |
| 维护工具与生成器 | 2026-10-12 | 源码 `f8ce79ba26492b4a4e054e797d11b8c03c4f9c28`；Git bundle SHA-256 `448f1922a769f3b050fb1a266c119d3969323fc936021d26367c61901888ace1` | test4 `/data2/monitoring/releases/native-gauges-20261012-f8ce79b/`，含 `source/`、`runtime/`、`checks/` |
| monitoring-api | 2026-10-12 | 源码 `f8ce79b`；镜像 `sha256:216731a9f83dd975d9be8f3db5d906a0e2cf586f997f98a906be22f19e7bc21a`；瞬时值直读原始采样，summary 保留 Decode 缓存命中率 | 同批 `api-build/`、`api-release/`、`checks/live-acceptance.json` |
| vmalert | 2026-10-12 | 源码 `f8ce79b`；镜像 `sha256:9eedd2fd65f87f4737efe06b17b3cd37f0e3178375a28bf00b948778de7d34e6`；官方二进制 1.151.0，202 条规则 | 同批 `native-build/`、`native-release/`、`checks/native-check.json`、`checks/native-audit.json` |
| Perses 看板 | 2026-10-12 | 资源源码 `f8ce79b`；3 项目、21 看板、261 面板；修改 5 张公开看板，16 项受影响查询检查通过 | 同批 `dashboards/`；本机 `output/playwright/native-gauges-20261012-f8ce79b/` |
| Perses | 2026-10-09 | `0.54.0-perf.3`；镜像 `sha256:978402ac5154e3ee2cf8c66f244fc7169be74125ef01a5fe9d28c1e8a24764e7` | 原 TLS 发布记录和 [构建版本锁](../../perses/performance/release-lock-perf3.json) |

本次按停机窗口替换 API 和 vmalert，更新看板与生成器。采集配置逐字节一致，因此未替换 vmagent；VM、缓冲和状态挂载保留，未重算历史。三环境健康且配置来源覆盖完整，快照读回与同一时刻 VM 结果一致；最终检查的数据延迟约 20 秒。DCU Decode 缓存 summary/full 的 13 个历史点相同，其中 6 个非空，实际缺口保留。

202 条规则逐条 raw/记录对照通过，其中 184 条当时非空，18 条为空不代表真实业务覆盖。新进程写入错误及丢弃均为零，六条配置实例数记录在进程启动后写入并保持新鲜；537 条瞬时来源序列在 API 中可读，`monitoring_direct_*` 别名没有写入 VM。35 个生成器模块字节核验及独立运行通过，安装新增 `/data2/monitoring/perses/release/monitoring/` 包目录。发布后 Perses 未认证访问返回 401，API 非白名单访问返回 403，TLS 使用服务器证书校验。

1080p Chrome 检查覆盖五张受影响看板的关键图表：A3 Prefill/Decode 队列分别有 8/32 条序列，模型页 Decode 筛选与汇总页输出吞吐角色正确，DCU HiCache 有 8 个 Prefill rank，Mooncake segment 容量有已用/总量两条序列。281 次图表查询均成功，未捕获页面运行错误；见同批 `checks/browser-acceptance.json` 和本机截图。Mac 未信任服务器自签证书，因此通过验证上游证书的临时本机转发入口访问真实线上数据，未禁用 TLS 校验；该入口和测试浏览器已关闭。

Perses TLS 的原记录确认 HTTPS、HTTP/2、认证和资源保留；证书与访问方式见 [维护说明](../../perses/README.md)。本次 API 使用固定的已安装基础镜像叠加已提交源码构建，基础镜像及产物身份见 `api-build/`。

此前查询瘦身的常规性能门槛未全部通过；本次功能验收不替代该性能验收，取舍依据见 [当前查询说明](../query-slimming.md)。

## 当前说明与材料

- [指标口径与历史边界](../metrics-and-history.md)：请求范围、A3 贡献分母、采集完整性、CPU 聚合及流停顿。
- [时间范围与缓存](../time-navigation.md)、[查询加速](../perses-query-acceleration.md)、[当前加速清单](../../deploy/perses_acceleration/STATUS.md)。
- [代码退役索引](retired-code.md)：原路径及 SHA-256。

日期目录中的 Dockerfile、manifest、panels、版本锁和校验和保留。原始执行证据在忽略的 evidence/work 目录，Git 检出不能恢复它们。新发布更新本页对应组件的版本身份、验收范围及证据位置；过程日志和审核流水不另行提交，旧版本从 Git 查阅。

## 完整历史

在仓库根目录执行：

```sh
git ls-tree -r --name-only fbf952971be7 -- docs/releases deploy reviews deployments
git show fbf952971be7:docs/releases/columns-maintenance-20261010.md
git show fbf952971be7:docs/releases/query-slimming-20261009.md
git show fbf952971be7:docs/releases/perses-tls-20261009.md
git show fbf952971be7:docs/releases/monitoring-history.md
git show fbf952971be7:docs/releases/perses-history.md
```

这些正文保存当时的发布、故障、未完成项与验收边界，不能作为当前操作指令或恢复旧版本的授权。
