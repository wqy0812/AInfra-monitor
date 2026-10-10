# Perses 看板与维护

入口使用 [HTTPS](https://122.247.53.162:18431)，支持 HTTP/2。证书为自签 IP 证书，SAN 为 `IP:122.247.53.162`；访问设备需要信任证书。服务器证书位于 `/data2/monitoring/perses/tls/server.crt`，私钥 `server.key` 仅保留服务器，由容器用户只读访问。证书到期前需更新证书并重启 Perses。

维护客户端默认使用 HTTPS，在服务器上自动加载上述证书进行校验。本机执行时设置 `PERSES_CA_FILE=/path/to/server.crt`；`PERSES_URL` 可指定其他环境。不要关闭证书验证。镜像替换必须保留证书挂载和全部 `--web.tls-*` 参数；登录 Cookie 使用 `security.cookie.secure: true`。

首次切换使用 `perses/tls_setup.py --help`，核对容器 ID、镜像 ID 和配置摘要后在停机窗口执行。工具创建证书、保留资源、替换同版本容器，并检查 TLS、HTTP/2、鉴权和数据源代理；失败保留现场并修复当前版本。

DCU、A3、XPU 各有 7 个公开入口，共 249 个面板：总览、网关监控、模型推理监控、Prefill 诊断、Decode 诊断、主机资源、采集与监控健康。网关监控包含请求流量与质量、在途请求诊断；模型推理监控包含后端请求性能、缓存与存储、加速卡资源。各分区保留原有布局和默认折叠状态，模型看板共用角色、节点和设备筛选；节点和设备仅影响引用它们的图表。

现行分区源定义位于 `projects/`，保留原来的文件及面板标识供查询生成和加速清单使用；`dashboard_columns.py` 在发布、初始化和说明生成时把五个分区源合并为上述两个公开看板，旧源不会作为独立入口发布。发布器在新看板读回与相关查询验收后移除旧入口。总览（`summary`）每图按角色或节点聚合、最多 4 条线，由 `summary_dashboard.py` 根据明细面板的现有查询重建，不保留网页编辑；`panel_trim.py` 记录已删除或合并的面板；`drilldown_layout.py` 把明细看板的诊断分组默认折叠，并减少非加速面板的曲线数。非缓存看板使用公共核心及平台扩展，保留平台真实的指标、角色和统计口径；A3 分 Prefill/Decode，主机与加速卡分开。

默认数据源直接查询 VM，`perses-accelerated` 为独立专用数据源。生成器读取 `acceleration_state.json` 保留已准入的 14 个合并面板和 CPU/DCU/A3 三组绑定，当前 JSON 有 18 个加速面板。这是仓库配置，线上一致性和健康需操作时检查。详见 [当前加速清单](../deploy/perses_acceleration/STATUS.md) 和 [查询口径](../docs/perses-query-acceleration.md)。

查询维护工具支持分区源和合并后的线上快照。catalog 和安装清单保留逻辑分区 ID，候选及 API 写回使用实际公开资源 ID；修改合并看板时仍检查整张资源是否被并发编辑。生成器安装将查询变更映射回原分区文件。

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
