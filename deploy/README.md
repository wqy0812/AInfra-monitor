# 部署工具

现行发布命令统一为 `python3 deploy/release.py <组件> ...`，在仓库根目录、目标主机本地执行。连接与传输遵循工作区 SSH MCP 约束；工具自身不建立远程连接。默认执行 [停机窗口升级](../docs/maintenance-window-upgrade.md)。

| 组件 | 入口与用途 |
| --- | --- |
| `container` | `replace.py`：替换受影响的独立 API、VM、vmagent、node/DCU exporter，启动后最多 90 秒健康与数据验收 |
| `image` | `perses/performance/image_release.py`：按 release lock 加载/更新 Perses 镜像，保留 systemd、访问控制和完整资源读回 |
| `dashboards` | `perses/project_release.py`：快照、并发编辑检查、逐项日志、资源读回和受影响查询验收 |
| `runtime` | `perses/reorg_runtime.py`：同步生成器和资源，保留原字节日志与并发检查 |
| `acceleration-ready` | `perses_acceleration/api_readiness.py`：通用 API 容器替换后的预计算启动检查 |
| `acceleration` / `merges` | 预计算分组/查询合并的专项准入与发布；详见 [加速运维](perses_acceleration/README.md) |

例如 `python3 deploy/release.py dashboards --help` 显示实际子命令参数。分组件实现可独立导入，后续功能改动复用这些实现，不新增按功能或日期命名的发布脚本。Perses 发布共用 `perses/release_support.py` 的完整资源快照、原子证据写入、读回和失败留证。

首次安装保留 `start.py`、`start_test4.py`；公共容器校验为 `container_validation.py`。采集输入为 `scrape.yml`、`profile_a3_scrape.yml`、`xpu_hardware_scrape.yml`。只读网关历史检查已迁到 `scripts/check_gateway_history.py`。

## 证据与故障处理

发布前显式创建新的证据目录；服务器新增目录须告知用户。容器替换要求 `--evidence DIR`，保存原容器身份、配置摘要、目标镜像、事务、成功或失败结果，不将容器环境变量写入证据。看板、生成器和加速工具分别保留快照与逐项日志。stdout/stderr 也保存到该批目录。

失败可能留下部分更新或停止的容器；先读实际状态和日志，再修复当前版本并重新验证。所有回退操作已从现行工具删除。快照和旧字节用于排查与并发核对，不能用删除日志、覆盖旧快照或伪造成功标记跳过失败批次。

这些修改需同步到服务器后才影响服务器工具；已运行进程和旧副本不会自动更新。

## 历史材料

已完成批次的 Python 发布/候选/验证脚本、源代码快照和旧看板基线已移出工作树，检索方式见 [退役清单](../docs/releases/retired-code.md)。带日期的目录仅保留追溯用 Dockerfile、manifest、panels 等输入及归档导航，不能作为新发布计划重跑。

历史正文集中于 [发布归档](../docs/releases/README.md)。`evidence/`、`work/`、运行日志、凭据和镜像归档不入库；版本锁、源输入摘要和校验和继续入库，不按扩展名批量忽略。
