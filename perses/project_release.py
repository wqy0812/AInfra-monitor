"""Project-aware Perses publication, audit and resource-scoped rollback.

Run on test4 through SSH MCP. This module never starts remote connections.
"""
import argparse
import concurrent.futures
import copy
import hashlib
import json
import math
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from project_split import PROJECTS, no_request_filter, read_resources, validate

BASE = "http://122.247.53.162:18431"
VM = "http://127.0.0.1:18428"
SERVICES = ("monitoring-perses", "monitoring-vm", "monitoring-vmagent", "monitoring-api")


TOKEN = None


def auth_headers():
    global TOKEN
    credentials = Path(os.environ.get('PERSES_CREDENTIALS_FILE', '/data2/monitoring/perses/admin-credentials.json'))
    if TOKEN is None and credentials.exists():
        req = urllib.request.Request(BASE + '/api/auth/providers/native/login',
            data=credentials.read_bytes(), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=20) as response:
            TOKEN = json.load(response)['access_token']
    return {'Authorization': 'Bearer ' + TOKEN} if TOKEN else {}


def http(url, method="GET", data=None):
    headers = auth_headers()
    if data is not None:
        data = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            body = r.read()
            return json.loads(body) if body else None
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise RuntimeError(f"HTTP {e.code} {method} {url}: {body[:1200]}") from e


def save(root, name, data):
    (root / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def spec(document):
    s = copy.deepcopy(document["spec"])
    for var in s.get("variables", []):
        v = var["spec"]
        v.setdefault("allowAllValue", False)
        v.setdefault("allowMultiple", False)
        v.setdefault("display", {}).setdefault("hidden", False)
    s.setdefault("variables", []) if document["kind"] == "Dashboard" else None
    return s


def fingerprint():
    items = json.loads(subprocess.check_output(["docker", "inspect", *SERVICES]))
    return [{"name": c["Name"], "id": c["Id"], "started": c["State"]["StartedAt"], "image": c["Image"]} for c in items]


def snapshot():
    projects = http(BASE + "/api/v1/projects")
    data = {"projects": projects, "datasources": [], "dashboards": []}
    for p in projects:
        for kind in ("datasources", "dashboards"):
            data[kind] += http(BASE + "/api/v1/projects/" + p["metadata"]["name"] + "/" + kind)
    return data


def endpoint(kind, document):
    plural = {"Project": "projects", "Datasource": "datasources", "Dashboard": "dashboards"}[kind]
    prefix = BASE + "/api/v1"
    if kind != "Project":
        prefix += "/projects/" + document["metadata"]["project"]
    return prefix + "/" + plural


def key(document):
    return document["kind"], document["metadata"].get("project", ""), document["metadata"]["name"]


def flattened(resources):
    return {key(d): d for group in resources.values() for d in group}


def digest(resources):
    return hashlib.sha256(json.dumps(resources, sort_keys=True).encode()).hexdigest()


def equivalent(a, b):
    """Label/timestamp equality with a tight tolerance for parallel float sums."""
    if len(a) != len(b):
        return False
    for left, right in zip(a, b):
        if left["metric"] != right["metric"] or len(left["values"]) != len(right["values"]):
            return False
        for x, y in zip(left["values"], right["values"]):
            if x[0] != y[0]:
                return False
            if x[1] != y[1] and not math.isclose(float(x[1]), float(y[1]), rel_tol=1e-10, abs_tol=1e-10):
                return False
    return True


def query(base, expression, start, end, step):
    expression = expression.replace("$__interval", str(step) + "s")
    for name in ("role", "node", "device"):
        expression = expression.replace("$" + name, ".*")
    params = {"query": expression, "start": start, "end": end, "step": step, "nocache": "1"}
    req = urllib.request.Request(base + "/api/v1/query_range", data=urllib.parse.urlencode(params).encode(),
                                 headers=auth_headers() if base.startswith(BASE) else {})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(e.read().decode()[:1500]) from e
    assert data["status"] == "success", data
    return sorted(data["data"]["result"], key=lambda x: json.dumps(x["metric"], sort_keys=True))


def panel_datasource(resources, dashboard, query_spec):
    """Match explicit references or the project's default Prometheus datasource."""
    reference = query_spec.get('datasource') or {}
    if reference.get('name'):
        return reference['name']
    project = dashboard['metadata']['project']
    defaults = [d['metadata']['name'] for d in resources['datasources']
                if d['metadata'].get('project') == project and d['spec'].get('default')
                and d['spec']['plugin']['kind'] == 'PrometheusDatasource']
    if len(defaults) != 1:
        raise ValueError('Expected one default Prometheus datasource for ' + project)
    return defaults[0]


def audit(resources, root, published=False):
    end = int(time.time() // 60) * 60 - 180
    start = end - 1800
    work = []
    for d in resources["dashboards"]:
        for pid, p in d["spec"]["panels"].items():
            for i, item in enumerate(p["spec"]["queries"]):
                for step in (15, 60):
                    work.append((d, pid, i, item["spec"]["plugin"]["spec"], step))
    def check(entry):
        d, pid, i, query_spec, step = entry
        project = d["metadata"]["project"]
        proxy_project = project if published else "dcu-monitoring"
        result = {"project": project, "dashboard": d["metadata"]["name"], "panel": pid, "query": i, "step": step}
        try:
            datasource = panel_datasource(resources, d, query_spec)
            result['datasource'] = datasource
            proxy = BASE + "/proxy/projects/" + urllib.parse.quote(proxy_project, safe='') + "/datasources/" + urllib.parse.quote(datasource, safe='')
            expression = query_spec['query']
            a = query(VM, expression, start, end, step)
            b = query(proxy, expression, start, end, step)
            if not equivalent(a, b):
                raise AssertionError("VM/proxy response differs")
            result.update(series=len(a), points=sum(len(r["values"]) for r in a),
                          environments=sorted({r["metric"].get("environment", "") for r in a}))
        except Exception as exc:
            result["error"] = str(exc)
        return result
    checks = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        for result in pool.map(check, work):
            checks.append(result)
            if len(checks) % 60 == 0:
                print(json.dumps({"checked": len(checks), "total": len(work), "errors": sum("error" in x for x in checks)}), flush=True)
    report = {"passed": all("error" not in x for x in checks), "resource_sha256": digest(resources), "start": start, "end": end,
              "checks": checks, "queries": len(checks), "empty": [x for x in checks if x.get("points") == 0]}
    save(root, "audit-after.json" if published else "audit-candidate.json", report)
    print(json.dumps({"passed": report["passed"], "queries": len(checks), "empty": len(report["empty"]),
                      "errors": [x for x in checks if "error" in x]}), flush=True)
    return report


def apply(resources, root):
    assert json.loads((root / "semantics.json").read_text())["passed"], "Semantic checks required"
    audit_report = json.loads((root / "audit-candidate.json").read_text())
    assert audit_report["passed"], "Candidate audit required"
    assert audit_report["resource_sha256"] == digest(resources), "Candidate changed after audit"
    before = json.loads((root / "before.json").read_text())
    assert snapshot() == before, "Concurrent resource edit detected before publication"
    assert fingerprint() == json.loads((root / "services-before.json").read_text()), "Service changed"
    existing = flattened(before)
    mutations = []
    save(root, "candidate.json", resources)
    save(root, "journal.json", mutations)
    def publish(document):
        identity = key(document)
        old = existing.get(identity)
        url = endpoint(document["kind"], document)
        if old:
            assert http(url + "/" + identity[-1]) == old, "Concurrent edit: " + str(identity)
            if spec(old) == spec(document):
                return
            payload = copy.deepcopy(old)
            payload["spec"] = document["spec"]
            # Journal before each request so an uncertain response can be reconciled.
            mutations.append({"action": "update", "before": old, "candidate": document})
            save(root, "journal.json", mutations)
            http(url + "/" + identity[-1], "PUT", payload)
        else:
            mutations.append({"action": "create", "candidate": document})
            save(root, "journal.json", mutations)
            http(url, "POST", document)
        actual = http(url + "/" + identity[-1])
        assert spec(actual) == spec(document), "Readback differs: " + str(identity)
        mutations[-1]["after"] = actual
        save(root, "journal.json", mutations)
        existing[identity] = actual
        print("Published", identity, flush=True)
    try:
        # A3 first; old DCU/A3 resources remain available until the target works.
        for project in ("a3-monitoring", "dcu-monitoring", "xpu-monitoring"):
            for category in ("projects", "datasources", "dashboards"):
                for d in resources[category]:
                    owner = d["metadata"]["name"] if category == "projects" else d["metadata"]["project"]
                    if owner == project:
                        publish(d)
            if project == "a3-monitoring":
                for d in resources["dashboards"]:
                    if d["metadata"]["project"] == project:
                        assert spec(http(endpoint("Dashboard", d) + "/" + d["metadata"]["name"])) == spec(d)
                probe = 'up{environment="a3-vllm"}'
                at = int(time.time() // 60) * 60 - 180
                a = query(VM, probe, at - 300, at, 15)
                b = query(BASE + "/proxy/projects/a3-monitoring/datasources/victoriametrics", probe, at - 300, at, 15)
                assert a == b and a, "A3 datasource acceptance failed"
        # A full post-publication query audit gates removal of migrated sources.
        assert audit(resources, root, published=True)["passed"]
        from dashboard_reorg import RETIRED
        candidates = flattened(resources)
        retired = copy.deepcopy(RETIRED)
        if ('Dashboard', 'a3-monitoring', 'backend-prefill') in candidates:
            retired['a3-monitoring'] += ('backend-diagnostics',)
        for project, names in retired.items():
            for name in names:
                identity = ('Dashboard', project, name)
                assert identity not in candidates, 'Retired resource still in candidate'
                if identity not in existing:
                    continue
                old = existing[identity]
                url = endpoint('Dashboard', old) + '/' + name
                assert http(url) == old, 'Concurrent edit before source removal'
                mutations.append({'action': 'delete', 'before': old})
                save(root, 'journal.json', mutations)
                http(url, 'DELETE')
        after = snapshot()
        expected = flattened(resources)
        actual = flattened(after)
        assert set(expected) == set(actual), (set(expected) ^ set(actual))
        for identity, d in expected.items():
            assert spec(actual[identity]) == spec(d), identity
        assert fingerprint() == json.loads((root / "services-before.json").read_text())
        save(root, "after.json", after)
        save(root, "publication.json", {"passed": True, "projects": len(resources['projects']), "dashboards": len(resources['dashboards']),
             "panels": sum(len(d["spec"]["panels"]) for d in resources["dashboards"]),
             "services_unchanged": True, "completed_at": time.time()})
        print("Publication accepted", flush=True)
    except Exception as error:
        error.add_note('Automatic rollback is disabled; preserve current state and fix forward.')
        raise


def rollback(root):
    journal = json.loads((root / "journal.json").read_text())
    for entry in reversed(journal):
        d = entry.get("candidate", entry.get("before"))
        url = endpoint(d["kind"], d) + "/" + d["metadata"]["name"]
        try:
            current = http(url)
        except RuntimeError as e:
            if "HTTP 404" not in str(e):
                raise
            current = None
        if entry["action"] == "create":
            if current is not None:
                assert spec(current) == spec(d), "Concurrent edit; keep created resource"
                if d["kind"] == "Project":
                    for child in ("dashboards", "datasources", "variables", "secrets"):
                        assert not http(url + "/" + child), "Project contains resources created after this release; keep it"
                http(url, "DELETE")
        elif entry["action"] == "update":
            if current is not None and spec(current) == spec(entry["before"]):
                continue
            assert current is not None and spec(current) == spec(d), "Concurrent edit; do not overwrite"
            current["spec"] = entry["before"]["spec"]
            http(url, "PUT", current)
        elif current is None:
            old = copy.deepcopy(entry["before"])
            old["metadata"] = {k: v for k, v in old["metadata"].items() if k in ("name", "project")}
            http(endpoint(old["kind"], old), "POST", old)
        else:
            assert spec(current) == spec(entry["before"]), "Concurrent source recreation"
    save(root, "rollback.json", {"passed": True, "at": time.time()})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "audit", "audit-published", "apply", "rollback"))
    parser.add_argument("--resources", type=Path, default=Path(__file__).parent / "projects")
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    assert args.evidence.is_dir(), "Announce and create evidence directory first"
    if args.action == "prepare":
        assert not (args.evidence / "before.json").exists(), "Use a fresh evidence directory"
        save(args.evidence, "before.json", snapshot())
        save(args.evidence, "services-before.json", fingerprint())
        return
    if args.action == "rollback":
        rollback(args.evidence)
        return
    resources = read_resources(args.resources)
    validate(resources)
    if args.action.startswith("audit"):
        assert audit(resources, args.evidence, args.action == "audit-published")["passed"]
    else:
        apply(resources, args.evidence)


if __name__ == "__main__":
    main()
