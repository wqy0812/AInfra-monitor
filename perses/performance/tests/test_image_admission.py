"""Maintenance-window image publication needs no candidate or soak reports."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import image_release


def test_apply_without_candidate_reports_uses_locked_image_and_post_upgrade_checks(tmp_path, monkeypatch):
    (tmp_path / 'release-lock.json').write_text(json.dumps({
        'candidate_version': '0.54.0-perf.3', 'candidate_config_digest': 'new',
        'previous_version': '0.54.0-perf.2', 'previous_image_digest': 'old'}))
    monkeypatch.setattr(sys, 'argv', ['image_release.py', 'apply', '--evidence', str(tmp_path)])
    observed = []
    monkeypatch.setattr(image_release, 'apply_image', lambda *args: observed.append(args))
    image_release.main()
    root, image, version, previous, validation = observed[0]
    assert (root, image, version, previous) == (tmp_path, 'new', '0.54.0-perf.3', 'old')
    assert validation['mode'] == 'maintenance-window'
    assert validation['post_upgrade_checks'] == ['version', 'resources']
    assert 'candidate-1800-second-soak' in validation['checks_not_run']
    assert 'passed' not in validation
