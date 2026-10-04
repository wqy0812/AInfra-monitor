#!/usr/bin/env bash
set -euo pipefail
MONITORING_COVERAGE_ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$MONITORING_COVERAGE_ROOT"
.venv/bin/python --version
pyenv version
mkdir -p work/coverage
if [ "$#" -eq 0 ]; then set -- -q; fi
if .venv/bin/python -m coverage run -m pytest "$@"; then
  MONITORING_TEST_STATUS=0
else
  MONITORING_TEST_STATUS=$?
fi
.venv/bin/python -m coverage report
.venv/bin/python -m coverage json -o work/coverage/coverage.json
.venv/bin/python -m coverage html
exit "$MONITORING_TEST_STATUS"
