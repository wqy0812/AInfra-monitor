# 本地最新版 API 与派生查询发布

发布日：2026-09-15，北京时间。所有远程命令与文件传输通过 SSH MCP。

## 已部署范围

- test4 `monitoring-api`：本地 `monitoring/*.py` 共 15 个文件，容器内 SHA256 与发布快照逐项一致。
- 镜像：`sha256:d61e940ec986161e3e65f81a6c7eb82ed906d14503b315036cac3118b4102826`。
- 新容器：`75c3dab74b26a49112eb62db6ff2c373217df227d539ab923450e1d0ebb1cca2`。
- Perses：仅更新 `a3-monitoring/a3-overview`、`dcu-monitoring/backend-diagnostics`、`dcu-monitoring/overview` 的请求派生 schema 与说明。已验证其他字段、布局、资源集合无变化。
- 本地补充 `project_split.py` 的 schema 兼容转换与对应回归测试；保留现行双项目结构。未发布 code-eval Web、网关或模型服务。

## 验证

- 本地 155 项回归通过；`git diff --check` 通过。
- 候选镜像使用真实 VM 数据只读回放：DCU Decode 输出吞吐包含有效零值与 857.4 Token/s 的非零样本；A3 源缺少 `is_streaming`，请求派生值为 null。
- 独立临时 VM 140 项语义查询通过；测试实例及夹具数据已清理。
- 发布前、发布后各 512 组 VM/Perses 代理查询一致。该历史窗口包含新 schema 尚未开始写入的时期，108 组查询为空；查询对照成功不代表这些查询均有数值。
- 发布后单独检查新数据窗口：DCU Prefill、Decode 的 `output_tokens valid=1`；各返回 5 个面板数据点，VM 与代理相同。Decode 在该窗口采样点为 0，Prefill 最大 0.2 Token/s。
- DCU/A3 API 均健康且数据新鲜；21 个采集目标全部 up=1。
- A3 的源 `generation_tokens_total` 缺少 `is_streaming` 标签，新版请求数、Token、时延、队列派生指标留空。A3 Prefill 缓存命中派生值仍有效。
- 其他三个运行容器的 ID、StartedAt、PID 保持一致。未进行浏览器目视验收。

## 证据与回退

远端发布根目录：`/data2/monitoring/releases/local-latest-20260915-2030`。

根目录保留 `containers-before.json`、`perses-before.json`、`before.json`、`journal.json`、`source-sha256.json`、`candidate-acceptance.json`、`audit-candidate.json`、`audit-after.json`、`acceptance.json`、`complete.json` 及发布文件。完整容器配置备份权限为 0600。

旧 API 容器保留为停止状态：`monitoring-api-local-latest-rollback-1789475794`。

如需回退，通过 SSH MCP 在 test4 依次执行：

```sh
python3 /data2/monitoring/releases/local-latest-20260915-2030/perses/project_release.py rollback --evidence /data2/monitoring/releases/local-latest-20260915-2030
python3 /data2/monitoring/releases/local-latest-20260915-2030/deploy/local_latest_release.py /data2/monitoring/releases/local-latest-20260915-2030 rollback
```

面板回退会检测并发编辑。未删除旧 VM 数据，未重建新口径历史；发布前的新 schema 时段留空。持久化发布文件若已用于后续重建，回退后还需按备份同步其版本。
