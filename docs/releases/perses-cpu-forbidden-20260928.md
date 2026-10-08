# Perses CPU 面板 Forbidden 修复

2026-09-28，DCU 主机看板 CPU 使用率面板通过 `perses-accelerated` 查询时返回 `{"detail":"Forbidden"}`。Perses 日志确认 query_range 为 HTTP 403；本机直连加速 API 为 200。

原因是 Uvicorn 默认信任回环代理的 X-Forwarded-For，将 Perses 转发的浏览器来源用于 monitoring-api 的 IP 白名单检查。以未列入原白名单的 `127.0.0.2` 连接已认证 Perses 代理，复现相同 403。

用户明确要求将该白名单放开为 `*`。API 中间件增加通配符支持，线上设置 `ALLOWED_CLIENTS=*`，并使用 `--no-proxy-headers` 按实际连接来源解释客户端。Perses 登录认证保持原状。启动脚本与替换工具同步支持；节点 exporter 的独立访问配置未改动。

线上镜像为 `sha256:66a185a73acfcb1e830f1d13386f52a14bd978dd2f7036f1040d84e652cf8852`，基于当前运行镜像只修改该中间件判断，不混入本地其他未发布改动。旧镜像保留为 `monitoring-api:before-allow-all-20260928`。发布使用容器替换与健康检查，失败自动恢复原容器；API 完成替换，Perses、VM、vmagent 容器身份不变。

验证：49 项本地访问控制/部署相关测试通过。原白名单外来源直连 API 与经过已认证 Perses 均返回 200；读取线上 CPU 面板表达式，以 1 小时、60 秒步长、nocache 查询返回两条序列共 122 点，与 VM 原始查询标签、时间戳、数值一致（绝对误差小于 1e-6）。三个监控环境 error 均为空，检查时处理延迟约 9 秒。此为 API 链路验证，未声称完成浏览器视觉验收或重新执行性能加速验收。

服务器证据位于现有 `/data2/monitoring/evidence/`：`forbidden-20260928-release.json`、`allow-all-20260928-release.json`、`allow-all-20260928-verification.json` 及对应发布日志。未新增服务器目录。
