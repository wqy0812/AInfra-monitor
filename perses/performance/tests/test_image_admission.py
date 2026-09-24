import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from image_release import validation_status


class ImageAdmissionTest(unittest.TestCase):
    def test_local_browser_requires_user_selection_and_both_evidence_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authorization = {'explicit_user_instruction': True, 'image': 'candidate',
                             'scope': 'deploy-perses-performance', 'validation_mode': 'local-browser',
                             'user_instruction': '浏览器验收本机就可以'}
            (root / 'deployment-authorization.json').write_text(json.dumps(authorization))
            with self.assertRaises(FileNotFoundError):
                validation_status(root, 'candidate', False, True)
            (root / 'local-browser-acceptance.json').write_text(json.dumps({
                'passed': True, 'environment': 'local-candidate', 'archive_config_digest': 'candidate'}))
            with self.assertRaises(FileNotFoundError):
                validation_status(root, 'candidate', False, True)
            (root / 'candidate-api-validation.json').write_text(json.dumps({'passed': True, 'image': 'candidate'}))
            result = validation_status(root, 'candidate', False, True)
            self.assertEqual(result['local_browser'], 'passed')
            self.assertIn('remote-candidate-1800-second-soak', result['checks_not_run'])
            with self.assertRaises(AssertionError):
                validation_status(root, 'different-image', False, True)
            with self.assertRaises(AssertionError):
                validation_status(root, 'candidate', True, True)

    def test_default_still_requires_candidate_browser_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileNotFoundError):
                validation_status(Path(directory), 'candidate', False)

    def test_authorization_preserves_deferred_checks_without_fake_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            authorization = {'explicit_user_instruction': True, 'image': 'candidate',
                             'scope': 'deploy-perses-performance', 'user_instruction': '上线吧'}
            (root / 'deployment-authorization.json').write_text(json.dumps(authorization))
            with self.assertRaises(FileNotFoundError):
                validation_status(root, 'candidate', True)
            (root / 'candidate-api-validation.json').write_text(json.dumps({'passed': True, 'image': 'candidate'}))
            status = validation_status(root, 'candidate', True)
            self.assertEqual(len(status['deferred_checks']), 2)
            self.assertNotIn('passed', status)
            with self.assertRaises(AssertionError):
                validation_status(root, 'different-image', True)


if __name__ == '__main__':
    unittest.main()
