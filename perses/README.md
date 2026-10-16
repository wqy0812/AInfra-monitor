# Perses 看板与维护

入口使用 [HTTPS](https://122.247.53.162:18431)，支持 HTTP/2。证书为自签 IP 证书，SAN 为 `IP:122.247.53.162`；访问设备需要信任证书。服务器证书位于 `/data2/monitoring/perses/tls/server.crt`，私钥 `server.key` 仅保留服务器，由容器用户只读访问。证书到期前需更新证书并重启 Perses。

维护客户端默认使用 HTTPS，在服务器上自动加载上述证书进行校验。本机执行时设置 `PERSES_CA_FILE=/path/to/server.crt`；`PERSES_URL` 可指定其他环境。不要关闭证书验证。镜像替换必须保留证书挂载和全部 `--web.tls-*` 参数；登录 Cookie 使用 `security.cookie.secure: true`。

首次切换使用 `perses/tls_setup.py --help`，核对容器 ID、镜像 ID 和配置摘要后在停机窗口执行。工具创建证书、保留资源、替换同版本容器，并检查 TLS、HTTP/2、鉴权和数据源代理；失败保留现场并修复当前版本。

DCU、A3、XPU 各有 7 个公开入口，共 261 个面板：总览、网关监控、模型推理监控、Prefill 诊断、Decode 诊断、主机资源、采集与监控健康。网关监控包含请求流量与质量、在途请求诊断；模型推理监控包含后端请求性能、缓存与存储、加速卡资源。各分区保留原有布局和默认折叠状态，模型看板共用角色、节点和设备筛选；节点和设备仅影响引用它们的图表。

现行分区源定义位于 `projects/`，保留原来的文件及面板标识供查询生成和加速清单使用；`dashboard_columns.py` 在发布、初始化和说明生成时把五个分区源合并为上述两个公开看板，旧源不会作为独立入口发布。发布器在新看板读回与相关查询验收后移除旧入口。总览（`summary`）业务图按角色或节点聚合；新增覆盖和质量图单独解释部分统计。已迁移原生记录的资源作为当前生成输入，重复生成保留其查询与说明；`panel_trim.py` 记录已删除或合并的面板；`drilldown_layout.py` 把明细看板的诊断分组默认折叠，并减少非加速面板的曲线数。非缓存看板使用公共核心及平台扩展，保留平台真实的指标、角色和统计口径；A3 分 Prefill/Decode，主机与加速卡分开。

全部看板直接通过 `victoriametrics` 数据源查询 VM。已移除 `perses-accelerated` 和 API 代理；原有加速目录/发布状态仅用于历史审计，不控制当前发布。新指标和覆盖/质量定义见 [原生记录规则](../docs/native-recording-rules.md)。

原始瞬时值使用共享的 `monitoring/raw_gauges.py` 生成查询，不写重复记录。`runtime apply` 的候选 `release/` 除 Perses 模块及 `projects/` 外，还须包含同一源码版本的 `monitoring/__init__.py`、`monitoring/native_rules.py`、`monitoring/raw_gauges.py`；安装器把该包与生成器一并校验、记入日志并安装，缺少模块会在写入前停止。独立安装后的生成器应能重新生成相同资源，不依赖开发目录的 `PYTHONPATH`。

查询维护工具支持分区源和合并后的线上快照。catalog 和安装清单保留逻辑分区 ID，候选及 API 写回使用实际公开资源 ID；修改合并看板时仍检查整张资源是否被并发编辑。生成器安装将查询变更映射回原分区文件。

查询维护安装器同样将上述三个共享模块纳入源码指纹、运行目录快照和安装日志。旧运行目录缺少 `monitoring/` 时，计划记录该新增目录，安装前校验全部文件及目录状态，再创建目录并输出其路径；其他缺失目录仍须显式准备。候选源码或运行文件变化后须重新审计，不复用旧证据。

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

连接、停机、测试范围和数据保护遵循 [停机窗口升级](../docs/maintenance-window-upgrade.md)。查询合并、改写和加速数据源的专项发布工具已于 2026-10-16 退役（见 [退役清单](../docs/releases/retired-code.md)）；看板变更统一经 `deploy/release.py dashboards` 发布。

图表含义见 [METRICS_GUIDE](METRICS_GUIDE.md)，采集基线见 [METRIC_COVERAGE](METRIC_COVERAGE.md)。生效时间和真实断档说明保留在面板及相关口径记录中；历史安装、分类迁移与发布过程见 [归档](../docs/releases/README.md)。
