import concurrent.futures
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[1] / "deploy/perses_acceleration"))
from admin import update


def test_concurrent_invalidation_and_group_disable_preserve_both_changes(tmp_path):
    path = tmp_path / 'admin.json'
    def invalidate(i):
        update(path, 'invalidate', start=i, end=i+1, reason='source correction')
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        pending = [pool.submit(invalidate, i) for i in range(20)]
        pending.append(pool.submit(update, path, 'disable', group='cpu'))
        for future in pending: future.result()
    result = json.loads(path.read_text())
    assert sorted(x['start'] for x in result['invalidated']) == list(range(20))
    assert result['disabled_groups'] == ['cpu']
