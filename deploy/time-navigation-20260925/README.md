# 时间导航与历史缓存发布

只通过 SSH MCP 在 test4 操作。发布包尚未完成上传，正式服务未切换。

API 物料上传至 `/data2/monitoring/releases/time-navigation-20260925`：本目录的
`release_api.py`、`candidate_api.py`、`manifest.json`，以及 `monitoring/api.py`、
`deploy/replace.py`、`deploy/container_validation.py`。所有文件平放；运行时的凭据、
容器配置与证据保留在服务器，目录和证据权限为私有。

先运行 `python3 release_api.py prepare`。以 `prepared.json` 中的精确镜像启动一次性
Python 容器执行 `candidate_api.py`，使用 host 网络读取本机 VM，但不挂载生产 state、
不启动物化循环。将成功输出及该镜像摘要保存到服务器 `candidate-api.json`。
确认 test1 的任务与 worker 空闲后再执行 `python3 release_api.py switch`。
脚本保留运行配置及旧容器，要求三个环境物化时间新鲜、原本正常的数据源继续正常，
核对完整/摘要结果、重复请求结果及无关服务指纹。A3 原有后端源错误不视为本次新增故障。
失败使用已有容器身份保护逻辑恢复旧实例；显式回退入口为 `rollback`。

Perses 使用 `perses/performance/image_release.py` 与 `release-lock-perf3.json`，
证据目录为 `/data2/monitoring/perses/evidence/time-navigation-20260925`。
完整归档必须符合锁定 SHA256，候选验证及发布准入要求沿用性能补丁 README。

本次重试已核对 58,284,272 字节旧断点与本地相同，后续拆为 28 段，
`perf3-suffix-000.part` 至 `perf3-suffix-024.part` 上传成功。
第 26 段（`025`）传输返回 `Connection closed`，已停止后续远程调用，剩余三段未完成。
本地分段和逐段摘要位于忽略目录 `work/time-navigation-20260924/upload-parts`；
恢复后须校验各段、旧断点及拼接后的完整归档，再允许载入镜像。
本次未新增服务器目录。离线浏览器验收镜像已下载至本地 Docker，尚未上传。
