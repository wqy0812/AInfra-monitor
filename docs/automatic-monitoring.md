# code-eval 登记驱动的自动监控

本文描述已实现的接入协议和部署入口。代码验证、线上部署与真实业务验收分别记录；发布身份及验收结论仍以 `docs/releases/README.md` 和本次证据为准。

退役专属项目清理已在当前源码实现，本轮尚未部署。后续按停机窗口更新 controller、API 和 probe worker，保留原受管目录和持久化状态；再更新 code-eval 配套组件。启动会补清理原有退役对象，验收需核对在用底座新鲜度、退役资源消失及清理读回状态。

相关本地回归：`.venv/bin/python -m pytest tests/test_monitoring_controller.py tests/test_managed_probes.py tests/test_source_catalog.py -q`。真实 VM 生命周期使用 `MONITORING_MANAGED_E2E=1 .venv/bin/python -m pytest tests/test_managed_monitoring_e2e.py -q`；独立 Perses 删除契约使用 `MONITORING_PERSES_E2E=1 .venv/bin/python -m pytest tests/test_perses_retirement.py -q`，预先准备 `perses/image-lock.json` 固定的镜像。两项仅使用本次临时容器、合成服务和数据，结束清理测试容器与网络，保留结果证据。

code-eval 是底座、exporter、固定 Router/Proxy 与网关入口的登记权威。engine 在保存后同步，并每 15 秒补偿同步；独立 `monitoring-controller` 持久化目录版本，编译采集、记录规则和 Perses 资源。vmagent、VM、vmalert、只读 API 与探测 worker 继续执行各自职责。新增同类底座无需编写采集配置或复制线上看板。

## 登记和能力

首版适配器为 `dcu-sglang-v1`、`a3-vllm-v1`、`xpu-sglang-v1`。环境名称和节点名不决定指标口径；适配器复用当前 SGLang/vLLM、加速卡、主机及 Mooncake 统计定义。节点和实例数量来自登记，多个同角色节点保留明细并按原始桶汇总延迟分位数。未支持的指标族通过新版本适配器扩展。

新底座以稳定 source ID 查询；已有三个环境保留旧 source ID、job、instance 和静态标签。迁移专用 `scrape_labels`、`scrape_job`、`scrape_metric_relabel_configs` 保留原始采集身份及指标过滤，普通编辑不丢弃这些元数据。地址变化是新时序身份，旧历史保留。

入口检测由 monitoring 主机发起，最大 8 秒、4 MiB，支持无认证和 Basic Auth；验证指标格式与适配器指标族。类型不匹配的入口不会加入抓取；暂时不可达的入口继续由 vmagent 展示真实采集故障。状态分别报告配置应用、入口检查和最近抓取健康/时间，不把配置已应用当成业务正常。缺少信息的入口显示待补充，其他有效入口仍可接入。

Proxy 观测地址自动提供网关指标和画像/异常查询。资源 exporter 不会生成请求明细；未登记观测地址时，画像和异常诊断保持待配置。异常列表、证据和上下文复用既有接口。

## 目录同步和恢复

控制接口必须使用 TLS、专用 Bearer token 与明确来源 IP 白名单。token 使用 `MONITOR_CONTROL_TOKEN_FILE`，不放入命令参数或公开状态。

| 接口 | 行为 |
| --- | --- |
| `PUT /internal/monitoring/v1/catalog` | 接收 `schema_version=1`、递增 `revision`、foundations 和 gateways 完整目录；同版本同内容幂等续期，旧版本或同版本异内容返回 409 |
| `GET /internal/monitoring/v1/integrations` | 只读返回接收/应用版本、分组件状态、入口检查、采集新鲜度及 Perses 链接 |
| `POST /internal/monitoring/v1/reconcile` | 按 `expected_revision` 重试；`force_perses=true` 仅在用户已确认的冲突内容未再次变化时重建指定底座 |
| `POST /internal/monitoring/v1/probes/pause` | 持久化 `router_id`/`operation_id` 的暂停，等待 worker 的新鲜、同操作停止派发确认；在途请求正常收尾 |
| `POST /internal/monitoring/v1/probes/resume` | 显式恢复；重复旧恢复不会清除更新的维护暂停，已结束暂停操作不能迟到后重新生效 |

controller 运行目录包含 `desired.json`、`integration-state.json`、`scrape.json`、`rules.json` 和受限 `secrets/`。采集配置通过 `authorization.credentials_file` 引用私有 Basic 编码文件；文件保存用户名与原始密码的 Base64 编码，避免 native 读取密码文件时裁剪有意义的首尾空白。公开目录和状态不含凭据。每个版本的秘密文件独立保存；新配置读回确认后清理不再被当前登记或生效配置引用的秘密文件。

`catalog.json` 是采集和规则验证生效后的目录；`probes-catalog.json` 是最新接受的探测拓扑，停用/删除可立即停止探测，不依赖采集修复完成。文件路径分别由 `MONITORING_CATALOG_FILE` 和 `MONITORING_PROBE_CATALOG_FILE` 指定。`probes-control.json` 保存续期和显式暂停，`probes-runtime.json` 保存 worker 的实际目录版本、目标身份、暂停确认和在途请求。

编译先调用已部署固定版本 vmagent/vmalert 的 dry-run；成功后原子替换文件、重载并核对实际目标、带配置指纹的采集池以及规则名称、表达式和标签。采集池名称包含配置摘要，最终时序的 job 标签保持原身份，因此凭据或过滤规则单独变化也能确认已生效。不确定结果通过读回恢复，不重复创建 Perses 资源。受管配置文件或 Perses 资源被并发改动时保留冲突，停止覆盖；故障按当前版本修复，没有版本回退路径。

底座退役（`enabled=false`）后退出运行查询和探测目录，停止新探测，已发请求按原时限收尾；采集和规则移除后核对 native 实际配置。controller 删除该底座整个专属 Perses 项目，包括手工新增或修改的看板和项目数据源；全局、共享和其他底座的资源保留。资源归属冲突停止清理并报告失败。

清理前在 `STATE_DIR/retirement-evidence/` 保存 0600 的资源内容及摘要，持久化删除意图并逐项读回，按看板、项目数据源、项目的顺序删除。并发变化保留冲突，重试时重新留证；请求结果不明或重启后先查询是否已经删除。采集、规则、专属项目和探测进程的新鲜停止／在途收尾确认全部完成，接入状态才成为 `disabled`；失败不会被退役标记掩盖。既有退役登记在 controller 启动后同样清理，不需要新目录版本；重新生成入口拒绝重建退役资源。底座资料、VM 历史、发送缓冲和处理水位保留。

## Perses 和双层探测

Perses 从同一源码版本的 `dashboard_columns.py` 与 `projects/` 模板生成资源，可创建空的新项目；全部表达式通过共享来源绑定器替换环境、节点、实例和覆盖数量。已有项目保留七类入口及面板身份。新项目按已登记能力生成分区；能力移除后保留已经发布的历史资源。

每个已关联网关入口每分钟探测一次，成功结果按实际 Router 共享给底座固定 Proxy/Router；底座仅在没有覆盖或入口失败、缺测时独立探测，同分钟轮次最多一次。新目标使用稳定偏移，原三个入口保留 0/20/40 秒。底座按 `foundation:<id>` 归属；入口按 `gateway:<id>` 归属，切流不改写旧入口历史。探测沿用短提示、64 token 输出预算、50 秒总时限及完整 SSE 判定，既不补发，也不因导入失败重新生成。

底座探测许可超过 30 秒未续期时暂停；Router 维护先取得停止派发确认。网关入口继续使用普通业务准入。旧入口历史仅通过显式 `legacy_probe_environment` 和 `probe_migrated_at` 边界接回，不能根据名称或当前 Router 推断旧归属。

## 维护窗口接管

所有步骤从 `deploy/release.py integrations` 进入，在目标机本地运行，通过工作区规定的 SSH MCP 连接。主机仅需要 Python 标准库和 Docker；准备阶段通过指定的不可变 API 镜像使用运行依赖。

1. 从当前线上采集配置、Perses 和 code-eval 读取生成经过核对的迁移目录。准备目录中的 Basic 密码不得写入 Git；可先用三个无认证旧环境引导，随后由 engine 经 TLS 同步完整目录。
2. 使用新源码 commit 构建 API 和 native 镜像，保留基底镜像和最终镜像身份。准备控制 token、受信任 TLS 证书与密钥，复用 Perses 验证过的 CA 和登录凭据文件。
3. `prepare --image IMAGE --catalog CATALOG --state STATE --templates PERSES_RUNTIME --evidence EVIDENCE --adopt-perses --perses-credentials FILE --perses-ca FILE` 生成初始受管文件，读取现有 Perses 资源作为接管基线并保存哈希与证据。`STATE` 必须是空目录；模板目录必须包含 `dashboard_columns.py` 和 `projects/`。
4. `install --image API_IMAGE --native-image NATIVE_IMAGE --state STATE --evidence NEW_EVIDENCE --token-file FILE --tls-cert FILE --tls-key FILE --perses-credentials FILE --perses-ca FILE --allowed-clients IP_LIST --controller-url HTTPS_URL` 校验准备输入，停止原探测 worker，记录确切历史切换时间，再替换受影响消费者并启动 TLS controller。API、vmagent 和 vmalert 只读挂载受管目录；probe worker/controller 写入自己的运行状态，VM 和发送缓冲挂载保持不变。
5. `installation.json` 返回 `probe_cutover_at`；code-eval 数据迁移使用相同时间，避免丢失切换窗口的新探测记录。再启用 engine 的同步 URL/token/CA，检查接收和应用版本、采集新鲜度、Perses 和正常周期产生的两类探测记录。

安装器复制当前已部署的 native 二进制进行校验，不联网下载依赖；源码模板只读挂载并记录哈希。输出中的 `created_directories` 必须在交付说明中告知用户。失败保留新旧容器证据并修复当前版本；不恢复旧容器、不清空历史、不额外发起真实模型冒烟。
