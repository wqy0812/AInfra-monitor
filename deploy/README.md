# 部署工具与历史发布包

本目录保存部署代码、校验工具、采集配置及历史发布输入。所有服务器调用与传输均经 SSH MCP；这些 Python 脚本在目标主机本地运行，不负责建立远程连接。

| 用途 | 文件或目录 |
| --- | --- |
| 启动、容器替换及公共校验 | `start.py`、`start_test4.py`、`replace.py`、`container_validation.py` |
| 采集配置 | `scrape.yml`、`profile_a3_scrape.yml`、`xpu_hardware_scrape.yml` |
| 专项发布及核验 | 顶层 `*_release.py`、`*_verify.py`、`check_*.py`；按各专项文档的版本约束使用 |
| 按日期保存的历史发布包 | `history-summary-20260924/`、`review-fixes-20260924/`、`stream-direction-20260923/`、`xpu-cache-20260923/` |
| 时间导航与历史缓存发布 | [time-navigation-20260925](time-navigation-20260925/README.md) |
| 查询加速 | [perses_acceleration](perses_acceleration/README.md) |

历史发布过程统一见 [发布归档](../docs/releases/README.md)。原目录中的 `STATUS.md` 保留导航；脚本、输入清单和源码快照暂不搬动，避免破坏路径加载、源码摘要和回退约束。

## Git 边界

- 保留：源码、采集配置、发布脚本读取的 `manifest.json` / `*-manifest.json` / `panels.json`，以及版本锁和校验和。
- 排除：`complete.json` / `*-complete.json`、代理验证结果、运行日志和容器快照。本地留存统一放到仓库根目录 `evidence/<批次>/`；9 个历史结果已迁移，清单见归档索引。
- 远端历史脚本的输出和回退文件布局维持原约定；下载留存时放到本地证据目录。本轮仅整理本地仓库，未改动远端发布流程。

`.gitignore` 同时排除发布目录中再次出现的完成报告及 Perses 代理验证报告，防止误加入 Git；没有笼统忽略 JSON 或 manifest。
