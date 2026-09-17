"""Apply description-only refinements and allow negative clock offsets."""
import copy
import json
import sys
import time
from pathlib import Path
from project_release import BASE, endpoint, fingerprint, flattened, http, save, snapshot, spec
from project_split import read_resources, validate


def content(d):
    d = copy.deepcopy(d)
    for p in d["spec"]["panels"].values():
        p["spec"]["display"].pop("description", None)
        if d["metadata"]["name"] == "gateway-requests":
            for q in p["spec"]["queries"]:
                s = q["spec"]["plugin"]["spec"]
                legend = s.get("seriesNameFormat", "")
                s["seriesNameFormat"] = legend[len("后端 "):] if legend.startswith("后端 ") else legend
    if d["metadata"]["name"] == "a3-hosts":
        d["spec"]["panels"]["extra-clock-offset"]["spec"]["plugin"]["spec"]["yAxis"].pop("min", None)
    return spec(d)


def main():
    root = Path(sys.argv[1])
    resources = read_resources(root / "projects")
    validate(resources)
    before = snapshot()
    save(root, "polish-before-" + str(time.time_ns()) + ".json", before)
    old = flattened(before)
    journal = json.loads((root / "journal.json").read_text())
    changed = []
    for d in resources["dashboards"]:
        identity = ("Dashboard", d["metadata"]["project"], d["metadata"]["name"])
        current = old[identity]
        assert content(current) == content(d), "Refinement changes query or layout"
        if spec(current) == spec(d):
            continue
        url = endpoint("Dashboard", d) + "/" + d["metadata"]["name"]
        assert http(url) == current, "Concurrent edit"
        journal.append({"action": "update", "before": current, "candidate": d})
        save(root, "journal.json", journal)
        candidate = copy.deepcopy(current)
        candidate["spec"] = d["spec"]
        http(url, "PUT", candidate)
        actual = http(url)
        assert spec(actual) == spec(d)
        journal[-1]["after"] = actual
        save(root, "journal.json", journal)
        changed.append(identity)
    assert fingerprint() == json.loads((root / "services-before.json").read_text())
    save(root, "after.json", snapshot())
    save(root, "candidate-final.json", resources)
    save(root, "polish.json", {"passed": True, "changed": changed, "queries_layouts_unchanged": True, "at": time.time()})
    print(json.dumps({"passed": True, "changed": changed}))


if __name__ == "__main__":
    main()
