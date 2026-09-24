# Perses 0.54.0 performance patches

Two independently gated releases. The source base is commit
`4c719fc19fa21d333797e84c4fe7e3d81c25f4f5`; the original image remains pinned
in `../image-lock.json`. No user-authored dashboards are replaced with a stale
repository snapshot during publication.

## First batch

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
`previous_image_digest` and `previous_version` for upgrade and rollback. The build
reads these from `release-lock.json`, or an explicit previous-release lock passed
as its third argument. `image_release.py --lock FILE` selects a version-specific lock.
For a future upgrade from the deployed perf.3, pass `release-lock-perf3.json`
as the third argument instead of the default historical perf.2 lock.

## Second batch

`quantile_release.py` prepares a snapshot-derived patch for precisely 13 panels:
four histogram panels in each gateway-requests dashboard and five A3 backend
histograms. The original three query tails/guards must match exactly. They are
replaced with one `histogram_quantiles` and one validity mask. `perses_quantile`
is a presentation label carrying P50/P95/P99. Original source/grouping, missing
values and validity semantics are retained. Custom query-index styling fails
closed instead of being silently discarded.

The generator uses the same shape; only the three corresponding project JSON
files change. There are no recording rules, schema changes or history rewrites.

## Validation and evidence

The local **perf.3** candidate adds tab-scoped time range inheritance (explicit
URL ranges win), visibility-aware relative refresh, paused fixed windows, and
focus/reconnect suppression for chart and variable queries. It preserves manual
refresh and existing dashboard specs. The toolbar explains fixed-window pause.
Absolute URL ranges retain milliseconds. The earlier builtin plugin collector's
`ListVariable` metadata kind is corrected to upstream's `Variable` kind.
See [behavior and acceptance](../../docs/time-navigation-cache-20260924.md).
Perf.3 was deployed on 2026-09-25 with local browser acceptance and remote
candidate/production API validation. The remote browser and 1800-second remote
soak were not run. See the [deployment record](../../deploy/time-navigation-20260925/README.md).
The existing `release-lock.json` still identifies the previous perf.2 release.
Use `release-lock-perf3.json` for the deployed artifact.
The perf.3 lock pins the deployed perf.2 image as its previous release. Resource
validation logs in using the server-local `admin-credentials.json` (or
`PERSES_CREDENTIALS_FILE`), keeps tokens separate for production and candidate,
retries an expired token once, and enumerates every project, including XPU and
custom projects. Credentials and tokens are never written to release reports.

- `tests/ui.test.cjs`: real React Query/React provider tests for relative/fixed
  refresh, timers, inactive cleanup, variable changes, selective imports and retry.
- `tests/test_image_admission.py`: default admission still requires browser
  evidence; explicitly authorized production validation retains deferred checks
  and requires exact-image API evidence.
- `tests/test_image_auth.py`: authenticated resource snapshots, XPU change
  detection, per-server tokens, expiry recovery and explicit upgrade baselines.
- `tests/cache_test.go`: substituted response body, cache headers, 304 responses,
  changed prefix validators and HTML revalidation. Runs with upstream `go test -mod=vendor ./ui`.
- `tests/quantiles.py`: 162 synthetic assertions against localhost VM 1.151.0.
- `tests/dashboard_quantiles.py`: all 13 exact dashboard families, 52 range/step
  comparisons, alternating cold/warm seven-pair performance samples. The fixture
  seeds one hour; its 24-hour queries intentionally also cover missing history.
- `local_candidate.py` starts baseline/candidate containers on loopback 18542/18541,
  using only local JSON and a synthetic localhost VM on 18543. `local_samples.py`
  provides synthetic chart observations. These are not production measurements.

`local_candidate.py --lock FILE` selects a candidate release lock; the default is
the checked-in `release-lock.json`. It verifies each image digest, Linux/amd64
architecture and binary version before starting the long-running containers.
The candidate version label must also match. The baseline remains pinned to
`../image-lock.json`; a mutable image tag is never used as the candidate identity.

Image publication now rejects an occupied version-specific backup name before
stopping the service. Stop, rename, create, start and acceptance share one recovery
path, which reconciles container IDs even after an uncertain Docker response.
Only this transaction's replacement may be removed; a failed rename cannot delete
the original container. These tool changes require separate deployment acceptance.

Browser checks use the computer-use browser/CDP APIs with a 1920x1080 viewport.
Cold and warm cache samples are separated, with first query, last initial
response, request counts, plugin counts and bytes recorded. Response completion
is not claimed as exact chart paint time. Do not treat local evidence as remote
acceptance. Local evidence is under `../../evidence/performance-20260916/`.

## Remote publication, via SSH MCP only

Remote evidence root: `/data2/monitoring/perses/evidence/performance-20260916`.
Notify the user before creating any new remote directories. Never run local
command-line SSH, SCP or port-forwarding subprocesses in these scripts.

1. Upload the archive, generated release lock and first-batch scripts. Run
   `image_release.py load --evidence DIR`, then `candidate`. The candidate binds
   only `127.0.0.1:18541` and uses `candidate-data` / `candidate-config.yaml` copied
   from the current server. The script preserves source ownership in the copied
   data and checks all dashboard/datasource responses, since health alone does
   not detect unreadable resources. Reach it through an approved test route.
2. Record remote candidate browser checks in `remote-browser-acceptance.json`
   (`passed`, `environment="remote-candidate"`, exact `image`) and an actual
   >=1800-second remote soak in `remote-soak.json` (`passed`, `elapsed_seconds`,
   exact `image`). This is the default admission path; do not manufacture reports.
   When the user explicitly instructs deployment after the blocked checks are
   disclosed, `--defer-browser-validation` requires `deployment-authorization.json`
   and exact-image candidate API evidence. It records deferred checks in the
   publication report; it does not mark candidate browser or soak tests passed.
   If the user explicitly selects local browser acceptance, use
   `--local-browser-validation` with that instruction and
   `validation_mode="local-browser"` in `deployment-authorization.json`.
   It requires `local-browser-acceptance.json` tied to the exact archive config
   digest plus successful remote candidate API evidence. The report explicitly
   records that remote browser and the 1800-second remote soak were not run.
   Evidence directories are resolved to absolute paths before Docker bind mounts.
3. Run `image_release.py apply --evidence DIR`. It preserves the original container
   as `monitoring-perses-before-perf3` for perf.3 (the suffix follows the selected
   version), keeps systemd/access controls, checks
   unchanged resources and protected monitoring services, and restores the old
   container on failure. Explicit rollback is `image_release.py rollback`.
4. Capture a fresh dashboard snapshot after the first batch. Prepare changes from
   that snapshot, then copy `changes.json`, `quantile-semantics.json` and the
   query publication scripts into the evidence directory. Run
   `publish_quantiles.py audit --evidence DIR`; it checks exact old/new and proxy
   values and refuses acceptance when actual samples are absent or performance
   fails. Run `apply` only after the first batch was accepted. Journaled updates
   affect three dashboards; `rollback` restores only matching candidate specs.
   `--samples N` fixes the cold and warm paired sample count per panel for both
   audit and apply (minimum 7). The final production cohort uses 41 samples;
   the 20% median improvement and 5% maximum regression gates are unchanged.
   Earlier failed attempts and their rollback journals remain in evidence.

## Production release (2026-09-16)

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

### Rollback

Run through SSH MCP on test4. Roll back quantile changes with
`publish_quantiles.py rollback --evidence DIR` (set
`PYTHONPATH=/data2/monitoring/perses/release`). It checks current content before
restoring the latest pre-publication specs.

For the image, `image_release.py rollback --evidence DIR --lock DIR/release-lock-perf2.json`
returns perf.2 to perf.1. A subsequent rollback with `DIR/release-lock-perf1.json`
returns perf.1 to the original 0.54.0 image. Image rollback alone keeps dashboard
definitions; the two batches have independent rollback paths.
