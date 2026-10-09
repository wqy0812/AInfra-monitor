# 面板查询瘦身

本功能按批次生成候选，审计通过后才更新发布清单。`perses/projects`、线上面板和加速 catalog 继续代表已发布资源，不能用本地候选覆盖尚未准入的资源。

## 批次与口径

| 批次 | 范围 | 每面板选择器出现次数 |
| --- | --- | --- |
| `generation-results` | DCU、A3、XPU 的 `gateway-requests/generation-0` | 144 → 72，查询 4 → 2 |
| `gateway-latency` | 三项目 `extra-first`、`extra-duration` | 101 → 41 |
| `gateway-tokens` | 三项目 `extra-input`、`extra-output` | 81 → 41 |
| `a3-histograms` | A3 已加速的 8 个直方图面板 | 7 个 113 → 33，1 个 105 → 33 |

generation 的总量表达式保持不变。三个结果类别合并后，求和和异常剔除均按 `environment,result` 分组，来源守卫仍按 `job,instance,environment` 匹配。展示阶段恢复中文名称和原顺序。

直方图只重写原来连续的相邻桶比较。上下界使用精确白名单，先算 rate 再映射标签，比较保留所有来源标签及映射后的 `le`。其他守卫保持原文。异常 count 的中间数值可以变化，最终分位值和留空位置必须一致。

当前清单增加可选 `rewrites`：每项为 `[project,dashboard,panel,kind]`，`kind` 为 `generation-results` 或 `histogram-monotonic`；省略视为空。未知目标、重复项、未知模板、结果类别渲染覆盖均拒绝处理。再生成仅应用已准入项。

## 审计与发布

远程操作先检查 SSH MCP 目标目录。每批使用独立的新证据目录，新增服务器目录须告知用户；四批串行，后续批次要求前面的目标已记入正式清单。

```sh
python3 deploy/release.py queries audit --batch generation-results --evidence BATCH_DIR
python3 deploy/release.py queries resume --batch generation-results --evidence BATCH_DIR
python3 deploy/release.py queries apply --batch generation-results --evidence BATCH_DIR
```

`resume` 只复用相同资源、运行版本、工具指纹及固定窗口下的完整测量组；失败组不丢弃、不选择性重测。工具变化或并发编辑需要重新核查并建立新批次。出现写入日志后，不允许盲目重放 apply；应根据日志逐项读回后修复当前状态。

每个目标面板及受影响整页执行 `1h/5s`、`24h/60s`，分别允许/禁用缓存的 41 对交替 A/B。目标面板所有查询完成时间的中位数须严格下降，页面和独立并行非目标探针 P95 不得恶化超过 5%。页面测试执行冻结看板的全部查询，报告为代理数据完成时间，不宣称浏览器首屏绘制时间。

语义检查覆盖 `1h/5s`、`1h/15s`、`1h/60s`、`24h/60s`，比较完整序列标签、时间戳和留空，浮点误差 `rel_tol=1e-9, abs_tol=1e-10`。apply 重新检查完整矩阵、原始性能样本、健康记录、候选指纹、并发编辑及浏览器/合成证明。

任何未达标项阻止本批发布。失败保留证据并修复当前候选，不回退线上版本、不自动关闭加速组。

2026-10-09 用户在上述性能结果已披露后明确要求“都上线验收吧”，本次按单独授权执行并记录性能例外；没有修改常规准入工具、伪造通过结果或放宽正确性与物化覆盖要求。当前执行状态见[本次上线记录](releases/query-slimming-20261009.md)。

用户随后要求按收益取舍并允许回退不值得的改动。本次保留三批共 15 个网关面板改写；A3 的全部回源对照不能证明相对既有物化路径的收益，因此撤回候选 catalog，保留原 revision。A3 候选代码和流程不等于已发布资源；今后若继续，应在相等历史覆盖下比较原、新物化方案，并计入重新物化成本。

### TLS / HTTP2 性能诊断

`query_release.py` 的默认客户端是 urllib；HTTPS 地址和可信证书并不使它自动使用 HTTP/2。需要实际 HTTP/2 对照时，服务器已有支持 nghttp2 的 pycurl 环境可执行：

```sh
python3 deploy/perses_acceleration/query_h2_benchmark.py --evidence FRESH_H2_DIR
```

此入口只测第一批三个 `generation-0` 面板及其整页，保留上述 41 对、24 组窗口/缓存矩阵和统计门槛。目标与探针各用一个独立、预热后持续复用的 TLS 连接，目标最多三个在途请求；每个响应严格检查 HTTP/2、证书校验、状态码及完整查询结果，记录端口与新建连接数，不静默重试。证据为独立 `h2-*` 文件，只用于诊断，不能代替发布准入。该对照衡量当前 TLS/HTTP2 下原式与瘦身候选的差异；没有同期 HTTP/1.1 对照时，不用它计算协议升级收益，也不把代理查询完成时间称作浏览器绘制时间。

## 本地验证

使用项目 `.venv`，执行前检查 Python 版本。真实 VM 必须为专用的本地测试实例；禁止向生产库写入合成数据。

```sh
QUERY_SLIMMING_TEST_VM_URL=http://127.0.0.1:18563 .venv/bin/python -m pytest \
  tests/test_query_slimming.py tests/test_query_slimming_vm.py \
  tests/test_query_rewrite_release.py -q

.venv/bin/python deploy/perses_acceleration/query_synthetic.py \
  --snapshot BATCH_DIR/query-before.json --evidence LOCAL_EVIDENCE \
  --vm http://127.0.0.1:18563 --batch generation-results
```

七步长异常测试覆盖正常、零值、缺样、重启、重置、桶异常、多实例及 backend/model 组隔离。`query_synthetic.py` 另外用完整面板原表达式验证正常、零值和结果类别缺失，并生成浏览器所需数据及候选指纹。每次准备一套干净的专用 VM，避免同名测试序列跨批次污染。

浏览器验收使用线上同版本 Perses 二进制和插件，两套本地实例分别导入冻结的 before/candidate；仅把数据源 URL 指向测试 VM。打开 Playwright CLI 会话后运行：

```sh
.venv/bin/python deploy/perses_acceleration/query_browser.py \
  --evidence LOCAL_EVIDENCE --output output/playwright/query-slimming \
  --cli /path/to/playwright_cli.sh --session query-slimming
```

默认端口为 before `18564`、candidate `18565`。工具使用 1920×1080，检查目标面板资源指纹、图例顺序、颜色、空白、水平溢出、脚本错误及查询响应，输出 `query-browser.json`。浏览器和合成证明都必须对应当前批次候选，不能将 `--batch all` 的证明冒充单批次证明。

## A3 revision 更新

已有加速组不能再走首次切换流程。先冻结旧 catalog 和当前资源，再生成只改变 8 个 A3 表达式及 revision 的候选：

```sh
python3 perses/acceleration_catalog.py before.json new-catalog.json \
  --previous-catalog old-catalog.json
```

该模式保留现有七步长、组成员及其他 10 个面板的全部条目，拒绝同时指定 `--steps`。随后按[停机窗口流程](maintenance-window-upgrade.md)更新携带新 catalog 的 monitoring-api，保留现有 state 挂载。旧面板表达式暂时回源；不提前把新表达式发布到面板。

新 API 稳定后，在同一新批次目录执行原式审计及加速 revision 准备：

```sh
python3 deploy/release.py queries audit --batch a3-histograms --evidence A3_BATCH
python3 deploy/release.py acceleration prepare --group a3 --evidence A3_BATCH \
  --catalog new-catalog.json --replace-revision --previous-catalog old-catalog.json
```

按现有管理入口只对新 revision 补算有界历史。等待七步长的 12 小时完整覆盖、完成标记和实时水位，默认等待上限 8 小时，每 60 秒检查；超时或连续三次查询失败停止等待并保留现场，不清理水位或旧数据。

然后执行现有 `acceleration audit/impact/apply/observe --group a3 --catalog new-catalog.json --evidence A3_BATCH`，保留完整正确性、性能及实际浏览器验收。每个后续命令都必须指定同一 catalog。apply 同时验证旧、新 catalog 指纹以及本批的原式审计，保持原有专用数据源和组启用状态；只有通过后才更新面板及正式 rewrites 清单。
