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

## 跨项目兼容性

`tests/integration/test_code_eval_cache.py` 保留一条历史兼容性测试：旧缓存发布器不能冻结活动任务。显式启用：

```sh
CODE_EVAL_ROOT=../code-eval .venv/bin/python -m pytest tests/integration --run-cross-project -q -rs
```

`CODE_EVAL_ROOT` 可指定兼容的 code-eval 检出，省略时使用同级目录。需另行准备该版本的 Python 依赖。fixture 在执行阶段才加载其 `backend/`，并将 `DATA_DIR`、`HOST_DATA_DIR`、`STATE_DIR` 指向测试临时目录。

该用例需要 `deploy/releases/legacy_20260908_20260916/bin/cache_monitor_host.py`。缺少明确前置文件时跳过，模块存在但依赖导入或行为错误时失败。依赖已移除 `app.monitor` 的旧采集器测试已删除。
