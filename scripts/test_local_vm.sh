#!/usr/bin/env bash
set -euo pipefail

MONITORING_ROOT=$(cd "$(dirname "$0")/.." && pwd)
MONITORING_TEST_PYTHON="$MONITORING_ROOT/.venv/bin/python"
MONITORING_TEST_VM_URL=http://127.0.0.1:18543

cd "$MONITORING_ROOT"
"$MONITORING_TEST_PYTHON" --version
pyenv version

MONITORING_DOCKER_ENDPOINT=${DOCKER_HOST:-$(docker context inspect --format '{{.Endpoints.docker.Host}}')}
case "$MONITORING_DOCKER_ENDPOINT" in
  unix://*) ;;
  *) echo "本地测试需要本机 Docker Unix socket。" >&2; exit 1 ;;
esac

cleanup() {
  local MONITORING_TEST_EXIT=$?
  trap - EXIT INT TERM
  if ! docker compose -f tests/compose.vm.yml down --timeout 10; then
    echo "测试容器清理失败，请检查 monitoring-test-vm。" >&2
    if [ "$MONITORING_TEST_EXIT" -eq 0 ]; then
      MONITORING_TEST_EXIT=1
    fi
  fi
  exit "$MONITORING_TEST_EXIT"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Recreating the dedicated container clears its tmpfs before each test run.
docker compose -f tests/compose.vm.yml up -d --force-recreate vm
"$MONITORING_TEST_PYTHON" - "$MONITORING_TEST_VM_URL" <<'PY'
import sys
import time
import urllib.request

url = sys.argv[1] + '/health'
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
deadline = time.monotonic() + 30
while True:
    try:
        with opener.open(url, timeout=1) as response:
            if response.status == 200:
                break
    except OSError:
        pass
    if time.monotonic() >= deadline:
        raise SystemExit('VictoriaMetrics 未在 30 秒内就绪：' + url)
    time.sleep(.25)
print('VictoriaMetrics ready: ' + sys.argv[1])
PY

export HOST_CPU_TEST_VM_URL="$MONITORING_TEST_VM_URL"
export GATEWAY_TEST_VM_URL="$MONITORING_TEST_VM_URL"
export PERSES_ACCELERATION_TEST_VM_URL="$MONITORING_TEST_VM_URL"

if [ "$#" -eq 0 ]; then
  set -- -q -rs
fi
"$MONITORING_TEST_PYTHON" -m pytest "$@"
