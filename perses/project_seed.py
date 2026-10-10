"""Create missing project resources, preserving every existing resource."""
import argparse
from pathlib import Path
from project_release import endpoint, flattened, http, snapshot
from project_split import read_resources, validate
from dashboard_columns import apply as columns


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--resources", type=Path, default=Path(__file__).parent / "projects")
    args = parser.parse_args()
    resources = columns(read_resources(args.resources))
    validate(resources)
    existing = flattened(snapshot())
    for category in ("projects", "datasources", "dashboards"):
        for d in resources[category]:
            identity = (d["kind"], d["metadata"].get("project", ""), d["metadata"]["name"])
            if identity in existing:
                print("Preserved", identity)
            else:
                http(endpoint(d["kind"], d), "POST", d)
                print("Created", identity)


if __name__ == "__main__":
    main()
