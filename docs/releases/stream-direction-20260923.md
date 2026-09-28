# 流停顿监控查询服务与页面发布

本地执行结果保存在 `evidence/stream-direction-20260923/`（相对于仓库根目录，已忽略，不随 Git 提供）；下文中的服务器路径仍是当次发布记录。脚本及发布输入保留在 [原发布目录](../../deploy/stream-direction-20260923/)。

2026-09-23，通过 SSH MCP 完成以下发布：

| 目标 | 变更 | 验收 |
| --- | --- | --- |
| test4 monitoring-api / 18430 | 仅替换 gateway_live.py，新增后端等待、写持续历史字段 | 健康状态恢复 ok；A3/XPU 均有有效历史点；DCU 新字段为空 |
| test1 code-eval-web / 18080 | 仅替换 index.html、charts.js | 在线 HTML/JS 含新图表及字段；Web 代理 A3 历史查询有效 |
| test4 Perses / 18431 | 三个项目各两张网关图表，共 6 张 | 24 张看板、292 个面板不变；其他 spec 完全保留；A3/XPU 数据源代理各两条查询在最近三分钟均返回 13 个点 |

新增服务器目录：

- test4：`/data2/monitoring/releases/stream-direction-20260923`
- test1：`/data2/code-eval/releases/stream-direction-20260923`

监控镜像：`sha256:c92e4765b1ca064fa44d158605e04df7835b9b1ec4a30ce3a7ffb77402870bfc`。
Web 镜像：`sha256:edaf43b2e4a5e4bd678265de3eb2e10354a72bf60fc765d7a6364676460fb393`。

原容器均已停止并保留为 `monitoring-api-before-stream-direction-20260923`、`code-eval-web-before-stream-direction-20260923`，可回退。Perses 修改前的线上快照保存在 test4 发布目录 `perses-before.json`。发布脚本在验证失败时回退目标服务或看板。

查询服务仅短暂重启；Web 独立更新，Engine、模型、VM、vmagent 和 Perses 容器均未重启。保留已有 DCU 42 个诊断面板、注释精简和单位设置。首次健康读数为启动预热的 degraded，随后健康检查为 ok；A3 Prefill/Decode 原生采集来源仍报告 error，这与本次网关指标无关，未把它宣称为正常。

DCU 网关尚未部署两个新指标，对应新曲线留空，未回填旧指标。A3/XPU 网关已部署，新数据从该版本发布后开始积累。Perses 即时查询在末端抓取窗口不完整时可能留空；通过已完成采样窗口的 query_range 验证数据有效。
