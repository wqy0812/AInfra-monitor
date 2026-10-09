"""Stable release entry point. Runs locally on the target host; never uses SSH."""
import argparse
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
COMMANDS = {
    'container': ('deploy/replace.py', 'Replace an owned container in a maintenance window'),
    'dashboards': ('perses/project_release.py', 'Prepare, publish or audit project resources'),
    'image': ('perses/performance/image_release.py', 'Load or publish a pinned Perses image'),
    'runtime': ('perses/reorg_runtime.py', 'Install generator files with a concurrency journal'),
    'acceleration': ('deploy/perses_acceleration/materialized_release.py', 'Admit and publish materialized-query groups'),
    'acceleration-ready': ('deploy/perses_acceleration/api_readiness.py', 'Check API acceleration startup after container replacement'),
    'merges': ('deploy/perses_acceleration/merge_release.py', 'Validate and publish query merges'),
    'queries': ('deploy/perses_acceleration/query_release.py', 'Audit and publish scoped query rewrites'),
}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest='component', required=True)
    for name, (_, description) in COMMANDS.items():
        subparsers.add_parser(name, help=description, add_help=False)
    args, remaining = parser.parse_known_args(argv)
    # Keep each component's validation and exit status; no shell or side effects
    # in the dispatcher. `release.py <component> --help` shows its real options.
    return subprocess.call([sys.executable, str(ROOT / COMMANDS[args.component][0]), *remaining])


if __name__ == '__main__':
    raise SystemExit(main())
