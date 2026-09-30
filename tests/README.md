# Python 回归测试

使用 pyenv 管理的 Python 3.11+ 和仓库 `.venv`。`requirements-test.txt` 包含监控运行依赖、部署校验依赖、pytest 及 pytest-asyncio，不需要 code-eval 后端。准备环境时执行：

```sh
.venv/bin/python -m pip install -r requirements-test.txt
```

## 按改动选择回归

后续按 [停机窗口升级](../docs/maintenance-window-upgrade.md)，普通发布仅运行相关用例及启动后的功能核验。真实 VM 仅在查询、统计、采集/导入或存储逻辑受影响时使用；UI 改动执行对应 UI/浏览器检查，性能改动执行对应正确性与性能对照。

从仓库根目录按文件选择测试，无需额外的 PYTHONPATH，例如部署工具改动：

```sh
.venv/bin/python -m pytest tests/test_deployment_tools.py tests/test_forward_repair.py -q
```

`pytest.ini` 指定收集 `tests/` 和 `perses/`，其中包括 `perses/performance/tests/` 的 Python 测试；本轮不移动 Perses 测试。`work/`、`evidence/`、离线 vendor 和虚拟环境不参与收集。`tests/test_cache_semantics.py` 只验证 monitoring 自身的缓存语义。

需要完整 Python 回归时显式运行 `.venv/bin/python -m pytest tests perses -q`。真实 VictoriaMetrics 语义用例在未配置可丢弃测试实例时跳过；浏览器、Go、前端 Jest 和线上验收不包含在此命令中。跳过不表示相应验收通过。

## 本地 Docker VictoriaMetrics 回归

Docker Desktop 启动后，从仓库根目录执行：

```sh
./scripts/test_local_vm.sh
```

入口使用 [Compose 配置](compose.vm.yml) 启动 `monitoring-test-vm`，版本为
VictoriaMetrics 1.151.0，地址为 `http://127.0.0.1:18543`。运行前会重建该测试容器，
清空 tmpfs 中的合成数据，并设置 `HOST_CPU_TEST_VM_URL`、`GATEWAY_TEST_VM_URL`、
`PERSES_ACCELERATION_TEST_VM_URL`，默认仅执行 `test_host_cpu_vm.py`、
`test_gateway_live_vm.py` 和 `test_perses_acceleration.py` 三个 VM 语义测试文件。
容器限制为 1 CPU / 1 GiB 内存，数据随容器停止丢弃。测试成功、失败或中断后，
入口都会停止并移除该测试容器及其 Compose 网络；平时不运行。镜像保留在本机缓存中。

可传入 pytest 参数缩小到相关文件；需要完整 Python 回归时显式指定全部测试目录：

```sh
./scripts/test_local_vm.sh tests/test_host_cpu_vm.py -q -rs
./scripts/test_local_vm.sh tests perses -q -rs
```

仅启动实例，或停止并移除实例：

```sh
docker compose -f tests/compose.vm.yml up -d
docker compose -f tests/compose.vm.yml down
```

## 已删除的历史升级测试

2026-09-30 删除依赖 code-eval 退役发布脚本的“活动任务不能冻结”测试及其跨项目加载/开关。该用例验证旧升级流程，不适用于当前 monitoring 停机窗口升级；`--run-cross-project` 和 `CODE_EVAL_ROOT` 不再是本项目测试入口。
