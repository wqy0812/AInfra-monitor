# 部署工具

现行发布命令统一为 `python3 deploy/release.py <组件> ...`。构建在具备 Git 检出和目标架构 Docker 的主机执行，容器替换与验收在目标主机本地执行。连接与传输遵循工作区 SSH MCP 约束；工具自身不建立远程连接。默认执行 [停机窗口升级](../docs/maintenance-window-upgrade.md)。

| 组件 | 入口与用途 |
| --- | --- |
| `api-image` | `api_image.py`：从明确的 Git commit 和本地基础镜像 ID 构建 API，固定源码、依赖检查、Dockerfile 及最终镜像身份 |
| `container` | `replace.py`：替换受影响的独立 API、VM、vmagent、node/DCU exporter，启动后最多 90 秒健康与数据验收 |
| `image` | `perses/performance/image_release.py`：按 release lock 加载/更新 Perses 镜像，保留 systemd、访问控制和完整资源读回 |
| `dashboards` | `perses/project_release.py`：快照、并发编辑检查、逐项日志、资源读回和受影响查询验收 |
| `runtime` | `perses/reorg_runtime.py`：同步生成器和资源，保留原字节日志与并发检查 |
| `acceleration-ready` | `perses_acceleration/api_readiness.py`：通用 API 容器替换后的原生记录及只读 API 检查 |
| `acceleration` | 官方 vmalert 构建、发布与逐规则验收；见 [原生规则运维](../docs/native-recording-rules.md) |
| `merges` | 既有原始查询的合并维护 |

例如 `python3 deploy/release.py dashboards --help` 显示实际子命令参数。分组件实现可独立导入，后续功能改动复用这些实现，不新增按功能或日期命名的发布脚本。Perses 发布共用 `perses/release_support.py` 的完整资源快照、原子证据写入、读回和失败留证。

首次安装保留 `start.py`、`start_test4.py`；公共容器校验为 `container_validation.py`。采集输入为 `scrape.yml`、`profile_a3_scrape.yml`、`xpu_hardware_scrape.yml`。只读网关历史检查已迁到 `scripts/check_gateway_history.py`。

## API 镜像构建

固定模板为 [Dockerfile.api](Dockerfile.api)。构建从指定 commit 读取完整 `monitoring/`、`requirements.txt` 和模板；不打包未提交文件。模板先核对基础镜像已安装的依赖版本，再删除原 API 源码目录并完整复制，避免旧模块在多次覆盖构建后残留。不更新依赖、不联网安装、不替换容器。

```sh
# 构建主机：使用新的空目录，显式提供完整本地镜像 ID。
mkdir -p evidence/api-build-<批次>
python3 deploy/release.py api-image <commit> \
  --base-image sha256:<已核验的64位镜像ID> \
  --evidence evidence/api-build-<批次>
```

基础镜像必须为本地 Linux 镜像，架构须匹配部署目标，且包含该 commit 的精确 Python 依赖；不接受可变 tag。为兼容 BuildKit 的 `FROM` 语法，工具给该 ID 添加按内容命名的本地 `monitoring-api-build-base:sha256-<ID>` 别名，构建前后核对身份并保留别名，不删除基础镜像。依赖不符或缺失时构建失败，先准备并核验匹配的运行时镜像，再用新证据目录构建，不能跳过检查。此入口仅负责源码镜像，不提供依赖运行时的下载或升级。

`api-build-inputs.json` 记录 commit、逐文件 SHA-256、源码归档和基础镜像身份，`api-source.tar` 保存 Docker 实际接收的输入，`api-build.json` 记录最终不可变镜像 ID。失败保留输入、日志及 `api-build-failure.json`，不覆盖原批次。构建成功不代表服务验收成功。

异机构建时使用 `docker save` / `docker load` 传递镜像，归档放 `work/`，传输前后核对归档 SHA-256，加载后核对 `api-build.json` 中的镜像 ID。发布仍使用 `python3 deploy/release.py container monitoring-api <最终镜像ID> --evidence <新的发布证据目录>`，保留当前 state 挂载和运行参数；涉及加速服务时继续执行 `acceleration-ready`。构建与替换证据使用不同目录，服务器新增目录须在操作结果中告知。

## 证据与故障处理

发布前显式创建新的证据目录；服务器新增目录须告知用户。容器替换要求 `--evidence DIR`，保存原容器身份、配置摘要、目标镜像、事务、成功或失败结果，不将容器环境变量写入证据。看板、生成器和加速工具分别保留快照与逐项日志。stdout/stderr 也保存到该批目录。

失败可能留下部分更新或停止的容器；先读实际状态和日志，再修复当前版本并重新验证。所有回退操作已从现行工具删除。快照和旧字节用于排查与并发核对，不能用删除日志、覆盖旧快照或伪造成功标记跳过失败批次。

这些修改需同步到服务器后才影响服务器工具；已运行进程和旧副本不会自动更新。

## 历史材料

已完成批次的 Python 发布/候选/验证脚本、源代码快照和旧看板基线已移出工作树，检索方式见 [退役清单](../docs/releases/retired-code.md)。现有带日期的目录冻结，仅保留追溯用 Dockerfile、manifest、panels 等输入及归档导航，不能作为新发布计划重跑；新构建复用固定模板，产物和执行记录写入 `work/` / `evidence/`，不新增日期源码目录。

历史正文集中于 [发布归档](../docs/releases/README.md)。`evidence/`、`work/`、运行日志、凭据和镜像归档不入库；版本锁、源输入摘要和校验和继续入库，不按扩展名批量忽略。
