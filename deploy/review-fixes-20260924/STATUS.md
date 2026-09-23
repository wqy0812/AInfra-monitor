# 审核修复发布 — 2026-09-24

通过 SSH MCP 完成部署，时间约为北京时间 00:14–00:16。

| 节点 / 服务 | 本次覆盖文件 | 源码提交 |
| --- | --- | --- |
| test4 / monitoring-api | `/monitoring/monitoring/xpu_cache.py` | `4ed8470` |
| test1 / code-eval-web | `/app/frontend/static/app.js`、`/app/backend/app/main.py` | `d35e75c` |

缓存查询独立限时四秒并隔离异常；Web Prefill 标注修正为 XPU-1；非 DCU 环境需要独立监控的提示改为通用文案。采用基于现有镜像的源文件覆盖，保留原容器配置。其他已核对的 API、缓存适配器、网关历史和前端文件与本地提交一致，没有重复覆盖。

## 验证

- 候选镜像在无网络环境通过合成缓存超时、其他监控数据保留、截止时间和取消清理验证；Web 候选标注及 Python 语法检查通过。
- 两端健康接口正常，最终容器均运行且重启计数为 0；Web Docker health 为 healthy。
- 两端 DCU/A3/XPU 三小时历史接口均返回数据，四个 gateway_status 字段均为 ok。XPU 540 个有效缓存点；该数字仅对应验收时窗口。
- 发布后容器内源码 SHA-256 与 manifest 一致，线上 `/static/app.js` 返回 XPU-1 标注。
- Engine、VM、vmagent、Perses 的运行容器 ID 和启动时间未变化；本次未操作网关或推理进程。
- A3 Prefill/Decode 采集源在发布前后均报告 error；A3 历史接口成功不表示当前推理源已恢复。本次没有修复该既有问题。

结果见 [监控验收](monitoring-complete.json) 和 [Web 验收](web-complete.json)。完整容器配置快照仅保留在服务器的受限发布目录中，不纳入 Git。

## 服务器新增目录

- test4：`/data2/monitoring/releases/review-fixes-20260924`
- test1：`/data2/code-eval/releases/review-fixes-20260924`

## 回退

旧容器已停止并保留为 `monitoring-api-before-review-fixes-20260924` 和 `code-eval-web-before-review-fixes-20260924`。需要回退时，通过 SSH MCP 在对应节点执行，先 Web 后监控；脚本会验证旧容器及当前镜像身份，保留失败候选。

```sh
# test1
python3 /data2/code-eval/releases/review-fixes-20260924/release.py web rollback
# test4
python3 /data2/monitoring/releases/review-fixes-20260924/release.py monitoring rollback
```

回退后重新检查健康接口、XPU 最新数据和三个环境的历史接口。不要重复运行 switch；新的发布应创建新的版本目录。
