"""Isolated VM acceptance for project query builders; run on test4 via SSH MCP."""
import json
import math
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from project_queries import Queries, ratio

URL = "http://127.0.0.1:18540"
NAME = "perses-project-split-fixture-20260915"


def evaluate(q, at, step):
    q = q.replace("$__interval", str(step) + "s")
    data = urllib.parse.urlencode({"query": q, "time": at, "nocache": 1}).encode()
    with urllib.request.urlopen(urllib.request.Request(URL + "/api/v1/query", data=data), timeout=30) as r:
        return json.load(r)["data"]["result"]


def main():
    root = Path(sys.argv[1])
    storage = root / "fixture-storage"
    assert root.is_dir() and not storage.exists()
    assert subprocess.run(["docker", "inspect", NAME], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0
    storage.mkdir(mode=0o700)
    started = False
    try:
        subprocess.check_output(["docker", "run", "-d", "--name", NAME, "--network", "host", "--cpus", ".5", "--memory", "256m",
            "-v", str(storage) + ":/storage", "--entrypoint", "/vm", "monitoring-vm:1.151.0",
            "-storageDataPath=/storage", "-httpListenAddr=127.0.0.1:18540", "-memory.allowedBytes=128MiB"])
        started = True
        for _ in range(50):
            try:
                urllib.request.urlopen(URL + "/health", timeout=1).close()
                break
            except OSError:
                time.sleep(.2)
        else:
            raise RuntimeError("Fixture startup failed")
        start = int(time.time() // 5) * 5 - 900
        cases = ["active", "zero", "absent", "gap", "stale", "down", "restart", "reset",
                 "missing-bucket", "bad-buckets", "bad-delta", "missing-count", "mixed-types", "missing-denominator"]
        lines = []
        for case in cases:
            for i in range(91):
                if case == "gap" and i == 89 or case == "stale" and i >= 85:
                    continue
                ts = (start + i * 5) * 1000
                def emit(metric, value, extra=None):
                    labels = {"job": "fixture", "instance": "node1", "environment": case, **(extra or {})}
                    tags = ",".join(k + "=" + json.dumps(v) for k, v in labels.items())
                    lines.append(f"{metric}{{{tags}}} {value} {ts}")
                emit("up", 0 if case == "down" and i == 89 else 1)
                emit("boot", start + 440 if case == "restart" and i >= 88 else start - 1000)
                if case == "absent":
                    continue
                value = 0 if case == "zero" else i * 5
                if case == "reset" and i >= 88:
                    value -= 440
                emit("work_total", value)
                emit("current", 0 if case == "zero" else 5)
                if case != "missing-denominator":
                    emit("known_total", 0 if case == "zero" else value * 2)
                variants = [("streaming", 1), ("nonstreaming", 2), (None, 3)] if case == "mixed-types" else [(None, 1)]
                for scope, factor in variants:
                    extra = {} if scope is None else {"request_scope": scope, "is_streaming": "true" if scope == "streaming" else "false"}
                    emit("typed_total", value * factor, extra)
                    for bound, multiplier in (("1", 1), ("2", 2), ("+Inf", 3)):
                        if case == "missing-bucket" and bound == "2":
                            continue
                        v = value * multiplier * factor
                        if case == "bad-buckets" and bound == "1":
                            v = value * 4
                        if case == "bad-delta" and bound == "1":
                            v = i * 12
                        if case == "bad-delta" and bound in ("2", "+Inf"):
                            v += 10000
                        emit("latency_bucket", v, dict(extra, le=bound))
                    if case != "missing-count":
                        emit("latency_count", value * 3 * factor + (10000 if case == "bad-delta" else 0), extra)
        body = ("\n".join(lines) + "\n").encode()
        urllib.request.urlopen(urllib.request.Request(URL + "/api/v1/import/prometheus", data=body), timeout=30).close()
        urllib.request.urlopen(URL + "/internal/force_flush", timeout=10).close()
        checks = []
        invalid = {"absent", "gap", "stale", "down", "restart", "reset"}
        for step in (15, 60):
            for case in cases:
                q = Queries(case, "fixture", origin="boot")
                expressions = {
                    "rate": q.rate("work_total", group="environment"),
                    "gauge": q.gauge("current"),
                    "ratio": ratio(q.rate("work_total", group="environment"), q.rate("known_total", group="environment"), "environment"),
                    "histogram": q.histogram("latency", ["1", "2", "+Inf"], .5, "environment"),
                    "types": q.rate("typed_total", group="environment"),
                }
                for name, expression in expressions.items():
                    rows = evaluate(expression, start + 450, step)
                    empty = case in invalid and not (name == "gauge" and case == "reset")
                    empty |= name == "histogram" and case in {"zero", "missing-bucket", "bad-buckets", "bad-delta", "missing-count"}
                    empty |= name == "ratio" and case in {"zero", "missing-denominator"}
                    if empty:
                        assert not rows, (case, step, name, rows)
                    else:
                        assert rows, (case, step, name, "unexpected empty")
                        value = float(rows[0]["value"][1])
                        expected = 0 if case == "zero" else (5 if name == "gauge" else 50 if name == "ratio" else 1.5 if name == "histogram" else 6 if name == "types" and case == "mixed-types" else 1)
                        assert math.isclose(value, expected, rel_tol=1e-6), (case, step, name, value, expected)
                    checks.append({"case": case, "step": step, "query": name, "passed": True})
        report = {"passed": True, "queries": len(checks), "checks": checks}
        (root / "semantics.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"passed": True, "queries": len(checks)}))
    finally:
        if started:
            subprocess.run(["docker", "rm", "-f", NAME], check=True, stdout=subprocess.DEVNULL)
        shutil.rmtree(storage)


if __name__ == "__main__":
    main()
