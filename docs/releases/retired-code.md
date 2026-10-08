# 历史代码退役清单

本次清理前的源码提交为 `a624c42`（完整提交 ID、原路径、文件字节数、SHA-256 和退役原因见 [机器可读清单](retired-files.json)）。删除的是已完成发布批次及其独有检查、冻结源码和旧单项目看板基线；当前 API、查询语义、现行项目资源与加速目录没有改动。

可在仓库内只读检索原文：

```sh
git show a624c42:deploy/xpu_release.py
git show a624c42:perses/dashboards/overview.json
```

历史代码仅用于追溯，不恢复为发布入口。新的发布使用 [统一工具](../../deploy/README.md)，按新快照和本次已验证的镜像执行，禁止复用历史单次授权或验收结论。

## 依赖处理

- 网关历史只读检查迁到 `scripts/check_gateway_history.py`，保留拒绝 VM 写入的回归。
- 管理状态并发更新测试从旧影子监视测试中提取到 `tests/test_acceleration_admin.py`。
- 查询/生成器所用 `generate.py`、`gateway_generation.py`、`generate_xpu.py`、迁移与平台辅助模块保留；只删除旧 CLI 的不可达实现。
- 加速 API 的构建/替换复用通用容器入口，启动验收由 `api_readiness.py` 提供。续办工具读取新证据，也能只读识别历史证据；不生成旧回退结果。
- 容器、镜像、看板、生成器及加速发布的回退函数/选项已删除，现行失败留证和并发编辑测试保留。固化于旧批次的性能豁免发布器也已退役。
- 没有仅因“无代码引用”就删除通用 CLI 诊断：内存窗口、覆盖探针、浏览器首屏、性能复验和本地合成工具仍保留，用途见 [加速运维](../../deploy/perses_acceleration/README.md)。

版本锁、manifest、源码摘要、采集输入、必要测试样本和历史正文仍保留。执行报告、容器快照与凭据不新增到 Git；远端旧文件不受本地清理影响。
