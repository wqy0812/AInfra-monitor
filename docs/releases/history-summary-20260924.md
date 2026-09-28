# history-summary-20260924 — deployed

本地执行结果保存在 `evidence/history-summary-20260924/`（相对于仓库根目录，已忽略，不随 Git 提供）；下文中的服务器路径仍是当次发布记录。脚本及发布输入保留在 [原发布目录](../../deploy/history-summary-20260924/)。

2026-09-24: monitoring-api on test4 and code-eval-web on test1 were updated through SSH MCP. Only monitoring/api.py and Web main.py, monitor_client.py, app.js were overlaid on the respective live images. Before hashes matched Git HEAD. Runtime configuration was preserved; engine, VM, vmagent and Perses were unchanged. Previous containers remain stopped for rollback.

Read-only candidate checks against live VM data compared full and summary results for all three environments over fixed 1-hour and 3-hour windows. Aggregate values and gap markers matched exactly. DCU 1-hour payload: 5,747,400 -> 725,053 bytes; candidate query duration: 1.257 -> 0.317 seconds. These candidate timings are not steady-state Web latency.

First Web switch encountered HTTP 503 and automatically rolled back. A read-only candidate check subsequently passed, and a second switch passed all three environments. Final Web health is healthy, with no container restarts. Web history response: DCU 719,574 bytes / 4.176 seconds; A3 771,805 bytes / 5.533 seconds; XPU 698,970 bytes / 3.291 seconds.

Pre-existing A3 PoolTimeout had stopped its watermark before deployment. API restart resumed A3 catch-up; its watermark advanced, but it remains behind current time. monitoring-api reports degraded and CPU was near its 1-CPU quota during catch-up. The release verifies payload reduction and successful requests, not resolution of this separate backlog or stable sub-second latency.

Remote release directories created:
- test4: /data2/monitoring/releases/history-summary-20260924
- test1: /data2/code-eval/releases/history-summary-20260924

Rollback containers: monitoring-api-before-history-summary-20260924 and code-eval-web-before-history-summary-20260924. The failed first Web candidate is retained stopped as code-eval-web-failed-history-summary-20260924. Each release directory contains the manifest, image build evidence and complete.json. Container inspection evidence on the hosts is private (umask 077); it was not downloaded.
