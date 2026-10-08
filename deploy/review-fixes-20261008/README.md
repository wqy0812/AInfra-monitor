# monitoring 审核修复发布（2026-10-08）

本目录为已完成发布的源码、输入清单与追溯材料，不能直接重跑；下一次发布需重新核查工作区、线上镜像、源文件、看板和状态。

修复 A3 的非零已用量/零总容量观测，以及 Perses 安装清单缺少 `a3_mooncake.py`。核查现场发现已提交的 `replay.py` 来源切换修复尚未上线，本次同步到当前工作区版本。

- 原 API 镜像：`sha256:441cb9964954f66e8e12f4e207091791e2ee3d620c51bbe0f0ea72181a6cf8de`。
- 发布镜像：`sha256:abbc3f6c464109f0feef6f1625f3dfee3b4ec11e69644d4c055dde20e140b927`。
- 载荷 SHA256：`00facd5aa596a5a9451f4978a6a669d525f63eb329ccd1cf71f72d044052cfd8`。
- `manifest.json` 固定源文件摘要、现场文件摘要、API 配置摘要和 39 项 Perses 资源 spec 摘要，不含认证凭据。
- `release.py` 使用本次同步的通用 `deploy/replace.py`，采用停机窗口替换；异常保留现场并停止，不回退。源码写入前检查并发修改，逐文件原子替换并读回验证。
- 验证 API 全部 24 个源码文件、93 个运行时文件、采集配置、39 项在线资源、访问控制配置、持久化启用时间和处理水位。运行时实际更新 22 个文件。

服务器宿主 Python 为 3.7，本次发布工具使用原 API 固定镜像内的 Python 3.11.16 执行，绑定宿主 Docker CLI、Docker socket 与 `/data2/monitoring`；没有启动候选监控服务。临时工具容器运行后自动删除。

服务器新增 `/data2/monitoring/releases/review-fixes-20261008/`，含载荷内的 `monitoring/`、`deploy/`、`perses/` 及 `perses/projects/` 子目录。原始证据留在该目录；本地副本为忽略目录 `work/review-fixes-20261008/evidence/`。完整验收见 [部署记录](../../deployments/2026-10-08/monitoring-review-fixes.md)。
