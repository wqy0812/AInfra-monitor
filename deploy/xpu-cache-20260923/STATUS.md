# XPU cache hit rate — deployed 2026-09-23

Updated monitoring-api on test4 and code-eval-web on test1 through SSH MCP. Both switches passed health and live 3-hour history verification. Images, preserved rollback container names and results are in monitoring-complete.json and web-complete.json. Existing container configuration was retained; no engine, gateway, inference, VM or collector restart occurred.

The native representative Prefill rank gauge now supplies latest/cache history and the Web chart. Raw retained VM history is queried directly to recover earlier values that the old materializer blanked; no historical data is rewritten. The verified live window reports 0% from the raw source, and the API preserves zero instead of null. Positive values (97.44%), zero, missing/invalid samples, scrape failures, ambiguity and stale data were covered with fixtures. Existing DCU/A3 behavior is preserved.

Validation: 48 focused Python tests passed. XPU desktop browser and gateway monitoring regressions passed at 1920×1080. The broad test run found four pre-existing failures (query-count, target-count and gateway-field expectations), reproduced using the pre-change api.py/xpu.py; two initial import-path failures passed with the complete PYTHONPATH. VM-dependent tests without their fixture server were skipped. Production candidate queries were read-only and completed well within the API timeout.

Server directories created:
- test4: /data2/monitoring/releases/xpu-cache-20260923
- test1: /data2/code-eval/releases/xpu-cache-20260923

Each retains its release files, restricted original container snapshot, Dockerfile/build log and verification record. The deployment script automatically restores the previous container if verification fails. Manual rollback through SSH MCP: stop and rename the new target container, rename the corresponding *-before-xpu-cache-20260923 container back, start it, and verify health. Roll back Web before monitoring if reverting both.

## Local review fixes — 2026-09-24

Cache history queries now have independent four-second deadlines and isolate HTTP, timeout and malformed-response failures. Missing values remain blank; missing continuity keeps valid samples but breaks connecting lines. Other monitoring history remains available, and request cancellation still propagates. The Web Prefill label now identifies XPU-1. Regression expectations include four gateway history fields and the additional A3 Mooncake target.

Local validation: 304 monitoring/Perses tests passed; 218 VM-dependent cases skipped without their local fixture server. All three XPU/profile/gateway browser checks passed at 1920×1080. These review fixes have not been deployed; the deployment results above describe the September 23 release.
