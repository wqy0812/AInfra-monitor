# 原生记录规则替换实施记录（2026-10-11）

已在 test4（dkfhc8287nap002）按停机窗口完成替换：CPU 4 项、DCU 6 项、A3 8 项，共 126 条记录规则由官方 vmalert 1.151.0 执行。API 只读取原生记录并在缺口回源，不启动旧预计算 worker，不读取旧 `monitoring_perses_value/complete`。业务 `monitoring_chart_*` 保持原处理语义。

## 运行与版本

- 应用源码版本：`273f93e9e972b1dc81769bde23f11abaa7aaced0`。
- API 镜像：`sha256:528911c9714f6190d2cd780dfac98238b3ad1bc1a5ac68bdf2d3a4c2c6e2c1cb`。
- vmalert 启动器镜像：`sha256:61050999b5ea56cd5a2ae3a9c8a1e76c6ef9f904a2295cfed5ff7c310f38489b`。
- 宿主运维工具另含 `1101c67` 的容器构建标签继承修复；最终镜像与容器 revision 标签一致。
- 运维源码位于 `/data2/monitoring/native-20261011/source`，稳定入口为该目录的 `deploy/release.py`。规则服务监听 `127.0.0.1:18432`。
- 最终 API 健康为 `ok`，126 条规则加载、求值错误为 0；原生写入错误和丢弃行计数为 0。
- 旧水位和旧补算状态文件在停掉旧 worker 后至最终发布的 SHA-256 一致；VM 数据、vmagent 缓冲和看板资源未改写。未执行版本回退。

本次新增服务器目录为 `/data2/monitoring/native-20261011`，包含 `source`、`api-build`、`api-build-r2`、`api-build-final`、`native-build`、`native-build-final`、`api-publish`、`api-publish-final`、`native-publish`、`native-publish-final`、`api-labels`、`native-labels` 及源码自身的子目录。

## 验证

| 验证 | 结果 |
| --- | --- |
| 最终相关 Python/部署回归 | 230 passed |
| 真实 VM/vmalert 全表达式语义 | 1026 项通过 |
| 有限筛选组合 | 378 项通过 |
| 故障与回源 | 13 项通过 |
| 本地后端对照 | 252 组，只记录，不作为性能准入 |
| 本地 1080p 新旧看板对照 | 35 组通过 |
| 线上实际看板 | 18 张受影响图表完成绘制、图例与截图检查，无页面错误 |
| 最终镜像线上查询复核 | 18 项通过，包含记录缺口与原始 VM 的拼接 |
| 线上当前原生覆盖检查 | 通过功能准入；90/126 项完整覆盖所选两个格点，18/18 个短步长规则有完整记录产出 |

合成数据的故障场景主要使用 5–120 秒步长；600/3600 秒步长验证正常窗口、对齐与筛选，不宣称每种故障均在每个长步长重复执行。

第一轮线上 `--filters` 完成 504 次相等断言后，旧验收工具在收尾覆盖断言失败；此运行不记为整套通过。保留的 traceback 位于 `query-acceptance.err`。验收工具已修正为结束时重新读取覆盖，并将完整覆盖与正确性分开：记录缺点可回源，不能因性能未达到每周期完整产出而否定用户已明确要求的替换。最终覆盖与 18 项查询复核分别保存为独立报告。

截图复核还发现初版浏览器脚本会截到加载占位，已补充等待 Skeleton 消失、图表绘制完成，并重新检查全部 18 张图表。旧报告保留为 `browser-live-prepaint.json`，以最终 `browser-live.json` 为准。

证据在本地 `evidence/issue4-native-all-20261011-r2/`、`evidence/issue4-native-release-20261011/`、`output/playwright/issue4-live/`，以及服务器上述发布目录。证据目录不纳入 Git。

## 覆盖边界

替换与服务验收已完成，最近 12 小时原生 replay 仍在后台运行，尚未完成全历史覆盖。5 秒规则组存在跳过的评估周期；相应点按完整性检查回源，不补造样本、不读取旧预计算。当前观察到的 90/126 是指定短窗口的覆盖计数，不代表 12 小时补算进度百分比。

后续运维见 [原生记录规则运维](native-recording-rules.md)。正常查询不等待补算；较老历史直接回源。发布期间每次原生进程恢复都会分配新代次并启动有界 replay，旧代次仅在没有更新代次时参与原生读取，不能跨代次拼出完整结果。
