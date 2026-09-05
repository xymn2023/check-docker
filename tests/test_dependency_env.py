from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import dependency_env


class DependencyEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.lock = self.root / 'requirements.lock'
        self.lock.write_text('example-package==1.2.3\n')
        self.candidate = self.root / 'existing'
        (self.candidate / 'bin').mkdir(parents=True)
        (self.candidate / 'bin' / 'python').write_text('placeholder')
        self.target = self.root / 'release' / 'venv'
        self.cache = self.root / 'cache'

    def test_matching_existing_environment_skips_all_install_commands(self):
        with patch('dependency_env.valid', return_value=True), \
             patch('dependency_env.subprocess.run') as run:
            message = dependency_env.prepare(self.lock, self.target, self.cache, [self.candidate])
        run.assert_not_called()
        self.assertTrue(self.target.is_symlink())
        self.assertEqual(self.target.resolve(), self.candidate.resolve())
        self.assertIn('复用现有环境', message)

    def test_matching_cache_is_reused_when_previous_environment_is_invalid(self):
        cached = self.cache / dependency_env.fingerprint(self.lock)
        (cached / 'bin').mkdir(parents=True)
        (cached / 'bin' / 'python').write_text('placeholder')
        def validity(path, lock):
            return path == cached
        with patch('dependency_env.valid', side_effect=validity), \
             patch('dependency_env.subprocess.run') as run:
            message = dependency_env.prepare(self.lock, self.target, self.cache, [self.candidate])
        run.assert_not_called()
        self.assertEqual(self.target.resolve(), cached.resolve())
        self.assertIn('复用缓存环境', message)

    def test_dependency_or_python_identity_changes_cache_key(self):
        first = dependency_env.fingerprint(self.lock)
        self.lock.write_text('example-package==1.2.4\n')
        self.assertNotEqual(first, dependency_env.fingerprint(self.lock))


if __name__ == '__main__':
    unittest.main()
