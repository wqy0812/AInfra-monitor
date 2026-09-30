# Python 回归测试

使用 pyenv 管理的 Python 3.11+ 和仓库 `.venv`。`requirements-test.txt` 包含监控运行依赖、部署校验依赖、pytest 及 pytest-asyncio；不包含可选 code-eval 后端的依赖。准备环境时执行：

```sh
.venv/bin/python -m pip install -r requirements-test.txt
```

## 默认回归

从仓库根目录执行，无需 code-eval 或额外的 PYTHONPATH：

```sh
.venv/bin/python -m pytest -q
```

`pytest.ini` 指定收集 `tests/` 和 `perses/`，其中包括 `perses/performance/tests/` 的 Python 测试；本轮不移动 Perses 测试。`work/`、`evidence/`、离线 vendor 和虚拟环境不参与收集。`tests/test_cache_semantics.py` 只验证 monitoring 自身的缓存语义。

`cross_project` 用例默认显示跳过原因，不读取同级源码。真实 VictoriaMetrics 语义用例仍按已有规则，在未配置可丢弃测试实例时跳过；浏览器、Go、前端 Jest 和线上验收不包含在此 Python 命令中。跳过不表示相应验收通过。

## 本地 Docker VictoriaMetrics 回归

Docker Desktop 启动后，从仓库根目录执行：

```sh
./scripts/test_local_vm.sh
```

入口使用 [Compose 配置](compose.vm.yml) 启动 `monitoring-test-vm`，版本为
VictoriaMetrics 1.151.0，地址为 `http://127.0.0.1:18543`。运行前会重建该测试容器，
清空 tmpfs 中的合成数据，并设置 `HOST_CPU_TEST_VM_URL`、`GATEWAY_TEST_VM_URL`、
`PERSES_ACCELERATION_TEST_VM_URL`，执行包含真实 VM 语义用例的完整 Python 回归。
容器限制为 1 CPU / 1 GiB 内存，数据随容器停止丢弃。测试成功、失败或中断后，
入口都会停止并移除该测试容器及其 Compose 网络；平时不运行。镜像保留在本机缓存中。

可传入 pytest 参数，例如只运行真实 VM 用例，或同时启用跨项目兼容性检查：

```sh
./scripts/test_local_vm.sh tests/test_host_cpu_vm.py tests/test_gateway_live_vm.py tests/test_perses_acceleration.py -q -rs
./scripts/test_local_vm.sh --run-cross-project -q -rs
```

仅启动实例，或停止并移除实例：

```sh
docker compose -f tests/compose.vm.yml up -d
docker compose -f tests/compose.vm.yml down
```

## 跨项目兼容性

`tests/integration/test_code_eval_cache.py` 保留一条历史兼容性测试：旧缓存发布器不能冻结活动任务。显式启用：

```sh
CODE_EVAL_ROOT=../code-eval .venv/bin/python -m pytest tests/integration --run-cross-project -q -rs
```

`CODE_EVAL_ROOT` 可指定兼容的 code-eval 检出，省略时使用同级目录。需另行准备该版本的 Python 依赖。fixture 在执行阶段才加载其 `backend/`，并将 `DATA_DIR`、`HOST_DATA_DIR`、`STATE_DIR` 指向测试临时目录。

该用例需要 `deploy/releases/legacy_20260908_20260916/bin/cache_monitor_host.py`。缺少明确前置文件时跳过，模块存在但依赖导入或行为错误时失败。依赖已移除 `app.monitor` 的旧采集器测试已删除。
