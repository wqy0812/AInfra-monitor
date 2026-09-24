# 时间范围、历史缓存与自动刷新

此变更已于 2026-09-25 发布至 test4：monitoring-api 历史缓存和 Perses `0.54.0-perf.3` 均已切换并通过线上核验。评测平台前端不在此次修改范围内。版本、回退及证据位置见 [发布记录](../deploy/time-navigation-20260925/README.md)。

## 行为

- Perses 在当前标签页的 sessionStorage 中保存最近使用的时间范围，跨 dashboard 和项目继承；URL 显式范围优先，相对范围继续滚动，绝对范围保留毫秒。继承后替换 URL 中的时间参数，不增加浏览历史。节点、角色、刷新周期不随时间记忆继承；存储被禁用时正常使用 URL 和看板默认范围。
- Perses 仅在可见、相对范围、刷新周期非零时定时查询；隐藏时停止定时器，恢复时补查一次。固定窗口保留手动刷新并显示暂停提示，切回相对窗口恢复原周期。图表和变量查询不再因窗口焦点或网络重连额外刷新。
- monitoring-api 每个环境最多缓存 8 个历史结果，LRU 淘汰、单调时钟计时、TTL 5 秒。相同起止时间、步长、hours、view 共享在途查询。每个共享任务和 HTTP 调用均有 8 秒期限；调用者取消不传播到其他等待者，错误不缓存，退出时回收共享任务。
- 该缓存不在 Perses 直连 VM 的查询路径上。指标口径、断档、API 参数及响应结构未变。

## 验证

Python 回归覆盖历史摘要、网关历史、缓存共享、取消、过期、LRU、失败重试与关闭回收。React Query 回归覆盖相对/固定刷新、隐藏/恢复、时间继承、URL 优先、毫秒精度、前进后退和禁用存储。

`perses/performance/tests/browser-navigation.cjs` 使用 1920×1080 本地 Chrome/Playwright 验证完整候选页面，拦截 datasource 代理返回合成空数据，不访问生产。后台状态通过 document.hidden 与 visibilitychange 事件注入，报告注明此方法，不等同于操作系统真实切换标签页。`PLAYWRIGHT_MODULE` 指向安装的 Playwright 模块，`CANDIDATE_URL` 仅允许回环地址，`BROWSER_EVIDENCE` 指定报告及截图前缀。

构建先准备 vendor；Go 测试和编译均使用 `-mod=vendor` 与 `GOPROXY=off GOSUMDB=off GOTOOLCHAIN=local`。新增补丁保留上游 source map SHA256 校验，候选使用独立版本和归档，不覆盖原发布锁。生产发布只能使用 SSH MCP，并单独验收真实数据与候选镜像。

## 本地验收结果（2026-09-24）

- 70 项 Python 回归、19 项 React Query/路由测试通过；10 个补丁文件的 TypeScript 检查通过。
- Go 1.26.5 在离线 vendor 模式下通过 UI 测试并完成 Linux amd64 编译，前端生产构建通过（保留上游已有的包大小警告）。
- 最终 perf.3 镜像通过 1920×1080 浏览器验收：固定窗口首次 11 个查询，随后两个刷新周期无新增；手动刷新只增加 11 个；隐藏期间无周期查询，恢复可见只增加 11 个。跨项目跳转保留绝对起止时间，浏览器无运行时错误。
- 合成空数据验证刷新与导航行为，不作为真实指标正确性或线上性能结论。
- 候选归档：`work/time-navigation-20260924/perses-0.54.0-perf.3.tar.gz`；版本锁：`perses/performance/release-lock-perf3.json`；浏览器报告和截图：`work/time-navigation-20260924/browser.json`、`browser.png`。构建产物位于忽略目录，版本锁随源码保存。
- 本地候选容器已停止，生产服务未修改，未创建服务器目录。

## 发布前修复与历史阻塞（2026-09-25）

- perf.3 发布锁明确以线上 perf.2 的镜像摘要和版本为升级／回退基线；新构建也从前一发布锁生成这两个字段。除历史 perf.1 外，缺少基线的发布锁在任何容器操作前被拒绝。
- 发布器使用服务器本地凭据分别登录正式服务与候选，认证过期只重试一次；资源快照动态枚举全部项目、看板和数据源，覆盖 XPU 与自建项目，发布前后核对完整快照。发布证据以私有权限保存。
- 修复后 320 项 Python 测试、19 项前端测试通过；218 项独立 VM 测试未启用。本次复审的 1080p 本地候选浏览器验收已通过，合成数据不代表线上验收。
- test1 的评测与两个 worker 在发布准备检查时空闲。test4 已创建 `/data2/monitoring/perses/evidence/time-navigation-20260925`（含 `candidate-data` 副本）及 `/data2/monitoring/releases/time-navigation-20260925`。
- SSH MCP 上传曾返回 `Broken pipe` / `Connection closed`，每次失败后均停止远程操作；用户要求重试后恢复上传。最终校验全部 28 个后续分段、旧断点和完整归档 SHA256 后才载入镜像。用户随后明确选择本机浏览器验收，因此没有上传或执行远端浏览器镜像。

## 上线核验（2026-09-25）

- 完成发布器相对路径修复，Docker bind mount 统一使用绝对路径。最终 Python 回归 325 项通过、218 项独立 VM 测试未启用；前端 19 项测试已通过。
- 再次运行本机 1920×1080 浏览器验收通过，报告与部署归档的配置摘要绑定；本机 Docker manifest ID 与服务器 config digest 的差异通过 RootFS 和 Config 对照确认。合成数据、DOM 可见性事件的验证范围仍如上所述。
- monitoring-api 一次性候选读取真实 VM，三个环境均返回 361 点；各环境 20 个并发等待者共享在途查询。正式切换后再次验证完整/摘要一致、重复结果一致和处理进度新鲜。
- Perses 正式健康接口返回 `0.54.0-perf.3`，三个项目各 10 张看板和 1 个数据源与切换前相同；各项目代理 `up` 查询成功，各返回 31 条序列。VM/vmagent 未重启，API 未被 Perses 切换重启。
- A3 后端 prefill/decode 源错误在发布前已存在，发布后仍保留；API 总体健康且三个环境处理时间均在 30 秒内，原先正常的数据源继续正常。
- 本机及远端候选容器已停止，两个原生产容器保留供回退。依用户“浏览器验收本机就可以”采用本机验收；远端浏览器和 30 分钟持续观察未运行，发布报告明确记录。
- 完成线上验证后下载证据副本时 SSH MCP 再次断开，已停止远程操作。只有 `image-publication.json`、`candidate-api-validation.json` 下载到本地证据目录；完整证据仍在服务器，部署成功结论来自切换及核验命令的成功输出。
