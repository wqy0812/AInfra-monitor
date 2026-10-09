# Perses 看板与维护

现行定义位于 `projects/`：DCU、A3、XPU 各 10 张专业看板，共 281 个面板。非缓存看板使用公共核心及平台扩展，保留平台真实的指标、角色和统计口径；A3 分 Prefill/Decode，主机与加速卡分开。

默认数据源直接查询 VM，`perses-accelerated` 为独立专用数据源。生成器读取 `acceleration_state.json` 保留已准入的 14 个合并面板和 CPU/DCU/A3 三组绑定，当前 JSON 有 18 个加速面板。这是仓库配置，线上一致性和健康需操作时检查。详见 [当前加速清单](../deploy/perses_acceleration/STATUS.md) 和 [查询口径](../docs/perses-query-acceleration.md)。

## 生成与初始化

在本目录执行 `python3 project_split.py` 再生成项目资源，执行 `python3 project_coverage.py --docs-only` 同步图表说明。当前生成器仍读取现有项目 JSON 并保留编辑；不承诺从空目录重建。部分迁移/查询辅助模块仍被导入，本次清理保留这些依赖。

`seed.py` 仅创建缺失资源，保留服务器已有内容。旧顶层 `dashboards/`、单项目 project/datasource 基线和专题发布器已退役，查阅原文件见 [退役清单](../docs/releases/retired-code.md)。`generate.py`、`gateway_generation.py`、`compare.py`、`validate.py` 保留到现行工具的简短兼容入口。

## 发布

在仓库根目录执行：

```sh
python3 deploy/release.py dashboards prepare --evidence DIR
python3 deploy/release.py dashboards apply --evidence DIR --resources perses/projects
```

`DIR` 必须是新建的证据目录。先读取服务器当前资源，核对网页编辑与本次输入，再发布。保留结构校验、并发检查、逐项日志与读回；仅核验受影响图表及其变量/数据源依赖。失败留存已写资源与原始错误，修复当前版本，无回退命令。

`audit-published --evidence DIR` 根据发布前快照选择受影响图表；没有基线时检查全部，`--full-audit` 显式扩大范围。`audit` 用于复杂查询改动的发布前核验，`check_project_semantics.py DIR` 仅在查询构造器或统计口径变化时执行。

生成器安装使用 `deploy/release.py runtime apply --evidence DIR --runtime TARGET`，输入为 `DIR/release/`；保留字节日志并防止覆盖并发编辑。Perses 镜像使用 `deploy/release.py image load|apply`，构建和锁定版本见 [performance](performance/README.md)。凭据在目标机 `admin-credentials.json`，可通过 `PERSES_CREDENTIALS_FILE` 指定。

连接、停机、测试范围和数据保护遵循 [停机窗口升级](../docs/maintenance-window-upgrade.md)。查询合并和数据源切换的专项覆盖、正确性与性能验收见 [加速运维](../deploy/perses_acceleration/README.md)。

图表含义见 [METRICS_GUIDE](METRICS_GUIDE.md)，采集基线见 [METRIC_COVERAGE](METRIC_COVERAGE.md)。生效时间和真实断档说明保留在面板及相关口径记录中；历史安装、分类迁移与发布过程见 [归档](../docs/releases/README.md)。
