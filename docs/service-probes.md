# 模型服务真实请求探测

本功能由 monitoring 的独立 `monitoring-probes` 进程执行，平台 Web 只读展示。代码已提供探测、VM 写入与查询、平台页面；启用以实际发布记录为准。

每个入口每 300 秒一次，A3/XPU/DCU 分别使用 UTC 周期内的 0/100/200 秒偏移。每个入口串行；启动、恢复和错过周期都只等待下一次未来时点，不补发、不立即重试。探测请求使用网关普通业务准入，不申请测试占用、不绕过维护或暂停。请求可能进入已有网关业务统计，最多每个入口每天 288 次；不为排除这些请求修改现有网关。

请求为 `POST /v1/chat/completions`，固定提示“请简短地回复：你好。”，`stream=true`、`max_tokens=64`、`n=1`。当前网关允许省略 model，并按真实绑定发现模型。总时限 60 秒、连接时限 10 秒、响应读取上限 1 MiB。收到非空 content/reasoning_content/reasoning，收到 stop/length 的 finish_reason、`[DONE]`，并正常完成 HTTP 流才成功；length 表示达到输出预算，也属于正常结束。仅 HTTP 200、仅心跳或仅 usage 不代表成功。收到 HTTP 错误、超时、连接失败、流内错误、格式错误、空输出或结束不完整时记录对应类别，不保存提示回显、模型输出或上游错误正文。

## 数据与查询

写入现有 VM `/api/v1/import` 的 `model_probe_*` 原始样本，标签只有稳定入口 environment；Router 交换不改变入口归属，也不尝试反推历史后端。每次结果使用同一个完成时间戳保存结果类别、请求开始时间、总耗时、首个输出耗时、HTTP 状态。导入失败留下缺口，不重新生成。停止时正在执行的探测被取消且不记作服务故障。

`GET /api/monitoring/probes` 固定读取最近 24 小时原始导出，不使用 query_range 的向前填充。返回 observed_at/start/end、interval_seconds=300、stale_after_seconds=600，以及三个 services，各含 environment/name/status/latest/records 和 offset_seconds（与 worker 共用调度定义，分别为 0/100/200）。记录包含 started_at/finished_at/status/reason/duration_seconds/first_output_seconds/http_status。缺失或矛盾的字段显示 unknown；没有记录或最后结果超过 600 秒时当前状态 unknown，旧结果仍保留原时间。VM 不可达返回 503。

平台 Web 代理同路径，前端打开时每 30 秒读取、切走或隐藏时停止。历史每点 5 分钟，对应一次计划探测，最近 24 小时保留首尾部分区间，根据当前时间格是否有记录显示 288 或 289 个时间格；每次结果独立显示，成功为绿色、失败为红色、缺测为灰色。点击时间点后选择该次探测查看详情。点宽和横纵间隔均为原 10 分钟展示的一半，高度保持 30px，随容器宽度自动换行。探测频率仍为每 5 分钟一次，页面保留现有 Perses HTTPS 入口。

槽位是否到期按入口的 offset_seconds 判断；各入口当前 5 分钟格在尚未到探测时间或已到时间但没有结果时隐藏，不留占位、不补更早的格子。读取到记录后显示该格，包括成功、失败及不完整记录；该时段结束后仍无记录则显示为灰色历史缺测格，迟到结果仍更新原格。时间范围以最近一次读取的 end 为准，随每 30 秒读取或手动刷新更新。新增字段向后兼容，平台应在 monitoring API 更新后升级，以消费实际调度偏移。

## 发布

沿用停机窗口与 `deploy/release.py`，不需要升级网关或修改历史数据。首次安装前，从平台“网关与 Router 登记”核对三个业务 URL，按 `deploy/probes.example.json` 准备实际 JSON 配置；不要复制 Router 地址。配置是入口地址的部署副本，入口地址变更后同步，Router 交换无需修改。

每个入口可以保持 URL 字符串，也可以使用 `{"url": "http://gateway/v1/chat/completions", "api_key": "入口业务密钥"}`。未设置密钥或为空时不发送认证头；有密钥时仅对该入口发送 `Authorization: Bearer ...`，不发送给 VM，也不写入探测结果。密钥限 8192 个可见 ASCII 字符，不包含空白。业务凭据仍由平台管理，这里是运维同步的部署副本，入口密钥轮换后同步配置并重启 worker。实际配置文件不得提交到 Git；含密钥时设置权限为 `0600`，沿用配置文件只读挂载，避免将密钥写入命令行或日志。

1. 使用 `release.py api-image` 构建包含本功能的已提交源码镜像，并通过 `release.py container monitoring-api IMAGE --evidence DIR` 更新只读 API。
2. 首次使用 `release.py probes --image IMAGE --config /absolute/probes.json --state /data2/monitoring/state --evidence DIR` 安装 worker。IMAGE 必须是不可变本地镜像 ID，配置文件、state 与独立空证据目录必须预先存在；新增服务器目录须告知用户。
3. 后续 worker 升级通过 `release.py container monitoring-probes IMAGE --evidence DIR`，保留配置只读挂载、state 和锁文件。只运行一个 worker；共用 state 的文件锁防止重复实例。配置路径保持稳定，内容调整后重启 worker。
4. 平台按自身发布入口更新 Web 镜像。worker `/health` 仅监听 `127.0.0.1:18433`，检查调度进程与 VM 可达性，不代替探测结果。安装检查不会额外调用模型。
5. 启用后等待三个入口各产生一条新记录（最长一个周期加 60 秒），检查时间、耗时与结果；失败据实展示并调查。若明确只需一次验收，消费这三次定时请求即可，不另加重复生成。失败保留证据、修复当前版本，不回退。

## 本地验证

使用项目 `.venv` 运行 `python -m pytest tests/test_probes.py -q`；通过 `scripts/test_local_vm.sh tests/test_probes_vm.py -q` 在可丢弃 VM 中验证真实导入、读取、缺测、过期和部分写入。平台使用对应后端代理测试及浏览器夹具，浏览器仅拦截合成数据，线上真实模型请求在启用验收时执行。
