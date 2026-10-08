# Perses performance release, 2026-09-16

Historical execution only. Current publication uses maintenance windows and forward repair.

## Production release (2026-09-16)

This section records historical execution and does not prescribe future upgrade
gates. Historical evidence root: `/data2/monitoring/perses/evidence/performance-20260916`.

The user explicitly authorized restoring the local `ssh -N jump` SOCKS listener
and a separate temporary loopback forward to the candidate. Port 1080 is working,
and three production dashboards were reopened in Chrome at 1920x1080. The
temporary candidate forward was rejected by test4 with `administratively
prohibited`; its effective SSH configuration is `allowtcpforwarding no` and
`permitopen none`. The failed local forward was closed. No server SSH or firewall
policy was changed. Remote commands and file transfers used SSH MCP.

After the blocked candidate browser/soak checks were disclosed, the user
explicitly instructed deployment. The recorded authorization moved browser
validation to production. No candidate browser or soak report was fabricated.

Production now runs **0.54.0-perf.2**, image config
`sha256:c5a17dc68da543e42b3c78618b0c452ea29328a027ec4439b402f3aa79a15bfa`.
The exact locked archive was verified on test4. Candidate APIs, all 16 dashboard
and two datasource copies, both proxies, HTML revalidation, immutable hashed
assets and ETag 304 responses passed. Data-copy ownership and resource-response
checks are now part of candidate preparation.

The final 13-panel publication passed 52 range/step groups and all performance
gates before and after publication. Each panel has 41 paired cold and 41 paired
warm samples. Post-publication cold median reductions are 46.6–72.8%, with a
panel median of 61.1%. The earlier seven-pair attempts triggered automatic
rollback; both failure records are retained. The final larger cohort was fixed
before running, and did not relax either performance threshold. Empty one-hour
windows remain in the evidence; data over 24 hours also matches.

Production perf.1 completed 1826 seconds with 121 cycles, 12 requests per cycle,
1452 HTTP 200 responses, and a settled cache of 27 entries/12 panel queries.
Perf.2 changes only failed-plugin retry. Its production tests separately confirm
successful initial loads, warm cache reuse, injected plugin failure and recovery,
variable changes, manual refresh, and DCU 24-hour quantile legends. The perf.1
soak is not presented as a 30-minute perf.2 soak. Perf.2 separately completed
343 seconds/23 cycles/276 HTTP 200 responses, with 27 cache entries and
12 panel queries (`production-refresh-perf2.json`).

Only three dashboard specs/13 target panels changed; existing guards and
descriptions from the latest online snapshot were preserved. Other dashboards,
projects, datasources, and protected service container IDs/start times match the
pre-cutover snapshot. Candidate containers are stopped. Original and perf.1
containers remain as `monitoring-perses-before-perf1` and
`monitoring-perses-before-perf2`.

Evidence: `image-publication.json`, `quantiles-publication.json`,
`quantiles-audit.json`, `production-resource-validation.json`,
`production-browser-perf2.json`, `production-soak-perf1.json`,
`production-service-observation.json`, and `final-deployment.json` in the remote
evidence root and local `../../evidence/performance-20260916/`.

### Historical rollback procedure (superseded 2026-09-28)

Current policy requires forward repair; do not execute these historical commands.
Automatic rollback has been removed from image and query publication.
The following describes the former procedure only.

The former test4 procedure rolled back quantile changes with
`publish_quantiles.py rollback --evidence DIR` (set
`PYTHONPATH=/data2/monitoring/perses/release`). It checks current content before
restoring the latest pre-publication specs.

For the image, `image_release.py rollback --evidence DIR --lock DIR/release-lock-perf2.json`
returns perf.2 to perf.1. A subsequent rollback with `DIR/release-lock-perf1.json`
returns perf.1 to the original 0.54.0 image. Image rollback alone keeps dashboard
definitions; the two batches have independent rollback paths.
