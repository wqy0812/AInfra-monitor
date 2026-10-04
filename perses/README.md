# Perses on test4

## 当前分类（2026-09-23）

运行概览已删除，图表迁入网关、后端性能、P/D 诊断、主机、加速卡、缓存和采集健康看板。DCU/XPU/A3 各 10 张，共 301 图；非缓存看板公共核心对齐，保留平台专属扩展，A3 Mooncake 新增 10 图。单位在标题，提示去除重复单位与曲线范围标题。对齐验收与当次发布过程见 [对齐发布记录](../docs/perses-alignment-mooncake-20260923.md)，此前迁移见 [重组记录](../docs/perses-dashboard-reorg-20260923.md)。XPU 角色修复见 [修复记录](../docs/xpu-role-fix-20260929.md)。旧安装、双项目迁移和发布过程见 [历史归档](../docs/releases/perses-history.md)，不能据此恢复旧概览或旧看板数量。

2026-09-30 已清理 XPU 无源缓存图，并补充当前已有指标；缓存看板去除重复的设备 KV 池后现为 6 图，见 [缓存看板更新](../docs/xpu-cache-refresh-20260930.md)。

现行资源：`projects/`；通过 `project_split.py` 再生成、`project_coverage.py --docs-only` 同步说明。发布脚本从服务器 `admin-credentials.json` 读取认证信息（可用 `PERSES_CREDENTIALS_FILE` 指定），不再依赖免登录访问。

## 时间导航与刷新控制已上线（2026-09-25，0.54.0-perf.3）

当前镜像配置摘要为 `sha256:978402ac5154e3ee2cf8c66f244fc7169be74125ef01a5fe9d28c1e8a24764e7`。跨看板、跨项目继承当前标签页时间范围，隐藏页面和固定窗口暂停定时查询，保留手动刷新。按用户要求在本机完成 1080p 浏览器验收；线上核验三个项目的 30 张看板、3 个数据源保持一致，各项目代理真实查询成功。未运行远端浏览器及 30 分钟持续观察。当次保留的旧 perf.2 容器和候选状态见 [历史发布记录](../deploy/time-navigation-20260925/README.md)；这不表示容器目前仍保留，也不是当前回退入口。后续升级按停机窗口验收，故障修复当前版本。

## 资源与维护入口

- `projects/<project>/`：现行项目、默认及专用数据源、看板定义；现有三个项目为 `dcu-monitoring`、`a3-monitoring`、`xpu-monitoring`。
- 顶层 `dashboards/`：历史基线，不作为现行初始化和发布输入。
- `performance/`：上游补丁、构建和相关测试；版本与摘要保留在 source/image/release lock 中。
- `generate.py`、`generate_a3.py`、`gateway_generation.py` 的 CLI 已转到项目生成器；部分旧模块仍提供被导入的辅助函数。退役发布入口保留原文件路径，不作为操作指南。

在 `perses/` 目录执行 `python3 project_split.py` 更新本地项目资源，再用 `python3 project_coverage.py --docs-only` 同步说明；后者沿用已保存的指标覆盖清单，无需历史证据目录。覆盖清单有采集基线日期，不代表当前在线覆盖数量。`seed.py` 仅创建缺失资源，保留服务器已有资源。

服务器发布一律经 SSH MCP，后续按 [停机窗口升级](../docs/maintenance-window-upgrade.md)。对显式看板更新，使用 `project_release.py prepare --evidence DIR`、`project_release.py apply --evidence DIR`；`--resources` 可指定资源目录。发布前保存资源快照并检查并发编辑，发布后读回核对并校验受影响图表及变量、数据源依赖；失败时保留已写资源与日志，修复当前版本后重新验证，不自动回退。`audit` 是复杂查询改动时可选的发布前核验；`check_project_semantics.py DIR` 仅用于查询构造器/统计口径改动的专项语义验收。`audit-published --evidence DIR` 默认按快照选择受影响图表，`--full-audit` 显式检查全部图表；无基线快照时校验全部图表。`validate.py`、`compare.py` 的 CLI 转到该模式，必须提供证据目录。不再要求其他服务的容器 ID/启动时间不变。

本地资源是版本化输入，发布还需核对线上快照并保留网页编辑。首次安装历史见归档；当前镜像构建和升级见 [performance/README.md](performance/README.md)。查询口径见 [图表说明](METRICS_GUIDE.md)、[覆盖说明](METRIC_COVERAGE.md)；Python 回归见 [测试说明](../tests/README.md)。

## Perses 查询加速（2026-09-26）

指标语义、查询合并及回源规则见 [查询加速与原有口径](../docs/perses-query-acceleration.md)。该说明独立维护，生成图表文档时保留入口链接。

生成器通过 `acceleration_publication.py` 读取 `acceleration_state.json`，仅保留已经准入的合并与数据源切换。默认清单为空时保持原路径。专用数据源写入 `perses-accelerated-datasource.json`，不会覆盖默认 `datasource.json`。查询加速的覆盖、正确性、性能准入与故障处置见 [运维说明](../deploy/perses_acceleration/README.md)，实际发布进度见 [状态](../deploy/perses_acceleration/STATUS.md)；普通升级按停机窗口验收，不重跑旧批次的长时间观察，也不执行版本回退。
