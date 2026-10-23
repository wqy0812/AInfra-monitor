# 模型服务真实请求探测

启用[自动监控目录](automatic-monitoring.md)后，目标来自 code-eval：优先探测网关入口，成功结果共享给实际经过的底座固定 Proxy/Router，未覆盖或失败时独立探测底座，目录不再限制为三个目标。维护暂停、30 秒底座运行许可和显式旧入口历史映射以自动监控协议为准。下文的三入口 JSON 是未设置受管目录时的旧部署配置，配套接管后不再手工维护。

本功能由 monitoring 的独立 `monitoring-probes` 进程执行，平台 Web 只读展示。已实现由 code-eval 登记自动生成底座与网关入口两组观测；是否已启用以实际发布记录为准。

## 自动接入与身份

配置 `MONITORING_PROBE_CATALOG_FILE` 后，worker 和查询 API 读取 controller 已接收的登记目录；未单独配置时使用 `MONITORING_CATALOG_FILE`。目录格式为 `schema_version: 1`、`revision`、`foundations` 和 `gateways`。缺失、格式错误或空目录不会恢复旧目标。未配置两种目录的安装在协调迁移前沿用下文的三入口配置。

- 底座使用 `foundation:<id>` 为 `probe_id`，`probe_kind=foundation`，向固定 Router 的 Proxy 业务地址发送请求，附带 `X-Aigate-Expected-Router` 并核对 `X-Aigate-Backend`。缺失或不一致的响应身份记为 `identity_mismatch`。必须已选择支持的监控适配模板、启用底座并登记 Router 业务地址才执行；资源 exporter 的 URL 不用于生成请求。
- 入口使用 `gateway:<id>`，`probe_kind=gateway`，访问登记的业务地址。只接入属于已登记底座的 Router 入口；未绑定入口显示待登记，其他已分配但与底座无关的入口不自动恢复探测。入口响应携带的实际 Router 随结果保存；没有该响应信息时保留空值，不用当前绑定解释历史。
- 网关入口每 60 秒一次；底座按同一分钟轮次等待关联入口结果，任一入口完整 SSE 成功且实际 Router 匹配时共享成功，不额外生成。全部入口失败、缺测、身份无法确认或没有关联入口时，通过固定 Proxy 独立探测，每轮最多一次；派发前检查更新轮次的成功覆盖。切流后失去覆盖的 Router 恢复独立探测，入口和底座失败分别保存。同目标保持串行，不补发已错过轮次，沿用 64 Token 输出预算、50 秒时限和完整 SSE 判定。新底座使用稳定的奇数秒偏移，新入口使用稳定的偶数秒偏移，worker 将偏移持久化在 `STATE_DIR/probe-offsets.json`。同一目标串行，新增目标、恢复或配置变化等待下一个未来时点。
- 维护通过 controller 的显式 pause/resume 接口控制。worker 每秒读取 `MONITORING_PROBE_CONTROL_FILE`（默认 `STATE_DIR/probes-control.json`），根据 `paused_routers` 停止新底座请求。controller 只有读到 worker 在 `MONITORING_PROBE_STATUS_FILE`（默认 `STATE_DIR/probes-runtime.json`）写入的对应操作及新观察时间才确认停止派发。已发起请求按原时限收尾。
- controller 的 `heartbeat_at` 超过 30 秒未续期时，底座探测暂停；入口继续走业务入口自身的准入。暂停不会伪造一次服务失败。worker 保留最近两天的暂停区间，页面将对应计划槽位标成暂停，实际失败记录仍具有优先级。
- 迁移旧三入口历史须在对应 gateway 明确登记 `legacy_probe_environment` 和 `probe_migrated_at`。仅将迁移时间之前、没有 `probe_id` 标签的旧样本接回该入口组；同一旧来源只能映射一个网关。旧样本不会被解释为底座探测。已迁移 A3/XPU/DCU 入口沿用 0/20/40 秒偏移。

受管 `GET /api/monitoring/probes` 返回 `probe_schema=managed-v1`，`services` 中包含 `probe_id`、`probe_kind`、所属底座或入口、`dispatch_status`、`pause_reason`、`pause_intervals` 及原有记录字段。`records[].actual_router_id` 只来自真实响应标签。新记录增加 `observation_source=switch|switch_shared|direct`、`source_probe_id` 和 `scheduled_at`。共享记录保留来源请求的真实开始/结束时间和整条 switch 链路耗时；同批导入入口及底座观测。底座归属取派发时的目录快照与实际响应身份，不按后来绑定重解释历史。共享覆盖不属于暂停。新记录缺少计划时间或其他必要字段时显示 unknown，旧五字段记录继续读取。公开返回不包含目标 URL、运行凭据。平台按 `probe_id` 选择详情，来源别名相同也不会混合两组结果。

每个入口每 60 秒一次，A3/XPU/DCU 分别使用 UTC 周期内的 0/20/40 秒偏移；新增入口不调整已有偏移，避免改变历史记录的分钟槽位归属。每个入口串行；启动、恢复和错过周期都只等待下一次未来时点，不补发、不立即重试。探测请求使用网关普通业务准入，不申请测试占用、不绕过维护或暂停。请求可能进入已有网关业务统计，最多每个入口每天 1440 次；不为排除这些请求修改现有网关。

请求为 `POST /v1/chat/completions`，固定提示“请简短地回复：你好。”，`stream=true`、`max_tokens=64`、`n=1`。当前网关允许省略 model，并按真实绑定发现模型。总时限 50 秒、连接时限 10 秒、响应读取上限 1 MiB。收到非空 content/reasoning_content/reasoning，收到 stop/length 的 finish_reason、`[DONE]`，并正常完成 HTTP 流才成功；length 表示达到输出预算，也属于正常结束。仅 HTTP 200、仅心跳或仅 usage 不代表成功。收到 HTTP 错误、超时、连接失败、流内错误、格式错误、空输出或结束不完整时记录对应类别，不保存提示回显、模型输出或上游错误正文。

## 数据与查询

写入现有 VM `/api/v1/import` 的 `model_probe_*` 原始样本，旧模式使用稳定入口 environment；受管模式另保存 probe_id/probe_kind、底座或入口 ID、实际 Router 和观测来源标签，新记录增加来源计划时间样本。Router 交换不改变入口归属，也不尝试反推历史后端。每次结果使用同一个完成时间戳保存结果类别、请求开始时间、总耗时、首个输出耗时、HTTP 状态。导入设置 5 秒总时限（覆盖完整请求与响应读取），失败留下缺口，不重新生成。停止时正在执行的探测被取消且不记作服务故障。

`GET /api/monitoring/probes` 固定读取最近 24 小时原始导出，不使用 query_range 的向前填充。返回 observed_at/start/end、interval_seconds=60、stale_after_seconds=120，以及登记目录中的 services，各含 environment/name/status/latest/records 和 offset_seconds。未接管目录时返回旧三入口及 0/20/40 秒偏移。记录包含 started_at/finished_at/status/reason/duration_seconds/first_output_seconds/http_status。缺失或矛盾的字段显示 unknown；没有记录或最后结果超过 120 秒时当前状态 unknown，旧结果仍保留原时间。VM 不可达返回 503。

平台 Web 代理同路径，每 30 秒只读刷新，切走或隐藏时停止；手动刷新不触发生成。保留底座服务和网关入口两组，用自适应多列紧凑卡片显示最近 6 小时。桌面 1280×720 可排三列，手机竖屏单列。卡片保留服务名、状态、来源、最近观测时间和时间轴，详情展示来源探测 ID、计划时间、真实请求时间、耗时、HTTP 状态与实际 Router。

退役底座及当前绑定它的入口从目标与公开列表中移除；已有请求正常收尾、VM 记录保留。平台再按本地权威登记过滤，避免同步期间出现旧卡片。维护暂停的在用底座仍展示。两组卡片各自按名称字母升序、忽略大小写及数字自然序排列。

时间轴使用滚动的 `[当前时间−6小时，当前时间]`，每点 10 分钟，首尾截取未满区间；通常有 37 点，恰好十分钟边界时为 36 点。跨午夜连续展示。新记录按来源计划时间归属分钟，避免把 switch 的偏移套入底座偏移；旧记录沿用实际开始时间减原偏移归槽，保留提前一秒容差。结果读取截止时间不随本地时钟伪增。尚未到期分钟不参与汇总，当前已到期无结果的槽位先显示等待，到下一周期仍无结果才显示缺测。

颜色按失败优先：有任一次失败为红色；无失败但有缺测、等待或记录不完整为灰色；全部已到期记录成功为绿色，维护暂停黄色。共享结果与独立结果均保留，同槽位失败不能被成功覆盖。详情按来源与完成时间区分记录，随刷新更新，选中区间移出最近 6 小时后关闭。API 继续返回最近 24 小时原始记录，不改写旧历史或补造共享结果。

## 发布

沿用停机窗口与 `deploy/release.py`，不需要升级网关或修改历史数据。自动接入的 worker 与 API 必须挂载同一 accepted probe catalog、control 与 runtime 状态文件，worker 对 state 具有写权限；controller 根据实际 ack 确认维护暂停。迁移前先停止旧 probe worker，核对并登记旧三入口映射与迁移时刻，再启用受管 worker，避免同入口重复生成。

下列手工配置流程用于尚未接管目录的安装：探测目标仅包含 A3、XPU、DCU，从平台“网关与 Router 登记”核对三个业务 URL，按 `deploy/probes.example.json` 准备实际 JSON 配置，不使用 Router 或模型直连地址。配置是入口地址的部署副本，入口地址变更后同步，Router 交换无需修改。

从四入口配置升级时，先停止 probe worker，再移除实际配置中的 `ppu` 项，更新 API 和 worker 后启动。配置校验要求恰好包含三个保留入口；原有 0/20/40 秒偏移不变，不补发。PPU 历史样本继续保留在 VM 中，查询接口与平台页面不再展示 PPU。此调整不改变 PPU 服务及准入状态。

拆分后的 aigate 切流业务入口免认证，配置只保留 URL，不配置 `api_key`，也不使用切流控制密钥。探测按入口归属的 `model_probe_*` 标签与按 Router 归属的 `aigate_*` 指标含义不同：切换入口目标后，探测标签不变，生成请求计入新的目标功能服务。

worker 继续兼容需业务认证的其他入口配置 `{"url": "http://gateway/v1/chat/completions", "api_key": "入口业务密钥"}`；未设置密钥或为空时不发送认证头。有密钥时仅对该入口发送 `Authorization: Bearer ...`，不发送给 VM，也不写入探测结果。密钥限 8192 个可见 ASCII 字符，不包含空白。实际配置文件不得提交到 Git；含密钥时设置权限为 `0600`，沿用配置文件只读挂载，避免将密钥写入命令行或日志。

1. 使用 `release.py api-image` 构建包含本功能的已提交源码镜像，并通过 `release.py container monitoring-api IMAGE --evidence DIR` 更新只读 API。
2. 首次使用 `release.py probes --image IMAGE --config /absolute/probes.json --state /data2/monitoring/state --evidence DIR` 安装 worker。IMAGE 必须是不可变本地镜像 ID，配置文件、state 与独立空证据目录必须预先存在；新增服务器目录须告知用户。
3. 后续 worker 升级通过 `release.py container monitoring-probes IMAGE --evidence DIR`，保留配置只读挂载、state 和锁文件。只运行一个 worker；共用 state 的文件锁防止重复实例。配置路径保持稳定，内容调整后重启 worker。
4. 平台按自身发布入口更新 Web 镜像。worker `/health` 仅监听 `127.0.0.1:18433`，检查调度进程与 VM 可达性，不代替探测结果。安装检查不会额外调用模型。
5. 启用后等待三个入口各产生一条新记录（最长一个周期加 40 秒及 5 秒写入时间），检查时间、耗时与结果；失败据实展示并调查。若明确只需一次验收，消费这三次定时请求即可，不另加重复生成。失败保留证据、修复当前版本，不回退。

## 本地验证

使用项目 `.venv` 运行 `python -m pytest tests/test_probes.py tests/test_managed_probes.py tests/test_switch_first_probes.py -q`；通过 `scripts/test_local_vm.sh tests/test_probes_vm.py -q` 在可丢弃 VM 中验证真实导入、读取、缺测、过期和部分写入。受管回归覆盖双层身份、旧历史映射、Router 身份断言、运行许可、真实停止派发确认及暂停区间。平台 `test:browser:probes` 和 `test:browser:monitoring-integration` 覆盖桌面与 iPhone 竖屏，浏览器仅使用合成 API，线上真实模型请求在启用验收时执行。
