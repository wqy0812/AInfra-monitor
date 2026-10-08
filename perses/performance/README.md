# Perses 构建、镜像发布与性能测试

当前源码基于 Perses `0.54.0` / `4c719fc19fa21d333797e84c4fe7e3d81c25f4f5`。仓库的 `release-lock-perf3.json` 记录 perf.3 发布产物，`release-lock.json` 是历史 perf.2 产物；两者都不替代下一次构建生成的 lock。线上镜像须操作时重新核查。

## 源码与构建

`patches/` contains the seven modified TypeScript sources recovered from the
published packages' source maps. `source-lock.json` pins each original map,
the upstream source archive and npm lock. `apply-ui.cjs` verifies every source
before compiling patched ESM/CJS files with the locked SWC. The optional
`PluginLoader.importPlugin` path preserves legacy loaders. The remote loader
loads one plugin export; the builtin variable collector limits runtime loading
to modules referenced by the dashboard and its external variables. Editing
retains the full catalog. Prometheus-calculated interval placeholders do not
participate in variable cache keys; other unresolved variables wait.

Relative-range refresh relies on the new query key instead of immediately
invalidating the old query. Fixed/unchanged ranges still explicitly refetch.
Inactive entries are removed after observers have switched keys. Manual
refresh retains variable invalidation.

The perf.2 correction captures failed plugin query hashes before resetting their
state. React Query evaluates the reset filter again when refetching; a filter
that still asks for `status === "error"` no longer matches after reset. The
stable hash set restarts failed active imports while keeping successful imports.

`endpoint.patch` and `cache.go` add content validators and immutable caching
for hashed assets; HTML and manifests revalidate. ETags cover bytes after API
prefix replacement. The deployment prefix remains fixed; changing it requires
a fresh asset URL namespace/build to avoid reusing immutable URLs.

Build in a **new** local directory with Node 22 (nvm), Docker and an installed
Go 1.26.5 toolchain on PATH. Dependency preparation runs `go mod tidy` and
`go mod vendor`; tests/builds use `-mod=vendor` with
`GOPROXY=off GOSUMDB=off GOTOOLCHAIN=local`:

```sh
./build.sh /tmp/perses-performance-build-new 0.54.0-perf.3
```

The output includes the binary, image archive, checksums and a generated
`release-lock.json`. Preserve that generated lock with its exact archive.
The checked-in release locks identify exact artifacts, not future builds.
Versioned archives use `perses-<version>.tar.gz`. New deployment locks must pin
`previous_image_digest` and `previous_version` for upgrade identity checks. The build
reads these from `release-lock.json`, or an explicit previous-release lock passed
as its third argument. `image_release.py --lock FILE` selects a version-specific lock.
For a future upgrade from the deployed perf.3, pass `release-lock-perf3.json`
as the third argument instead of the default historical perf.2 lock.

## 当前行为与验证

perf.3 补丁提供标签页内的跨看板时间继承、后台页面暂停、固定窗口暂停自动刷新和手动刷新，保留已有图表定义；显式 URL 时间优先。设计与当次证据见 [时间导航说明](../../docs/releases/time-navigation-cache-20260924.md)、[发布记录](../../docs/releases/time-navigation-20260925-deployment.md)。

- `tests/ui.test.cjs`：刷新、定时器、变量、选择性导入与失败重试。
- `tests/cache_test.go`：内容校验、缓存头、ETag 304 和 HTML 重验证；随上游使用 `go test -mod=vendor ./ui`。
- `tests/test_image_admission.py` / `test_image_auth.py`：固定镜像、停机升级、完整项目资源快照、认证和升级基线。
- `tests/quantiles.py`：当前分位查询的真实 VM 合成语义检查。
- `local_candidate.py` / `local_samples.py`：需要性能专项时使用的本地隔离对照，校验镜像、平台和版本；不属于普通升级前置条件，也不代表生产性能。

所有构建/测试保持 vendor 模式和离线依赖约束。UI 变化按改动执行 1920×1080 浏览器检查，区分冷/热缓存、响应完成与实际绘制；未执行的检查不能记作通过。

## 镜像发布

连接与传输遵循工作区 SSH MCP 约束，命令在目标主机仓库根目录运行。先上传当前代码、镜像归档及本次生成的 release lock，显式创建新证据目录并告知用户：

```sh
python3 deploy/release.py image load --evidence DIR --lock DIR/release-lock.json
python3 deploy/release.py image apply --evidence DIR --lock DIR/release-lock.json
```

`apply` 检查镜像摘要、架构、版本和原镜像身份，在停机窗口内替换 Perses；保留 systemd、访问控制、数据挂载、并发编辑检查，启动后读取所有项目（包括自定义项目）的资源。凭据来自目标机 `admin-credentials.json` 或 `PERSES_CREDENTIALS_FILE`；令牌按服务隔离，过期最多重新认证一次。

没有候选服务、30 分钟观察或回退选项。失败保留当前状态、原资源快照及原始异常，修复后重新核验。旧容器只作为当前事务证据；存在同名临时容器时停止并要求先核查。相关看板变更通过 `deploy/release.py dashboards` 发布；查询合并专项通过 `deploy/release.py merges`，不再重跑旧 13 面板迁移器。

早期性能发布、旧回退过程和测量数字单独保存在 [历史记录](../../docs/releases/perses-performance-20260916.md)。
