# 模型服务真实请求探测

本功能由 monitoring 的独立 `monitoring-probes` 进程执行，平台 Web 只读展示。代码已提供探测、VM 写入与查询、平台页面；启用以实际发布记录为准。

每个入口每 60 秒一次，A3/XPU/DCU/PPU 分别使用 UTC 周期内的 0/20/40/50 秒偏移；新增入口不调整已有偏移，避免改变历史记录的分钟槽位归属。每个入口串行；启动、恢复和错过周期都只等待下一次未来时点，不补发、不立即重试。探测请求使用网关普通业务准入，不申请测试占用、不绕过维护或暂停。请求可能进入已有网关业务统计，最多每个入口每天 1440 次；不为排除这些请求修改现有网关。

请求为 `POST /v1/chat/completions`，固定提示“请简短地回复：你好。”，`stream=true`、`max_tokens=64`、`n=1`。当前网关允许省略 model，并按真实绑定发现模型。总时限 50 秒、连接时限 10 秒、响应读取上限 1 MiB。收到非空 content/reasoning_content/reasoning，收到 stop/length 的 finish_reason、`[DONE]`，并正常完成 HTTP 流才成功；length 表示达到输出预算，也属于正常结束。仅 HTTP 200、仅心跳或仅 usage 不代表成功。收到 HTTP 错误、超时、连接失败、流内错误、格式错误、空输出或结束不完整时记录对应类别，不保存提示回显、模型输出或上游错误正文。

## 数据与查询

写入现有 VM `/api/v1/import` 的 `model_probe_*` 原始样本，标签只有稳定入口 environment；Router 交换不改变入口归属，也不尝试反推历史后端。每次结果使用同一个完成时间戳保存结果类别、请求开始时间、总耗时、首个输出耗时、HTTP 状态。导入设置 5 秒总时限（覆盖完整请求与响应读取），失败留下缺口，不重新生成。停止时正在执行的探测被取消且不记作服务故障。

`GET /api/monitoring/probes` 固定读取最近 24 小时原始导出，不使用 query_range 的向前填充。返回 observed_at/start/end、interval_seconds=60、stale_after_seconds=120，以及四个 services，各含 environment/name/status/latest/records 和 offset_seconds（与 worker 共用调度定义，分别为 0/20/40/50）。记录包含 started_at/finished_at/status/reason/duration_seconds/first_output_seconds/http_status。缺失或矛盾的字段显示 unknown；没有记录或最后结果超过 120 秒时当前状态 unknown，旧结果仍保留原时间。VM 不可达返回 503。

平台 Web 代理同路径，前端打开时每 30 秒读取、切走或隐藏时停止，手动刷新只读结果。时间轴按浏览器本地日期展示当天 00:00—24:00，每点 10 分钟，按左闭右开区间汇总，完整区间对应十次一分钟探测。北京时间每天固定 144 个时间格，未来时间保留空心位置，标注“未到时间”且不可点击。点宽、间距、高度（30px）及自动换行沿用现有样式，页面保留现有 Perses HTTPS 入口。

点的颜色按失败优先：有任一次失败为红色；无失败但有缺测、等待结果或不完整记录为灰色；已到计划时间的探测全部成功才为绿色。未来分钟不参与汇总；无结果的槽位在下次计划时间之前显示“等待结果”，到下次计划时间仍无结果则显示“缺测”。当天已到计划时间的格始终保留，不再隐藏无结果的当前格。

前端从 API 读取 interval_seconds、offset_seconds 和 stale_after_seconds，10 分钟展示粒度独立固定。按实际请求开始时间归属日期，再减去入口偏移确定分钟槽位，允许计时器提前 1 秒的容差且不跨日期移动；开始时间无效时按结束时间兜底。不完整记录仍为 unknown。跨分钟或跨 10 分钟完成的探测更新原槽位；同槽位多个记录全部保留，有失败不能被后来的成功覆盖。跨零点完成的请求归属于开始日期，不计入次日时间轴；本页只展示当天，不增加历史日期选择。

日期在本地午夜自动切换，即使新读取尚未完成或读取失败也不把昨天的格子显示为今天；旧日期的详情随之关闭。等待与缺测状态以最近一次读取的 end 为准，随每 30 秒读取或手动刷新更新。顶部最近结果独立使用 API 的 latest 和 120 秒新鲜度，跨零点仍可显示昨天开始的最新请求结果。点击时间点后按时间排序列出所有实际探测的开始时间（精确到秒）与成功、失败或记录不完整状态；缺测和等待行显示计划时间。点击实际记录后自动滚动到详情，查看开始/结束时间、首个输出耗时、总耗时、HTTP 状态和原因。弹窗及选中的记录详情随读取更新。

切换前当天的五分钟原始记录也按新的采样周期判断完整性，不补造历史。旧时段通常因分钟记录不足显示灰色，有失败则仍显示红色；当天实际时间与结果保留，次日自然切换为新日期。不增加频率切换边界或历史迁移；API 仍返回最近 24 小时原始记录，页面消费其中属于今天的记录，VM 历史保持不变。平台应与 monitoring API/worker 配套升级，先更新 monitoring；本地修改和验证不代表已经部署。

## 发布

沿用停机窗口与 `deploy/release.py`，不需要升级网关或修改历史数据。首次安装前，从平台“网关与 Router 登记”核对四个业务 URL，按 `deploy/probes.example.json` 准备实际 JSON 配置；不要复制 Router 地址。配置是入口地址的部署副本，入口地址变更后同步，Router 交换无需修改。PPU 使用稳定入口标识 `ppu`；部署时核对其网关业务入口，不使用模型直连或 proxy 地址。已有三入口部署升级时，先停止 worker，再保留原配置项并添加 `ppu`，随后替换 API 和 worker；新版本要求四个入口齐全。PPU 启用前没有探测样本，页面如实显示缺测，不补写历史。

拆分后的 aigate 切流业务入口免认证，配置只保留 URL，不配置 `api_key`，也不使用切流控制密钥。探测按入口归属的 `model_probe_*` 标签与按 Router 归属的 `aigate_*` 指标含义不同：切换入口目标后，探测标签不变，生成请求计入新的目标功能服务。

worker 继续兼容需业务认证的其他入口配置 `{"url": "http://gateway/v1/chat/completions", "api_key": "入口业务密钥"}`；未设置密钥或为空时不发送认证头。有密钥时仅对该入口发送 `Authorization: Bearer ...`，不发送给 VM，也不写入探测结果。密钥限 8192 个可见 ASCII 字符，不包含空白。实际配置文件不得提交到 Git；含密钥时设置权限为 `0600`，沿用配置文件只读挂载，避免将密钥写入命令行或日志。

1. 使用 `release.py api-image` 构建包含本功能的已提交源码镜像，并通过 `release.py container monitoring-api IMAGE --evidence DIR` 更新只读 API。
2. 首次使用 `release.py probes --image IMAGE --config /absolute/probes.json --state /data2/monitoring/state --evidence DIR` 安装 worker。IMAGE 必须是不可变本地镜像 ID，配置文件、state 与独立空证据目录必须预先存在；新增服务器目录须告知用户。
3. 后续 worker 升级通过 `release.py container monitoring-probes IMAGE --evidence DIR`，保留配置只读挂载、state 和锁文件。只运行一个 worker；共用 state 的文件锁防止重复实例。配置路径保持稳定，内容调整后重启 worker。
4. 平台按自身发布入口更新 Web 镜像。worker `/health` 仅监听 `127.0.0.1:18433`，检查调度进程与 VM 可达性，不代替探测结果。安装检查不会额外调用模型。
5. 启用后等待四个入口各产生一条新记录（最长一个周期加 50 秒及 5 秒写入时间），检查时间、耗时与结果；失败据实展示并调查。若明确只需一次验收，消费这四次定时请求即可，不另加重复生成。失败保留证据、修复当前版本，不回退。

## 本地验证

使用项目 `.venv` 运行 `python -m pytest tests/test_probes.py -q`；通过 `scripts/test_local_vm.sh tests/test_probes_vm.py -q` 在可丢弃 VM 中验证真实导入、读取、缺测、过期和部分写入。平台使用对应后端代理测试及浏览器夹具，浏览器仅拦截合成数据，线上真实模型请求在启用验收时执行。
