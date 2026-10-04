# A3 缓存展示与历史断线标记（2026-10-03）

已发布至 [A3 监控 → 缓存与存储](http://122.247.53.162:18431/projects/a3-monitoring/dashboards/a3-cache)。复用本地、外部 KV 前缀缓存两张面板，移到首行并列，纵轴统一 0–100%，补充各自分母、Token 加权、Prefill/Decode 角色和空值语义。Mooncake Master 分组及其他图表保留，PromQL 计算表达式和数据源未变。

`perses/a3_cache.py` 纳入项目生成入口，更新可重复生成；看板发布以在线资源快照合并本次展示改动，使用当前 `project_release.py prepare/apply` 执行并发检查、资源读回和受影响查询审计。发布日志仅更新 `a3-monitoring/a3-cache` 一个资源。

历史接口已有 `external_ratio`，但原先只为本地缓存生成断线标记。现为 A3 两个角色的 `gap_before` 增加 `external_cache`，包括最后一个实时补点边界；本地/外部缓存独立断线。API 原有字段和缓存计算不变，不补写历史、不改采集配置。

API 镜像：`sha256:e5ee00ee67dbab3d94ad86b269d37793204734ba59044f30eea3dc830c870a1d`，基于现场 `sha256:214948695dc8c888698e286cbb7df0f89a51c48c0c3703ef9117f38a9f9938f6` 仅覆盖 `monitoring/api.py`，保留已上线的实时回放优化。按停机窗口使用当前 `deploy/replace.py` 更新，三环境最新数据及健康验收通过，保留状态目录、水位和访问控制。

验证结果：

- 本地 A3/历史回归 33 项通过；真实 VM 的 6 项本地用例因 Docker 未就绪跳过，随后在 test4 的独立临时 VM 中执行同一文件及 summary 回归，12 项全部通过，覆盖 15/60 秒聚合窗口的有效零值、无效样本和缺采。
- Perses 相关结构/生成及发布范围回归 10 项通过。在线受影响面板共 4 次 VM/代理对比全部通过、无空结果。
- code-eval 与 Perses 同时段、10 秒步长的 Prefill 本地/外部命中率分别核对 232 点一致。
- Chrome 1920×1080 检查两图首行并列、0–100% 轴和 Prefill/Decode 筛选；code-eval 桌面及 iPhone 竖屏通过。

真实 VM 测试仅向 `127.0.0.1:18543` 临时容器导入合成数据，存储为 tmpfs，测试进程未挂载生产数据目录；临时 VM 和测试进程容器已清理。最初两次测试启动缺少本地打包的 pytest 辅助模块/元数据，补齐测试工具后通过，失败日志保留。

服务器新增目录：test4 `/data2/monitoring/releases/a3-cache-display-20261003/`，其下包括 tools、resources、api-payload、deploy、monitoring、test-tools 和 evidence；均属于本次发布及验证。本地原始材料在 `evidence/a3-cache-display-20261003/`，线上资源快照、发布日志、API 替换日志与数据对齐报告在其 `published/` 下。
