# XPU 卡硬件接入（2026-09-22）

通过 SSH MCP 在 test4 发布。采集链路：xpu_exporter → vmagent → VictoriaMetrics → Perses。

- 新增 `xpu-hardware` job，每 5 秒抓取两节点 `122.209.21.33:9507`、`122.209.21.34:9507`，4 秒超时，保留全部 `node_xpu_.*`，包括频率、ECC、XLink 等原始指标。
- 两节点分别标记 `node=xpu-1,role=decode` 与 `node=xpu-2,role=prefill`，环境为 `xpu-pd`。各 8 张 P800 OAM，卡标签 `devid=0..7`，每卡 98304 MiB = 96 GiB。
- 仅更新现行 `xpu-monitoring/hosts-xpu` 六个硬件面板：利用率、显存已用/总量/占比、温度、功耗。保留现有主机面板和布局；支持节点和卡号筛选。
- 显存 MiB 除以 1024 展示 GiB；百分数不再乘 100；温度 °C，功耗 W。负数、超过 100% 的比例、超过 15 秒的采样、失败抓取和窗口缺样留空，正常零保留。Exporter 无独立设备观测时间，新鲜度只能依据抓取时间。
- 以线上最新快照为基线，未用本地完整配置覆盖服务器；本地原有未提交的主机接入修改保留。vmagent 仅 HUP 热加载，Perses API 更新看板，无需重启推理或网关。

服务器新增目录 `/data2/monitoring/releases/xpu-hardware-20260922`，包含发布前配置/看板、候选、exporter 原始样本与验收结果。本地证据副本在 `evidence/xpu-hardware-20260922/`（忽略入库）。

发布入口：`deploy/xpu_hardware_release.py`，与 `xpu_hosts_release.py`、`perses/xpu_hardware.py` 和 `xpu_hardware_scrape.yml` 一同上传到上述目录。仅经 SSH MCP 执行 `prepare`、`publish`，待采样覆盖完整窗口后执行 `verify`。

回退前先确认配置及看板仍为本次候选；恢复 `scrape-before.yml` 并 HUP vmagent，再用当前 metadata 恢复 `dashboards-before.json` 中 XPU `hosts-xpu` 的 spec。保留其他看板及 VM 历史数据，不覆盖并发编辑。

## 验证结果

- 2026-09-22 20:08:19（北京时间）发布；当前 vmagent 镜像 dry-run 通过，两采集目标健康且 lastError 为空，各抓取 291 条样本。
- 两项本地回归测试通过；本地模板与线上候选的面板、布局、变量完全一致。
- 6 个面板 × 2 个步长（15/60 秒）× 5 种节点/卡号筛选，共 60 组范围查询全部通过。每组核对实际节点与卡号集合、数据点数量，并比较 Perses 代理与 VM 直连的标签、时间和值。
- 两节点共 16 卡都有数据。采集配置与候选一致；其他 XPU 看板及 DCU、A3 看板与发布前快照一致。
- 完成 API、数据和定义核验，未进行浏览器目视验收。
