# A3 SSD 缓存监控（2026-10-05）

已更新 A3 Mooncake 采集、监控 API、Perses 缓存看板和 code-eval Web。SSD 配额 1 TiB，验收时有效对象约 140.19 GiB（13.69%）；内存配额 128 GiB。容量表示 Store 登记的有效对象与配额，不等于物理缓存文件占用。

## 数据与展示

- A3 API 独立读取 `mooncake-a3` 的内存/SSD 容量与采集状态，复用 `mooncake.capacity`、`ssd_capacity`，不影响引擎实例完整性判断。有效零值保留，缺采、过期、重复来源和非法容量留空；内存与 SSD 分别判断有效性。
- code-eval A3/DCU 共用两张容量图、单位、触控详情、观测时间与断线处理。使用率在详情显示。DCU 原有 Store 命中率保留；A3 未暴露分层计数，明确说明观测缺口，不用外部 KV 或 Master RPC 成功率替代。
- Perses 由 `a3_mooncake.py` 统一维护 37 张原生图，覆盖当前端点的 88 个指标族、90 个指标名。包含容量、segment、对象大小、所有单次/批量 RPC、失败比例、驱逐、写入清理和 HA。直方图 bucket/count/sum 合并为一个族。
- 原有引擎图保留，Master 图不受角色筛选影响；引擎历史采集范围说明不再错误附加到 Master 图。全项目生成和文档生成可重复执行。
- 新采集与派生历史从上线后积累，未重置水位、补写历史或修改 A3 推理配置。实际 SSD 读写量和分层命中率仍未暴露。

## 验证

相关 Python/生成器回归 74 项通过；该轮 18 项条件性跳过不计入通过数。另在本机独立 tmpfs VictoriaMetrics 执行容量历史、断线与全部新查询语法/数值场景，共 27 项通过，临时容器和网络已清理。一次合并测试因合成数据时间段重叠失败，隔离时间段后通过；旧的固定图表数量断言已随新目录更新。

code-eval 合成浏览器回归和真实页面均通过 Chrome 1280×720、iPhone 390×844 竖屏触控，验证 A3/DCU 切换、容量详情、无横向溢出及无页面异常。真实页面无新增模型请求。Perses 1920×1080 检查分组与容量/诊断图。

在线 API 容量与同一观测时间的 VM 原始指标逐项相等，历史 used/total/ratio 一致。全部 90 个指标名已进入 VM。130 条当前原生查询与 Perses 代理逐项一致；其中 34 条因无调用分母或无新增对象而留空，非查询失败。资源发布的 15/60 秒历史比较共 252 次无错误；该审计固定向前避开最近三分钟，其发布初期空结果包含接入前历史，不作为当前有数据的证明。

## 发布与证据

- monitoring-api：`sha256:1f1de688381b4676fd7827c0ead361ab6eccf8f19dec8f808070ccd7f50e1fa0`，基于现场旧镜像仅替换 A3/API 三个文件。
- code-eval Web：`sha256:0cff33908a61c910c81b9d34abb3f21b63ca7792e5379f313c8d1cf46141b06d`，基于现场 Web 镜像仅替换前端构建资源；39 个载荷文件、37 个 HTTP 静态资源及访问控制核验通过。
- 按允许停机窗口更新 vmagent/API/Web；Perses 仅修改 `a3-monitoring/a3-cache` 一个资源。code-eval 维护状态恢复为 false。保留数据目录、水位、缓冲和并行后端工作，未回退版本。
- 本地证据：`evidence/a3-ssd-monitoring-20261005/`，包括 `published/`、单元/真实 VM 日志、当前查询对齐和浏览器截图。

服务器新增目录：test4 `/data2/monitoring/releases/a3-ssd-monitoring-20261005/`（含 tools、resources、api-payload、deploy、monitoring、evidence）；test1 `/data2/code-eval/releases/a3-ssd-monitoring-20261005/`（含 baseline/web、payload/web 和发布工具生成的快照）。测试未在 A3 上新增目录。
