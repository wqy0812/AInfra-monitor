# 发布身份与历史检索

当前操作见 [项目 README](../../README.md)、[部署工具](../../deploy/README.md)、[Perses 维护](../../perses/README.md) 和 [停机窗口升级](../maintenance-window-upgrade.md)。本页版本来自既有发布记录，**不代表本次重新核验了线上状态**。

## 最近保留的版本记录

| 范围 | 记录日期 | 版本身份 | 证据位置 |
| --- | --- | --- | --- |
| 维护工具与生成器 | 2026-10-10 | 源码 `0b9b20fbc1783995508bac309833203a4d7397f9`；源码归档 SHA-256 `777a01447050d955692caf9da781b12b57fdafa4b6dd2a9c1ff55657f526a2f1` | test4 `/data2/monitoring/releases/columns-maintenance-20261010/evidence/` |
| monitoring-api | 2026-10-10 | 镜像 `sha256:56f68fd72bbcecf29acd4408b23975db3488be6893b1fd79e96c8d393dfe0401`；保留原 A3 catalog 和 current_jobs 修复 | test4 `/data2/monitoring/evidence/query-release-20261009/` |
| Perses | 2026-10-09 | `0.54.0-perf.3`；镜像 `sha256:978402ac5154e3ee2cf8c66f244fc7169be74125ef01a5fe9d28c1e8a24764e7` | 原 TLS 发布记录和 [构建版本锁](../../perses/performance/release-lock-perf3.json) |

工具发布记录确认运行时文件、公开看板定位、资源读回和相关健康检查；未执行新查询发布或历史重算。API 记录确认原 A3 覆盖、完成标记及查询恢复，持久水位保留。查询瘦身的常规性能门槛未全部通过，不能把单独授权发布记作常规性能验收通过；取舍依据见 [当前查询说明](../query-slimming.md)。

Perses TLS 的原记录确认 HTTPS、HTTP/2、认证和资源保留；证书与访问方式见 [维护说明](../../perses/README.md)。API 的增量镜像身份来自原记录，不将维护工具 commit 推定为整个 API 镜像的源码 commit。

## 当前说明与材料

- [指标口径与历史边界](../metrics-and-history.md)：请求范围、A3 贡献分母、采集完整性、CPU 聚合及流停顿。
- [时间范围与缓存](../time-navigation.md)、[查询加速](../perses-query-acceleration.md)、[当前加速清单](../../deploy/perses_acceleration/STATUS.md)。
- [代码退役索引](retired-code.md)：原路径及 SHA-256。

日期目录中的 Dockerfile、manifest、panels、版本锁和校验和保留。原始执行证据在忽略的 evidence/work 目录，Git 检出不能恢复它们。新发布更新本页对应组件的版本身份、验收范围及证据位置；过程日志和审核流水不另行提交，旧版本从 Git 查阅。

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
