"""Known recurrence writers must wait for a maintenance transaction."""
from pathlib import Path
import fcntl
import os
import subprocess
import sys
import tempfile
import unittest

import yaml

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/bump_recurrence.py'


class WriterLock(unittest.TestCase):
    def test_normalizer_waits_and_preserves_alias(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            catalog = root / 'catalog.yaml'
            original = '- id: fixture\n  probe_when:\n    - `example_symbol()`\n'
            catalog.write_text(original)
            alias = root / 'alias.yaml'
            alias.symlink_to(catalog)
            with Path(str(catalog) + '.maintenance.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                child = subprocess.Popen([sys.executable, str(SCRIPT.with_name('normalize_catalog_yaml.py')),
                                          str(alias), '--apply'],
                                         env={**os.environ, 'RECALL_HOME':str(root),
                                              'RECALL_AUTOCOMMIT':'0', 'PYTHONDONTWRITEBYTECODE':'1'},
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    with self.assertRaises(subprocess.TimeoutExpired):
                        child.wait(timeout=.5)
                    self.assertEqual(original, catalog.read_text())
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)
                    stdout, stderr = child.communicate(timeout=5)
            self.assertEqual(0, child.returncode, stderr)
            self.assertTrue(alias.is_symlink())
            self.assertEqual('`example_symbol()`', yaml.safe_load(catalog.read_text())[0]['probe_when'][0])

    def test_recurrence_waits_and_preserves_alias(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            catalog = root / 'catalog.yaml'
            original = '- id: fixture\n  recurrences: 0\n  fix_pattern: Preserve me.\n'
            catalog.write_text(original)
            alias = root / 'alias.yaml'
            alias.symlink_to(catalog)
            note = root / 'note.txt'
            note.write_text('Verified fixture recurrence.')
            with Path(str(catalog) + '.maintenance.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                child = subprocess.Popen([sys.executable, str(SCRIPT), '--session', 'abcdef123456',
                                          str(alias), 'fixture', str(note)],
                                         env={**os.environ, 'RECALL_HOME':str(root),
                                              'RECALL_AUTOCOMMIT':'0', 'PYTHONDONTWRITEBYTECODE':'1'},
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    with self.assertRaises(subprocess.TimeoutExpired):
                        child.wait(timeout=.5)
                    self.assertEqual(original, catalog.read_text())
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)
                    stdout, stderr = child.communicate(timeout=5)
            self.assertEqual(0, child.returncode, stderr)
            self.assertTrue(alias.is_symlink())
            entry = yaml.safe_load(catalog.read_text())[0]
            self.assertEqual(1, entry['recurrences'])
            self.assertEqual('Preserve me.', entry['fix_pattern'])


if __name__ == '__main__':
    unittest.main()
