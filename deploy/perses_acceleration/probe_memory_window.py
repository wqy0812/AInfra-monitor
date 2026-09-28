"""Real-environment correctness probe for the DCU window that hit VM memory limits."""
import argparse
import json
import time
from pathlib import Path

import materialized_release as release


def main(root):
    catalog = json.loads(release.CATALOG.read_text())
    panel = next(p for p in catalog['panels'] if p['group'] == 'dcu')
    step = 5
    job = next(j for j in release.health()['perses_acceleration']['jobs']
               if j['job'] == panel['id'] + ':' + panel['revision'] + ':' + str(step))
    end = int(job['processed_at']); start = end - 43200
    counts = release.coverage(panel, step, start, end)
    actual, detail = release.compare_correctness_window(panel, release.expression(panel, step), start, end, step)
    filled = {t: 0 for t in counts}; filled.update(actual)
    assert filled == counts, 'Completion counts disagree'
    release.save(root, 'memory-window-comparison.json', {'passed': True, 'at': time.time(), 'panel': panel['id'],
                 'start': start, 'end': end, 'step': step, 'grid_points': len(counts),
                 'performance_admission': False, 'comparison': detail})
    print('DCU 12h/5s correctness passed:', detail['mode'], 'chunks:', detail['chunks'], flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args(); assert args.evidence.is_dir()
    main(args.evidence)
