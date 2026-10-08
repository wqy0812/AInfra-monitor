# A3 缓存命中贡献口径（2026-10-08）

已发布监控 API 和 code-eval Web。只改变 code-eval 的 A3 缓存命中率展示，Perses 保持现状；未修改或重启 A3 推理服务。工作区中其他并行修改未随本次发布。

## 计算与接口

读取四个 Prefill 实例的 `vllm:prompt_tokens_by_source_total`，分别取 `local_cache_hit`（G）、`external_kv_transfer`（M）、`local_compute`（C）的连续约 60 秒增量。跨实例先合计 Token，T＝G＋M＋C。

| 曲线 | API 字段（`nodes.prefill.cache_60s.effective` 内） | 计算 |
| --- | --- | --- |
| 整体 | `ratio` | (G＋M) / T |
| A3 显存 | `device` | G / T |
| Mooncake | `storage` | M / T |

例如 T＝1,000、G＝900、M＝50 时显示 95%、90%、5%。显存＋Mooncake＝整体≤100%。对象同时包含 `input_tokens`、`hit_tokens`、`computed_tokens`、`device_hit_tokens`、`storage_hit_tokens`、`window_seconds`、`semantics`、`since`、`reason`。原有 `cache_60s.ratio/external_ratio` 仍按各自查询 Token 计算，不换算、不改义。

每个采样点校验三种来源完整且唯一、实例/引擎/模型身份一致、三类之和等于 `vllm:prompt_tokens_total`、两类命中之和等于 `vllm:prompt_tokens_cached_total`。窗口必须连续，无计数回退或来源变化；四实例必须齐全。异常窗口全部留空并给出原因。T＝0 时保留零 Token 数，比例留空；T＞0 且命中为零时保留有效 0%。

此指标在原生 Prefill 首次输出时入账，与 DCU 的入账时机存在差别。Mooncake 表示外部 KV 的 Token 贡献，不拆内存/SSD，不增加 CPU 曲线。

## 历史边界

启用时间为北京时间 **2026-10-08 10:42:55**，Unix 时间 `1791427375`。首次有效比例需积累上线后约 60 秒观测。

`latest` 和 `history` 返回顶层 `cache_effective_since`。独立状态文件 `/state/a3-prefill-effective-start.json` 持久化启用时间，服务重启后沿用。新指标使用独立 schema `vllm-prefill-source-v1`；启用前不写新序列，历史响应强制留空。没有回填、清空历史或重置处理水位。图表使用独立 `effective_cache` 断线标记，详情统一显示总输入和各层命中 Token。

## 验证与发布证据

- 相关 Python 回归：64 项通过；需要独立 VM 的 12 项在普通运行中跳过。
- 独立真实 VictoriaMetrics 回归：36 项通过，包含新口径及已有历史边界测试。容器与临时网络已清理。
- 前端构建通过；源码构建与实际发布资源均通过 Chrome 1280×720、iPhone 390×844 合成测试，覆盖 95/90/5、零值、空值、旧数据不替代、断线、图例、键盘/触控及 DCU/XPU 不变。
- 真实页面两个尺寸均验收通过；图例和 Token 详情可用，手机无横向溢出。真实数据由线上流量提供，未调用真实模型进行测试。
- 线上原始数据重放与 API 的全部新字段精确相同；原有 ratio/external_ratio 和计数也精确相同。验收窗口：T＝2,599,426，G＝2,260,992，M＝208,896，C＝129,538，整体约 95.0167%。
- 历史边界检查：启用前 12 个点全部留空，启用后 65 个有效点满足比例恒等式；VM 新序列最早时间恰为 `1791427375`。所有原有水位保留并正常前进。

先替换 monitoring-api，再替换 Web，按停机窗口发布；健康与资源摘要核验后关闭维护。Web 刚启动时的首次 HTTP 检查遇到连接未就绪，随后检查确认健康，未重复切换或回退。Web 74 个容器文件、72 个 HTTP 静态资源摘要核验通过。基于线上资源定点变更并验证反向替换可还原基底，保留线上 Mooncake 容量图及旧分块 URL。

镜像：

- monitoring-api：`sha256:441cb9964954f66e8e12f4e207091791e2ee3d620c51bbe0f0ea72181a6cf8de`
- code-eval Web：`sha256:dc8ad2a2e8cb277d107d999c521e387952a709222444ea968d2becc2db30fea1`

本地材料位于 `monitoring/deploy/a3-effective-cache-20261008/`、`monitoring/evidence/a3-effective-cache-20261008/` 和 `code-eval/deploy/releases/a3-effective-cache-20261008/`。截图位于 `code-eval/output/playwright/a3-effective-cache-20261008/`。

服务器新增目录（含其构建、捕获资源及证据子目录）：

- test4：`/data2/monitoring/releases/a3-effective-cache-20261008/`
- test1：`/data2/code-eval/releases/a3-effective-cache-20261008/`

A3 服务器未新增目录。
