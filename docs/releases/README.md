# 发布身份与历史检索

当前操作见 [项目 README](../../README.md)、[部署工具](../../deploy/README.md)、[Perses 维护](../../perses/README.md) 和 [停机窗口升级](../maintenance-window-upgrade.md)。API 与探测 worker 的 2026-10-14 记录已经本轮线上核验；其他组件保持各自最近的历史记录，不代表本轮重新验收。

## 最近保留的版本记录

| 范围 | 记录日期 | 版本身份 | 证据位置 |
| --- | --- | --- | --- |
| 维护工具 | 2026-10-14 | 源码 `f69f1161a91608312f79389d408afd71c01ec6c3`；通用 api-image/container 入口 | test4 `/data2/monitoring/releases/probes-minute-20261014-f69f116/source/` |
| 已安装生成器 | 2026-10-12 | 源码 `f8ce79ba26492b4a4e054e797d11b8c03c4f9c28`；Git bundle SHA-256 `448f1922a769f3b050fb1a266c119d3969323fc936021d26367c61901888ace1` | `/data2/monitoring/releases/native-gauges-20261012-f8ce79b/runtime/` |
| monitoring-api | 2026-10-14 | 源码 `f69f1161a91608312f79389d408afd71c01ec6c3`；镜像 `sha256:2613fddadf60e1e79f53bdd6fb9987ccf4f1f15e18f215015729430940c8aa8c`；interval=60、stale=120、offset=0/20/40 | `probes-minute-20261014-f69f116/api-build/`、`api-release/`、`checks/acceptance.json` |
| monitoring-probes | 2026-10-14 | 与 API 同源码/镜像；A3/XPU/DCU 各每 60 秒一次，请求总时限 50 秒、VM 导入总时限 5 秒 | 同批 `probes-release/`、`checks/probes-first-cycle.json` |
| vmalert | 2026-10-12 | 源码 `f8ce79b`；镜像 `sha256:9eedd2fd65f87f4737efe06b17b3cd37f0e3178375a28bf00b948778de7d34e6`；官方二进制 1.151.0，202 条规则 | `native-gauges-20261012-f8ce79b/native-build/`、`native-release/`、`checks/native-check.json`、`checks/native-audit.json` |
| Perses 看板 | 2026-10-12 | 资源源码 `f8ce79b`；3 项目、21 看板、261 面板；修改 5 张公开看板，16 项受影响查询检查通过 | `native-gauges-20261012-f8ce79b/dashboards/`；本机 `output/playwright/native-gauges-20261012-f8ce79b/` |
| Perses | 2026-10-09 | `0.54.0-perf.3`；镜像 `sha256:978402ac5154e3ee2cf8c66f244fc7169be74125ef01a5fe9d28c1e8a24764e7` | 原 TLS 发布记录和 [构建版本锁](../../perses/performance/release-lock-perf3.json) |

**一分钟探测配套发布（2026-10-14 08:56—08:59，北京时间）**：已推送源码 `f69f1161a91608312f79389d408afd71c01ec6c3`，以原已安装镜像 `sha256:7733d4acf1ffa2d02dc8918ad51ecf3ca0387fec828c1bb48b1eec739b29e7e1` 为不可变基底，复用通用工具禁网构建并替换 API、worker。两个容器各 35 个源码文件与构建清单一致，启动参数、环境配置、资源限制、state 挂载及探测配置摘要保持一致，restart_count=0。API、worker 健康，三环境快照新鲜且来源完整；Mac 非白名单读取返回 403，平台代理正常读取。

新 worker 08:56:51 启动后，首轮自然定时请求全部成功：A3 08:57:00 开始，首输出 0.691 秒、总耗时 1.350 秒；XPU 08:57:20 开始，首输出 0.665 秒、总耗时 2.222 秒；DCU 08:57:40 开始，首输出 0.429 秒、总耗时 0.642 秒。均 HTTP 200、有效输出及流完整结束，仅消费定时请求，没有另加手动模型请求。API 返回 interval=60、stale=120、offset=0/20/40。

本轮复用审核通过的本地验证：探测单元 55 passed、独立真实 VM 1 passed。配套 code-eval Web 源码 `c830af2bf38f99e5ad99a9076e106178f667c049` 已发布，当天十分钟汇总、144 格、未来格、真实记录详情、只读刷新通过线上 Chrome 1280×720 与 iPhone 390×664 竖屏触控模拟验收。旧五分钟数据按新频率展示缺测；VM 历史、发送缓冲和处理水位保留，未更新 VM、vmagent、vmalert 或 Perses。

服务器新增 test4 `/data2/monitoring/releases/probes-minute-20261014-f69f116/`，包含 `source/`、`api-build/`、`api-release/`、`probes-release/`、`checks/`；本地公开证据为 `evidence/probes-minute-20261014-f69f116/`。Git bundle SHA-256 为 `88eb447a0f300f0eb92e5f868543cd539a9de0e43ed2142581ce9b3396a19c47`。原容器配置保存在服务器受限文件中，公开证据包不包含凭据。准备阶段 bundle 需要显式检出指定 commit，首次构建命令未运行即退出；修正后正常构建，说明保留于 `checks/preparation-notes.json`。上一轮探测发布正文从 Git 历史查阅。

**此前原生瞬时值发布（2026-10-12 08:20）**：以下证据属于 `native-gauges-20261012-f8ce79b`，本轮未重复执行。此前按停机窗口替换 API 和 vmalert，更新看板与生成器。采集配置逐字节一致，因此未替换 vmagent；VM、缓冲和状态挂载保留，未重算历史。三环境健康且配置来源覆盖完整，快照读回与同一时刻 VM 结果一致；最终检查的数据延迟约 20 秒。DCU Decode 缓存 summary/full 的 13 个历史点相同，其中 6 个非空，实际缺口保留。

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
