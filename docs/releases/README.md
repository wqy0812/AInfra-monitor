# 发布身份与历史检索

## 2026-10-23 退役监控清理与专属 Perses 项目删除

test4 已按停机窗口更新 monitoring-api、monitoring-probes 和 monitoring-controller，应用源码 `22def3ed726d9f19d09768038d9386c98bbe16a4`（#8 合并提交），三个组件均使用镜像 `sha256:f55312295de2b3dd595ba081283bd98cb45ce2b828cb69296bd7bc87e4217c97`，restart_count=0；每个运行包 40 个源码文件与确定提交构建输入逐项一致。

本轮首次使用原 API 镜像 `sha256:f8f919d9127865ac8eaf46d592e537aafb950d3c810a2960284552a4bdfe2776` 构建时触及 Docker 镜像层数上限，尚未替换服务即停止。保留失败日志后，改用已安装、精确依赖匹配且层数更少的 `sha256:b69a6553ec3a25e6260298bf1dd46bcda4e6b29163d454e4171f2c1bddecc8e7` 作为依赖基底，离线重建当前完整源码；没有启动旧版服务。

控制器启动补清理已有 PPU 退役登记，四项 scrape/rules/perses/probes 均为 disabled，专属项目 `foundation-1cb92478d6194655a048ec7d29778a98` 已确认不存在。该对象此前 Perses 报错且 applied_revision 停在 3；更新后 accepted_revision=applied_revision=18。5 个在用底座均 ready/healthy，95 个采集目标全部 up，快照年龄约 17.5–22.5 秒，探测 worker 有 10 个目标；发布前历史窗口仍可查询，A3 返回 26 条 up 历史序列。

保留原 state 目录、目录版本、凭据、VM 数据挂载、发送缓冲和处理水位。替换组件的启动参数、环境、资源与访问控制挂载均保持；VM、vmagent、vmalert 和 Perses 没有替换。退役删除按当前源码保留归属检查、删除意图和读回流程；本次实际旧退役项目原本已不存在，不能将其验收解释为线上删除了有内容的项目。手工资源删除和中断恢复由此前合成回归覆盖，本轮未重复运行真实容器合成验收。

配套 code-eval 源码 `cd493ba219ab74ae0ab276065fa25547020cf68f` 已部署 test1，并于北京时间 17:40:26 完成检查、恢复原准入。线上桌面 1280×720 和 iPhone 竖屏确认退役资源过滤与清理状态，无业务写入；没有为验收额外生成模型请求。code-eval #21 已知在途切流与退役竞态尚未修复，详见平台发布记录。

test4 新增 `/data2/monitoring/releases/retirement-20261023/`，含 source、首次失败的 build、成功的 build-r2、三个组件 window、checks 及受限 private。完成回执：`evidence/retirement-20261023/checks/completed.json`；构建身份：`evidence/retirement-20261023/api-build.json`；失败证据同目录保留。完整部署配置仅保存在服务器 private/。

## 2026-10-22 switch 优先的链路共享探测

已在 test4 更新 monitoring-api 与 monitoring-probes，应用源码 `b2933af18c687d610e583659be9ec88cf04c63a0`，镜像均为 `sha256:f8f919d9127865ac8eaf46d592e537aafb950d3c810a2960284552a4bdfe2776`，固定基底 `sha256:7a64ca38e555adeaabb17043b0c85252fd5b350a83487c7693327c2041c3f2cd`。两个运行容器各 40 个源码/资源文件与构建输入逐项一致，restart_count=0；controller、VM、vmagent、vmalert、Perses 与登记目录保持既有部署。

入口每分钟优先发起真实 SSE 请求，成功后按响应实际 Router 及派发时底座快照写入共享观测。未覆盖、入口失败或缺测时才派发独立底座请求；维护、许可和串行约束保留。观测新增来源、来源 ID 和计划时间，旧记录继续读取。部署工具的受管 worker 健康校验改为动态目录，旧三入口模式仍严格校验三个目标。

上线自然定时观测核对北京时间 17:33—17:38 六个完整分钟：每分钟 5 个 switch 请求、5 份共享底座观测、0 个独立请求，最新 10 个服务全部成功。启动的首个不完整周期保留一次真实独立探测，不补造旧入口的请求或共享结果。没有为验收额外生成模型请求或执行真实切流/故障注入。

相关回归 194 passed，真实 VM 2 passed，包含 VM/vmagent/vmalert/Proxy/Perses 的完整受管生命周期 1 passed。首次生命周期测试的工作区不在 Docker 共享目录内，转入已共享的 `monitoring/work/switch-first-e2e` 后通过。切流、失败恢复、身份缺失、迟到结果和串行派发使用合成服务回归；真实模型仅消费上线定时探测。

配套 code-eval Web 应用 `c44ebec12d35eab7fb9695ffe7407047b505e59c` 展示最近 6 小时紧凑卡片，桌面三列、手机单列并标明共享来源。前端 65 项单测、lint、构建以及合成/线上桌面 1280×720 与 390×844 触控模拟通过。平台按同一停机窗口完成核验并恢复原准入状态。

新增 test4 `/data2/monitoring/releases/switch-first-probes-20261022/`，包含 source、build、api-window、probes-window 和 checks。公开运行字节与观测收据保存在 checks；VM 历史、缓冲与处理水位保留。应用及交付文档位于本地 `codex/switch-first-probes` 分支，未推送。平台镜像与配套窗口见其发布身份记录。

## 2026-10-22 code-eval 登记驱动的自动接入

最终 API/controller 已更新并通过核验，95 目标全部 up，各容器 restart_count=0。平台于北京时间 13:53:47 确认恢复原 `maintenance=false`；修复后的 A3 一小时历史实际返回 HTTP 200，三个 A3 来源分别约 1.04/0.50/0.90 秒，桌面与手机各两次复核通过。

本轮新增 HTTPS/token/IP 白名单控制器，由 code-eval 登记驱动采集配置、原生规则、Perses 和两类探测目录。应用实现为 `b49c19659008cbcf7dcf0653e9cd5fe91c458f2f`，SGLang 指标兼容修复为 `88ca6f216c0bd856621373b9b18ac60100d4ed26`，历史响应组装和完整采集状态发布修复为 `71a581b911cdb5b4ca92adb4335039f351a7ce9f`。源码位于本地 `codex/automatic-monitoring-integration`，未推送；各组件身份分别记录，不能用仓库 HEAD 代替所有运行镜像。

| 组件 | 发布镜像 | 应用源码 |
| --- | --- | --- |
| API、controller | `sha256:7a64ca38e555adeaabb17043b0c85252fd5b350a83487c7693327c2041c3f2cd` | `71a581b911cdb5b4ca92adb4335039f351a7ce9f` |
| probes | `sha256:2ee8561899ad9a110d3c263a5a281b6b78bc48c9bdebacf64f179a4473c79f2e` | `b49c19659008cbcf7dcf0653e9cd5fe91c458f2f` |
| native vmalert | `sha256:54f478ce5bfaab5fb1f98b73fdacf165e5ffa97c94cfa172af1da110dc5dcf76` | `b49c19659008cbcf7dcf0653e9cd5fe91c458f2f` |
| vmagent | `sha256:d57065b16cffceb0ae46de4c358121386a7f0a98b381a65f0aa3d2515beeae8b` | 本轮替换配置挂载，复用原二进制 |

统一按停机窗口更新受影响组件，保留 VM 原容器/数据卷、发送缓冲和处理水位。接管原 DCU、A3-1/2、XPU，并由登记自动加入 A3-3/4、A3-5/6。5 来源、88 个登记 exporter、95 个采集目标；原 43 个目标的 URL、有效标签、间隔、超时和过滤规则逐项完全一致，仅新增 52 个目标。原历史没有复制或改写，旧探测停止时间 `1792646681.8726075` 同时写入平台与探测目录作为准确分界。

独立线上核验覆盖 366 条规则、5 项目/35 看板及全部 45 个 Perses 资源摘要；88 入口与 95 目标健康，5 来源 P/D 覆盖完整、采样年龄小于 30 秒，5 底座和 5 入口的正常定时流式探测全部成功且 Router 匹配。PPU 为 `not_configured` 且不在探测目录。TLS 验证、匿名/错误 token 拒绝、私有文件权限、24 小时前历史和缓冲状态通过。Perses 1080p 全部 35 页及修复后 6 个代表页通过，39 个真实查询中 35 个有时序，4 个无业务数据的空集合保留。

现场 SGLang 返回 OpenMetrics 下划线转义名称，首版检查器误判为类型不符；修复同时支持冒号与下划线。新 A3 的 18082/18084 原白名单缺少 test4，按来源精确补入，控制端口仍拒绝。ACL unit restart 意外触发 A3-3/5 的 switch/proxy 级联重启（北京时间 13:35:26–29），随后恢复绑定、准入和正常探测；模型 ID/PID 保持。日志不能证明停止瞬间没有在途请求，影响与失败证据保留，维护说明已改为直接执行规则脚本。A3 本轮没有新增目录。

平台复测发现 A3 一小时历史 503：7 个 VM 查询各 0.039–0.090 秒，Python 组装却为 361 个时间点初始化 24,126 次、重建来源 52,704 次，插桩耗时 10.613 秒。修复按时间点初始化、按序列解析路径及角色；真实 79 条序列/360 点的前后完整结果 SHA-256 相同，本地组装从 2.428 秒降到 0.055 秒。控制器同时改为采集读回后原子发布完整状态，避免异步中间态丢失 collection 字段或误报健康。未放宽超时、历史缺口或指标新鲜度口径。

发布前全量 724 passed、654 条环境条件 skipped；另执行真实 VM 回归与完整 VM/vmagent/vmalert/合成 Proxy/Perses 端到端验收。现场修复相关 144 passed、8 条需隔离 VM 的用例 skipped，并以真实线上 payload 做等价核验。最终服务与平台恢复回执在 `evidence/automatic-monitoring-20261022/`，本轮失败日志、修复前审计与计时证据分别保留，不覆盖失败结论。配套 code-eval 应用为 `fe361c68eda8fc704b56666ed7a61e6c9fabdf11`，平台迁移、桌面及手机验收见其发布记录。

test4 新增 `/data2/monitoring/release/automatic-monitoring-20261022/`（含 source）、`/data2/monitoring/evidence/automatic-monitoring-20261022/`（构建、安装及两轮修复证据）、`/data2/monitoring/control/` 和 `/data2/monitoring/state/managed/`（目录、规则、凭据、二进制和运行状态）。完整目录清单保存于本批 `created-directories.json`；token 和私有部署配置不进入公开证据。

## 2026-10-21 底座目录配套监控来源接口

源码 `eeebf3454fe9c11d6ca6b416eae0d172ee43c974` 已推送 main，test4 仅替换 monitoring-api。固定基底为 `sha256:b69a6553ec3a25e6260298bf1dd46bcda4e6b29163d454e4171f2c1bddecc8e7`，最终镜像为 `sha256:24e6e5f0c09ad40829976a696443c6cc658bb15295826283532691c51cdb8e7e`。使用通用 api-image 工具按精确提交、精确依赖禁网构建，36 个运行包文件与输入清单一致；API 运行参数、环境、HostConfig 和 state 挂载保持，restart_count=0。

新增只读 `/api/monitoring/sources` 目录，latest/history 响应增加同源元数据，供 code-eval 的底座登记、固定 Router 监控与画像使用。没有改变 VM 统计查询或采集口径。相关回归 29 项已通过；线上三环境 health=ok、Prefill/Decode 来源完整、快照年龄小于 30 秒，新来源目录和 latest 元数据通过。固定 10 分钟历史窗口的三环境各返回 113 点，来源身份一致；平台实际 Web 可读，Mac 非白名单访问新目录仍为 403。

本次只替换 API，未更新 probes、VM、vmagent、vmalert、Perses、gate 或模型服务，保留历史、发送缓冲、处理水位与访问控制。配套 code-eval 应用 `afa6e15ffcf4617c6bf100e8c65e5ff4c07ebb3c` 已发布 test1 engine/Web；平台本轮按用户要求不更新 gate。未执行真实模型或判题容器测试。

test4 新增 `/data2/monitoring/releases/foundation-router-20261021/`（repo、build、window、checks、private），完整部署快照保存在受限目录。脱敏完成回执在本地 `evidence/foundation-router-20261021/checks/completed.json`；code-eval 维护窗口与线上桌面/手机验收见其 `deploy/releases/README.md`。一次后台上传排队超时明确未启动，改用前台 SSH MCP 上传后成功；未重复发送未知结果的操作。

## 2026-10-20 性能报告服务缓存观测

北京时间 20:51:07 完成修复版 API 替换，配套 code-eval 于 20:54:01 完成验收并恢复准入。源码 `5de56302a90ecfb657cae4198eb79abc7feb2cc0`，最终镜像 `sha256:b69a6553ec3a25e6260298bf1dd46bcda4e6b29163d454e4171f2c1bddecc8e7`，固定基底 `sha256:922eb778d62a1a05a558188a513212db551db3ae5863394773fb7afdb25ce157`；36 个 API 包内文件逐项与精确提交构建清单一致。源码为本地提交，未推送。

新增实际后端端点到监控组的版本化配置，以及来源解析、报告观测两个只读查询接口；沿用传输层 IP 白名单、原生缓存指标和既有并发限制。DCU/A3 独立计算不重叠 30 秒计数增量，XPU 保存原生比例的有效时长加权值；趋势保留各自原生口径。来源按实际后端和核验的有效期选择，模型文字及网关所在环境不参与推断。说明见[服务缓存观测](../report-cache-observations.md)。

首次 API 提交 `30d3699a5e594b8cb43c08f3df13f781efa1ad4c`、镜像 `sha256:f6beac848fd51459370d6297198ddebe0497e7a169838bc4a7f901646a2ba0ec` 的当前短窗口查询正常，但真实历史补录暴露 VM 对 50 点以上缓存查询的时间轴调整，12 份报告均无有效匹配。保留首次构建、替换及补录回执，修复报告专用查询 `nocache=1`，独立 VM 用 60 点、带小数起点的历史用例验证后发布当前版本；没有回退或改动普通看板的缓存参数。重新补录的 12 份报告均 complete、覆盖 100%。

相关回归 62 passed、6 skipped（需显式隔离 VM 的既有用例）；本轮独立真实 VM 回归 9 passed，临时 VM 已清理。线上 DCU/A3/XPU 来源解析与 10 分钟观测均完整；最终三环境 Prefill/Decode 覆盖分别为 1/1、4/16、1/1，核验时数据年龄约 20 秒。平台实际 engine 来源可访问新接口，Mac 非白名单及伪造 X-Forwarded-For 均为 403。仅替换 API，原挂载保持；vmagent/vmalert 身份和挂载核验一致，未更改 VM 历史、发送缓冲、处理水位、采集配置、规则或模型服务。

test4 新增 `/data2/monitoring/releases/service-cache-20261020/`，含 repo、build、build-r2、window、window-r2；各轮材料保留，未清理远端证据。脱敏验收回执和来源配置证据在本地 `evidence/cache-reports-20261020/{verified.json,live-backends.json}`。配套平台源码开发提交 `27aa9c492814a1969c696a060ef7cc7c5d87d4b7`、发布提交 `07bd6c4d97d47a465e4a3a0e9eb1cf7e0806d599`，版本和历史补录边界见 code-eval 的 `deploy/releases/README.md`。

## 2026-10-20 三入口探测与平台展示配套

源码 `c49639d6bd65d387bafd21a3aceb7e0a488c048c` 已推送，06:52:50—06:52:59（北京时间）在维护窗口替换 monitoring-api 和 monitoring-probes。原固定基底为 `sha256:6a34995a5ce1c551b2e7590b063edcf331b9b311cbfba53e8ddd1a85e3c060ed`，两组件最终镜像均为 `sha256:922eb778d62a1a05a558188a513212db551db3ae5863394773fb7afdb25ce157`。通用工具按精确提交离线构建，两个容器各 32 个源码文件与输入清单一致，启动参数、环境、资源和数据挂载保持，restart_count=0。

探测配置仅去除 PPU，保留 A3/XPU/DCU 的原入口与每分钟 0/20/40 秒调度；API 和部署工具的就绪检查同步为 3 个目标。本次预检发现通用检查仍要求 4 个目标，已在上述源码内修正并增加 3 接受/4 拒绝的回归。相关部署、修复及探测回归共 161 项通过，独立真实 VM 语义测试 1 项通过。

API 健康、worker `/health` 为 `{"status":"ok","targets":3}`，三个环境 Prefill/Decode 来源覆盖完整，核验时快照年龄约 18 秒。新 worker 启动后三个入口均产生新定时观测：XPU 为成功/HTTP 200；A3、DCU 为 http_error/HTTP 503，与平台读回的两入口业务准入关闭一致，保留失败记录而未更改准入。只消费自然定时请求，未增加手动模型请求。配套 code-eval `4002092acc831f02d03fa61a02bbc1a135d19dc1` 已发布，三入口页面经桌面/手机触控模拟验收。

线上 scrape.yml 已与提交一致，SHA-256 `ec9232ff1c3479a2bf6cb47a8ed5cc6742663b5f445b5cda950d8e0ced983fa9`，无需重写或重启 vmagent；VM、vmagent、vmalert、Perses、历史、发送缓冲和处理水位保持。Mac 非白名单访问 monitoring API 仍为 403。准备阶段第一次浅 bundle 缺少父提交，克隆未成功；换为完整 bundle 后在精确提交构建，未影响运行服务。

服务器新增 test4 `/data2/monitoring/releases/performance-display-20261020/`，含 `build/`、`api/`、`probes/`、`private/`、`repo-full/`。本地脱敏收据为 `evidence/performance-display-20261020/`；受限配置快照仅在服务器保留。

## 2026-10-19 A3 网关采集地址迁回

A3 gateway 指标目标更新为 122.52.5.131:18082，自动探针更新为 http://122.52.5.131:9090/v1/chat/completions；模型和节点采集地址保持。维护窗口内停止/启动同版本 monitoring-vmagent、monitoring-probes，保留 VM 历史、缓冲与水位。最终 up=1、指标年龄约 4.1 秒，自动探针 success/HTTP 200；相关本地回归 11 passed。新增 test4 `/data2/monitoring/releases/a3-relocate-back-20261019/` 保存原配置和验收。

完整记录见 [A3 迁回验收](../../../environment/A3/changes/20261019-aigate-relocate-back/README.md)。


当前操作见 [项目 README](../../README.md)、[部署工具](../../deploy/README.md)、[Perses 维护](../../perses/README.md) 和 [停机窗口升级](../maintenance-window-upgrade.md)。最新一轮核验 A3 采集配置与 gateway 更新后的数据新鲜度；线上配置已一致，无需重启。API、probes、生成器、看板及其他组件的既有版本记录分别保留，不以当前仓库 HEAD 代替运行身份。

## 最近保留的版本记录

| 范围 | 记录日期 | 版本身份 | 证据位置 |
| --- | --- | --- | --- |
| aigate 采集配置 | 2026-10-17 23:38 | 源码 `f0ff0c186be2d0f2af8b1771e21ec54702c81227`；线上配置已一致，未重启 vmagent | test4 `/data2/monitoring/releases/multi-target-replay-20261017/checks/` |
| 维护工具 | 2026-10-21 | 源码 `eeebf3454fe9c11d6ca6b416eae0d172ee43c974`；当前通用 API 构建与维护窗口替换工具 | test4 `/data2/monitoring/releases/foundation-router-20261021/repo/` |
| 已安装生成器 | 2026-10-14 | 源码 `1f9d280922ca0b73cc77d3f2d42505bf081e077b`；35 个模块字节核验，独立生成的 15 项在途查询排除回放 | `gateway-replay-filter-20261014/runtime/` |
| monitoring-api | 2026-10-21 | 源码 `eeebf3454fe9c11d6ca6b416eae0d172ee43c974`；镜像 `sha256:24e6e5f0c09ad40829976a696443c6cc658bb15295826283532691c51cdb8e7e`；来源目录与 latest/history 元数据 | `evidence/foundation-router-20261021/checks/completed.json` |
| monitoring-probes | 2026-10-20 | 源码和镜像同 API；A3/XPU/DCU 每 60 秒一次，偏移 0/20/40 秒 | `evidence/performance-display-20261020/verification.json` |
| vmalert | 2026-10-12 | 源码 `f8ce79b`；镜像 `sha256:9eedd2fd65f87f4737efe06b17b3cd37f0e3178375a28bf00b948778de7d34e6`；官方二进制 1.151.0，202 条规则 | `native-gauges-20261012-f8ce79b/native-build/`、`native-release/`、`checks/native-check.json`、`checks/native-audit.json` |
| Perses 看板 | 2026-10-14 | 查询源码 `1f9d280`；3 项目、21 看板、261 面板；只更新 6 张看板的 15 项查询，30 项 VM/代理对照通过 | `gateway-replay-filter-20261014/dashboards/` |
| Perses | 2026-10-09 | `0.54.0-perf.3`；镜像 `sha256:978402ac5154e3ee2cf8c66f244fc7169be74125ef01a5fe9d28c1e8a24764e7` | 原 TLS 发布记录和 [构建版本锁](../../perses/performance/release-lock-perf3.json) |

**A3 迁移后的采集配置核验（2026-10-17 23:38，北京时间）**：采集目标为 `122.209.21.24:18082`、node=`a3-1`。已提交并推送的 `f0ff0c186be2d0f2af8b1771e21ec54702c81227` 修正配置及回归断言，相关回归 16 项通过。线上 `/data2/monitoring/release/deploy/scrape.yml` 与提交逐字节一致，SHA-256 `cef61cfacce589ac6d9e904cc9d3fa4bc551c39730434500eb41f6d0de957366`，因此没有重写配置、重启 vmagent 或重新构建 API/probes。`profile_a3_scrape.yml` 为仓库片段，现场无对应运行文件，本次仅保存候选输入。

A3 `up=1`，当前地址存在 357 条 aigate 指标序列；配套四环境 gateway 更新完成后，DCU/XPU/A3 三个既有 aigate 采集目标仍均为 `up=1`，采样年龄约 0.19–2.70 秒。只核验本次相关采集，未重做全看板或性能验收。VM 历史、发送缓冲、处理水位和原挂载保持。

本次新增服务器目录 test4 `/data2/monitoring/releases/multi-target-replay-20261017/`，含 `inputs/`、`private/`、`checks/`。脱敏收据集中在 `code-eval/deploy/evidence/multi-target-replay-20261017/public/monitoring-verification.json`。网关的多密钥配置与版本身份见 [aigate 发布记录](../../../aigate/docs/DEPLOYMENT.md)，平台镜像和页面验收见 [code-eval 发布记录](../../../code-eval/deploy/releases/README.md)。

**PPU 网关探测（2026-10-17 21:53—21:58，北京时间）**：PPU 使用平台已登记的切流业务入口 `http://122.209.21.209:60010/v1/chat/completions`，稳定标签 `ppu`。保留其他三个入口配置和 0/20/40 秒调度，新目标使用每分钟第 50 秒；启用前保持真实缺测，不生成历史记录。配套 code-eval Web 源码 `6e23abf4b1d0cd7a79773de87eb04a10b59c04fd` 已发布，最终平台维护为 false。

API 和 worker 以原 API 镜像 `sha256:35fd590ac9748492ed6ace1b95d78df6d0972066d972eb80b5fff4e244626eb0` 禁网构建并替换。两个容器各 33 个运行源码文件与清单一致，环境、启动参数、HostConfig、挂载保持，restart_count=0。三个原环境快照健康且完整，worker health 为 ok/targets=4，Mac 非白名单读取仍为 403。21:53:33 启动后四入口都有新成功记录；21:55:50 的 PPU 样本首输出 0.375 秒、总耗时 1.483 秒，HTTP 200 且完整 SSE 成功，只消费自然定时请求。

探测回归 55 项、部署工具回归 89 项、真实 VM 语义测试 1 项通过；平台前端 8 项时间轴回归、构建/lint/格式及桌面 1280×720、390×844 竖屏触控模拟的合成与线上检查通过。PPU 卡片、当天时间轴和真实记录详情正常，无页面脚本错误、业务 HTTP 失败或浏览器 API 写请求；既存 favicon 404 保留。未更新 VM、vmagent、vmalert、Perses、网关或模型。

首次 worker 发布检查仍要求三个目标，超时后保留新容器及原失败证据。修复就绪校验到四个目标后，使用同一通用校验函数确认当前容器身份和健康，并保存 `container-revalidation.json`；没有回退或重复替换 worker，旧停止容器保留作证据。

服务器新增 test4 `/data2/monitoring/releases/ppu-probes-20261017/`，含 `source/`、`api-build/`、`api-release/`、`probes-release/`、`private/` 和 `checks/`。本地公开证据在 `evidence/ppu-probes-20261017/`，源 bundle SHA-256 `cf5dcd12e13bf5d9951e4dab9e9ba11f737decb16daeaf1746c2cba71c904a45`，工具修复 bundle SHA-256 `f140b95d2dff2ae3e1a7d7df27774f684594150de24411951199b88bb01c6d89`。VM 历史、缓冲、处理水位及工作区已有采集配置改动保留。

**概览指标 API（2026-10-14 16:06 上线，北京时间）**：源码 `fd114f62230d5e18d4e4b066176bb04861a78969` 已提交并推送。以原 API 镜像 `sha256:2d7e160b38f137d1d2cd8eb91cf105a65a7fce9f89c509569f585d1356dccd2a` 为固定基底禁网构建，仅替换 monitoring-api。36 个运行源码文件与构建清单完全一致；环境、启动参数、HostConfig 和挂载一致，restart_count=0，三环境健康、快照年龄约 18–21 秒。

`full` / `summary` 均返回后端错误速率及 Prefill / Decode 节点平均利用率。固定截止 15 分钟前的 5 分钟窗口中，三个环境各 61 个时间点与直接 VM 查询逐值一致；六条利用率曲线均有 61 个有效点，DCU 错误速率有 61 个有效点。A3/XPU 当前没有 `aigate_upstream_errors_total{request_scope="all"}` 原始序列，起点指标和抓取正常；错误速率按既定口径留空，不补零，也未为验收制造错误请求。

首次近期窗口比较出现一处 A3 利用率读取差异，随后固定历史窗口的 API / VM 对照通过；原因未归定为计算缺陷或具体采集问题，初次脚本、失败输出及后续对照保留在 `checks/`。本地相关回归 88 passed、独立 VM 批次 89 passed；图表顺序与真实页面验收随配套 code-eval Web `030426e27d6f41f76be3499716f0b3f9a5def339` 记录。

服务器新增 test4 `/data2/monitoring/releases/overview-metrics-20261014-fd114f6/`，包含 `source/`、`api-build/`、`api-release/`、`checks/`。本地公开证据为 `evidence/overview-metrics-20261014-fd114f6/`；源 bundle SHA-256 为 `a6e74f8df5b3ff716d2c85652bc0832e677650b2deb771417dfdd545d644b67a`。私有容器快照仅保留服务器受限目录，公开证据不包含环境凭据。未更新采集器、vmalert、VM、Perses 或探测 worker；历史、发送缓冲和处理水位保留。

**此前网关在途查询修复（2026-10-14 15:18 上线，北京时间）**：源码 `1f9d280922ca0b73cc77d3f2d42505bf081e077b` 已提交并推送。新版网关每个 backend 同时输出普通与回放 gauge，原查询将两套序列计入后端组数，导致完整性检查把全部在途数据过滤为空。API 和看板增加 `traffic!="replay"`，保留旧版无标签数据、测得的零值以及缺样/过期/重启留空规则。

以已安装 API 镜像 `sha256:2613fddadf60e1e79f53bdd6fb9987ccf4f1f15e18f215015729430940c8aa8c` 为固定基底禁网构建，只替换 monitoring-api。运行容器 35 个源码文件与输入清单完全一致；环境配置、启动参数、HostConfig、资源限制及数据挂载保持一致，restart_count=0，三环境 health=ok。生成器通过通用 runtime 工具安装；看板基于发布前快照仅替换查询，保留线上说明、布局和其他编辑，资源并发核验和完整读回通过。15 项受影响查询分别按 15/60 秒步长进行 VM/代理对照，30 项全部通过且无空结果。

本地单元/生成回归 40 passed，独立真实 VM 324 passed，覆盖三环境、无回放/空闲回放/活跃回放、缺样、过期、重启及不完整后端。线上三环境截图时段 14:04:50—15:04:40 的后端等待和写出时间均恢复 719 个有效点（API 实际步长 5 秒），最近 6 分钟也有持续新数据。Chrome 1280×720 与 390×844 竖屏触控模拟验证 A3 两张图有曲线、无网关空数据提示，手机无横向溢出、无页面运行错误；桌面仅既存 favicon 404。浏览器证据位于 `output/playwright/gateway-replay-filter-20261014/`。API 非白名单读取仍为 403，Perses 未认证资源读取仍为 401。

服务器新增 test4 `/data2/monitoring/releases/gateway-replay-filter-20261014/` 及其 `source/`、`api-build/`、`api-release/`、`dashboards/resources/`、`runtime/release/`、`checks/` 子目录。Git bundle SHA-256 为 `2d48a60546033a4620911ee6eb38d98f23a6144790fdae8f666df8fe44c715c9`；本地公开证据在 `evidence/gateway-replay-filter-20261014/`，服务器私有配置快照未打包。VM 历史、发送缓冲及处理水位保留；未更改网关、模型服务、vmagent、vmalert 或探测 worker，没有发起模型请求或回放。

**此前一分钟探测配套发布（2026-10-14 08:56—08:59，北京时间）**：以下为探测 worker 和 API 的历史发布记录；两者已由上表版本替换。已推送源码 `f69f1161a91608312f79389d408afd71c01ec6c3`，以原已安装镜像 `sha256:7733d4acf1ffa2d02dc8918ad51ecf3ca0387fec828c1bb48b1eec739b29e7e1` 为不可变基底，复用通用工具禁网构建并替换 API、worker。两个容器各 35 个源码文件与构建清单一致，启动参数、环境配置、资源限制、state 挂载及探测配置摘要保持一致，restart_count=0。API、worker 健康，三环境快照新鲜且来源完整；Mac 非白名单读取返回 403，平台代理正常读取。

新 worker 08:56:51 启动后，首轮自然定时请求全部成功：A3 08:57:00 开始，首输出 0.691 秒、总耗时 1.350 秒；XPU 08:57:20 开始，首输出 0.665 秒、总耗时 2.222 秒；DCU 08:57:40 开始，首输出 0.429 秒、总耗时 0.642 秒。均 HTTP 200、有效输出及流完整结束，仅消费定时请求，没有另加手动模型请求。API 返回 interval=60、stale=120、offset=0/20/40。

本轮复用审核通过的本地验证：探测单元 55 passed、独立真实 VM 1 passed。配套 code-eval Web 源码 `c830af2bf38f99e5ad99a9076e106178f667c049` 已发布，当天十分钟汇总、144 格、未来格、真实记录详情、只读刷新通过线上 Chrome 1280×720 与 iPhone 390×664 竖屏触控模拟验收。旧五分钟数据按新频率展示缺测；VM 历史、发送缓冲和处理水位保留，未更新 VM、vmagent、vmalert 或 Perses。

服务器新增 test4 `/data2/monitoring/releases/probes-minute-20261014-f69f116/`，包含 `source/`、`api-build/`、`api-release/`、`probes-release/`、`checks/`；本地公开证据为 `evidence/probes-minute-20261014-f69f116/`。Git bundle SHA-256 为 `88eb447a0f300f0eb92e5f868543cd539a9de0e43ed2142581ce9b3396a19c47`。原容器配置保存在服务器受限文件中，公开证据包不包含凭据。准备阶段 bundle 需要显式检出指定 commit，首次构建命令未运行即退出；修正后正常构建，说明保留于 `checks/preparation-notes.json`。上一轮探测发布正文从 Git 历史查阅。

**此前原生瞬时值发布（2026-10-12 08:20）**：以下证据属于 `native-gauges-20261012-f8ce79b`，本轮未重复执行。此前按停机窗口替换 API 和 vmalert，更新看板与生成器。采集配置逐字节一致，因此未替换 vmagent；VM、缓冲和状态挂载保留，未重算历史。三环境健康且配置来源覆盖完整，快照读回与同一时刻 VM 结果一致；最终检查的数据延迟约 20 秒。DCU Decode 缓存 summary/full 的 13 个历史点相同，其中 6 个非空，实际缺口保留。

202 条规则逐条 raw/记录对照通过，其中 184 条当时非空，18 条为空不代表真实业务覆盖。新进程写入错误及丢弃均为零，六条配置实例数记录在进程启动后写入并保持新鲜；537 条瞬时来源序列在 API 中可读，`monitoring_direct_*` 别名没有写入 VM。35 个生成器模块字节核验及独立运行通过，安装新增 `/data2/monitoring/perses/release/monitoring/` 包目录。发布后 Perses 未认证访问返回 401，API 非白名单访问返回 403，TLS 使用服务器证书校验。

1080p Chrome 检查覆盖五张受影响看板的关键图表：A3 Prefill/Decode 队列分别有 8/32 条序列，模型页 Decode 筛选与汇总页输出吞吐角色正确，DCU HiCache 有 8 个 Prefill rank，Mooncake segment 容量有已用/总量两条序列。281 次图表查询均成功，未捕获页面运行错误；见同批 `checks/browser-acceptance.json` 和本机截图。Mac 未信任服务器自签证书，因此通过验证上游证书的临时本机转发入口访问真实线上数据，未禁用 TLS 校验；该入口和测试浏览器已关闭。

Perses TLS 的原记录确认 HTTPS、HTTP/2、认证和资源保留；证书与访问方式见 [维护说明](../../perses/README.md)。本次 API 使用固定的已安装基础镜像叠加已提交源码构建，基础镜像及产物身份见 `api-build/`。

此前查询瘦身的常规性能门槛未全部通过；本次功能验收不替代该性能验收，取舍依据见 [当前查询说明](../query-slimming.md)。

## 当前说明与材料

- [指标口径与历史边界](../metrics-and-history.md)：请求范围、A3 贡献分母、采集完整性、CPU 聚合及流停顿。
- [时间范围与缓存](../time-navigation.md)、[查询加速](../perses-query-acceleration.md)（历史记录，现行为原生记录规则）。
- [代码退役索引](retired-code.md)：原路径及 SHA-256。

`deploy/` 只保留最近两个日期目录中的 Dockerfile 和 manifest，更早的从 Git 查阅（`42312a9` 为移除前的最后一版）；版本锁和校验和保留。原始执行证据在忽略的 evidence/work 目录，Git 检出不能恢复它们。新发布更新本页对应组件的版本身份、验收范围及证据位置；过程日志和审核流水不另行提交，旧版本从 Git 查阅。

## 完整历史

在仓库根目录执行：

```sh
git ls-tree -r --name-only fbf952971be7 -- docs/releases deploy reviews deployments
git show fbf952971be7:docs/releases/columns-maintenance-20261010.md
git show fbf952971be7:docs/releases/query-slimming-20261009.md
git show fbf952971be7:docs/releases/perses-tls-20261009.md
git show fbf952971be7:docs/releases/monitoring-history.md
git show fbf952971be7:docs/releases/perses-history.md
```

这些正文保存当时的发布、故障、未完成项与验收边界，不能作为当前操作指令或恢复旧版本的授权。
