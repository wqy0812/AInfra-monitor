"""Check acceleration startup after the shared container release completes."""
import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'perses'))
from replace import inspect
from release_support import save, record_failure
from native_recording import fetch


def read_health():
    return fetch('http://127.0.0.1:18430','/health')


def validate_health(result):
    assert result['status']=='ok'
    assert result['metric_writer'] is False and result['perses_proxy'] is False
    assert result['metric_policy']['schema']=='native-1m-v1'
    for environment in ('dcu-pd','a3-vllm','xpu-pd'):
        item=result['environments'][environment]
        assert item['status']=='ok' and not item['error'] and 0<=time.time()-item['processed_at']<result['metric_policy']['snapshot_max_age_seconds']


def publication(root):
    path = root / 'container-publication.json'
    if path.exists():
        value = json.loads(path.read_text())
        assert value['container'] == 'monitoring-api' and value['acceptance'] == 'passed'
        return value
    # Read older evidence for interrupted operations; never recreate old releases.
    return json.loads((root / 'shadow-started.json').read_text())


def accepted(root):
    path = root / 'api-readiness.json'
    if not path.exists():
        path = root / 'shadow-observation.json'
    return json.loads(path.read_text())['passed']


def observe(root):
    expected = publication(root)
    deadline = time.monotonic() + 90
    bad = 0
    report = {'passed': False, 'mode': 'maintenance-window', 'started_at': time.time(),
              'image': expected['image'], 'container_id': expected['container_id'], 'records': []}
    try:
        save(root, 'api-readiness.json', report)
        while time.monotonic() < deadline:
            current = inspect('monitoring-api')
            assert current['Id'] == expected['container_id'] and current['Image'] == expected['image'], 'API changed during acceptance'
            try:
                result = read_health()
                validate_health(result)
                from native_recording import check
                check(expected=result['native_recording']['rule_count'])
                report.update(passed=True, ended_at=time.time())
                report['records'].append({'at': time.time(), 'health': result})
                save(root, 'api-readiness.json', report)
                return
            except (OSError, ValueError, KeyError, AssertionError, RuntimeError) as error:
                bad += 1
                report['records'].append({'at': time.time(), 'error': str(error)[:300]})
                if bad >= 3:
                    raise RuntimeError('Sustained acceleration startup failure') from error
            save(root, 'api-readiness.json', report)
            time.sleep(5)
        raise TimeoutError('Acceleration startup acceptance timed out')
    except BaseException as error:
        record_failure(root, 'api-readiness-failure.json', error)
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    assert args.evidence.is_dir()
    observe(args.evidence)
