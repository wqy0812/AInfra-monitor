# 业务图表记录规则迁移实施记录（2026-10-11）

已在 test4 按停机窗口迁移 16 个 `monitoring_chart_*` 数值字段：A3 两台主机的 CPU 忙碌率/I/O 等待，以及 A3、DCU 两套 Mooncake 内存/SSD 容量 used、total、ratio。API 周期写入排除这些字段；vmalert 直接从原始采集数据计算，保持原指标名、标签、单位与缺测语义。剩余窗口类业务预计算仍保留，范围见 [业务图表原生记录规则](chart-native-recording.md)。

## 最终部署

- 应用源码：`da62dd70d0b6808b8914519ab2c75fe734e1420a`。
- API 镜像：`sha256:9138471d3d3b2eaf290cda28a4d85f75f4e2947dc8189e9b77253e6d802f3539`。
- vmalert 启动器镜像：`sha256:ddc82af14923db0d08596d231adfb572760042382ba9a1b21f2e80a606fda604`。
- 官方 vmalert 版本仍为 1.151.0。现有 Perses 126 条规则，加上新增 6 条向量规则，共 132 条、10 组；新增规则覆盖 16 字段，最多 32 条 value/valid 结果序列。
- 持久切换边界：`1791715900`，保存在既有 state 目录的 `chart-recording-cutover.json`。重启后边界一致，切换前历史不改写。
- VM CPU 配额由 2 核增至 4 核，内存保持 2 GiB。为让运行时重新识别配额，重启了同一 VM 容器；镜像、数据挂载和 vmagent 缓冲保持原状。已验证 `go_gomaxprocs=4`、`vm_available_cpu_cores=4`。启动配置已同步。
- Perses 与 vmagent 未替换；看板内容、变量和数据源未发布变更。全程未执行版本回退。

## 验证与修正

| 检查 | 结果 |
| --- | --- |
| 初始相关 Python 回归 | 189 passed，15 项真实 VM 测试在该轮未启用 |
| 最终向量规则、运行时、写入归属、部署回归 | 95 passed |
| 最终真实 VM、官方 vmalert、原 CPU 看板回归 | 183 passed |
| CPU 配额启动配置相关回归 | 86 passed |
| 发布前线上独立原始数据重建对照 | 16 字段通过 |
| 最终线上原生记录对照 | 16 字段 × 13 点 = 208 点全部通过，所选窗口记录缺点为 0 |
| 1080p 实际看板 | 5 张图表完成绘制与图例检查，页面异常为 0 |
| A3 单节点筛选 | 两节点分别检查 CPU/I/O 等待，4 项通过 |
| 规则、写入、新鲜度 | 132 条加载，求值错误 0，remote-write 错误/丢弃 0；16 字段样本年龄最大约 14.25 秒 |

208 点对照窗口为 `[1791716510, 1791716570]`；其中 182 点为有效数值、26 点按原始语义无效，不能把无效点算成有效零。该窗口覆盖完整不等于全部历史覆盖完整。

首版每字段分别执行 value/valid，新增 32 条串行规则；线上数值正确，但 A3 CPU 扫描重复且拖累同组容量，新鲜度未通过。现场修正为 CPU/两套容量三个独立组，每组两条向量规则，共 6 条；共享 CPU 扫描，并用 MetricsQL WITH 压缩容量表达式，避免超出 VM 查询长度限制。真实 VM 对照重新通过后发布当前版本。

随后确认 8 核宿主上的 VM 持续打满原 2 核配额，调整至 4 核后，CPU 两条规则一次观察分别约 1.86/1.16 秒；新鲜度在历史 replay 运行时再次通过。这里报告功能新鲜度，不宣称完整性能矩阵或长期持续覆盖。

测试过程中曾因两个 CPU fixture 写入同一时间区间造成合并测试冲突，已分开合成时间区间并重跑。浏览器验收也修正了 DCU `cache-store` 的逻辑 ID 到实际公开 `model-monitoring` 页面映射；最终记录以 `browser-final.json`、`browser-filters.json` 为准。

## 补算和证据

新增业务 chart 的切换后补算进程已结束；最终验证的是上述独立窗口。VM 重启使独立的 Perses 12 小时 replay 失败，保留失败记录后重新提交了请求 `6670d5a33cba4aa39d78e5966a7810a5`，收尾时状态为 running。实时规则正常，Perses 未覆盖区间继续按现有逻辑回源，不能宣称 12 小时全部补齐。

本机证据：`evidence/chart-native-20261011/`、`output/playwright/chart-native/`；临时浏览器、令牌文件与本地令牌服务已清理。

新增服务器目录：`/data2/monitoring/chart-native-20261011`，含 `source`、`api-build`、`api-build-final`、`api-build-vector`、`native-build`、`native-build-vector`、`api-publish`、`api-publish-vector`、`native-publish`、`native-publish-vector` 及源码自身子目录。构建、失败与最终验收证据均保留于该目录。
