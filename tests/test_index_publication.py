"""A stale source snapshot cannot overwrite a newer shared index."""
from pathlib import Path
import fcntl
import io
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/build_probe_index.py'


class IndexPublication(unittest.TestCase):
    def test_source_change_refuses_old_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'FAILURE_MODES.yaml').write_text('- id: one\n  fix_pattern: Original.\n  probe_when: ["`specific-token`"]\n')
            (root/'PROCESS_FAILURES.yaml').write_text('[]\n')
            target = root/'index.json'
            with patch.dict(os.environ, RECALL_CATALOG_DIR=str(root), RECALL_PROBE_INDEX=str(target)):
                spec = importlib.util.spec_from_file_location('fixture_index', SCRIPT)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            old = module.build()
            (root/'PROCESS_FAILURES.yaml').write_text('- id: new\n  fix_pattern: Revised.\n  probe_when: ["`second-token`"]\n')
            target.write_text('{"last_good": true}')
            with self.assertRaises(module.CatalogError):
                module.write_index(old)
            self.assertEqual({'last_good':True}, json.loads(target.read_text()))

    def test_lost_race_exits_with_one_line_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'FAILURE_MODES.yaml').write_text('- id: one\n  fix_pattern: Original.\n  probe_when: ["`specific-token`"]\n')
            (root/'PROCESS_FAILURES.yaml').write_text('[]\n')
            target = root/'index.json'
            with patch.dict(os.environ, RECALL_CATALOG_DIR=str(root), RECALL_PROBE_INDEX=str(target)):
                spec = importlib.util.spec_from_file_location('fixture_index_race', SCRIPT)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            old = module.build()
            (root/'PROCESS_FAILURES.yaml').write_text('- id: new\n  fix_pattern: Revised.\n  probe_when: ["`second-token`"]\n')
            target.write_text('{"last_good": true}')
            err = io.StringIO()
            with patch.object(module, 'build', return_value=old), patch.object(sys, 'stderr', err):
                code = module.main()
            self.assertNotEqual(0, code)
            self.assertEqual(1, len(err.getvalue().strip().splitlines()), err.getvalue())
            self.assertIn('catalog changed during index build', err.getvalue())
            self.assertEqual({'last_good':True}, json.loads(target.read_text()))
            # A lost race is not a broken catalog, so it does not raise the error marker.
            self.assertFalse(Path(str(target)+'.error').exists())

    def test_publication_waits_for_shared_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ('FAILURE_MODES','PROCESS_FAILURES'):
                (root/(name+'.yaml')).write_text('[]\n')
            target = root/'index.json'
            target.write_text('{"last_good": true}')
            with Path(str(target)+'.build.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                child = subprocess.Popen([sys.executable,str(SCRIPT)],
                    env={**os.environ,'RECALL_CATALOG_DIR':str(root),'RECALL_PROBE_INDEX':str(target),
                         'PYTHONDONTWRITEBYTECODE':'1'}, stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                try:
                    with self.assertRaises(subprocess.TimeoutExpired):
                        child.wait(timeout=.5)
                    self.assertEqual({'last_good':True},json.loads(target.read_text()))
                finally:
                    fcntl.flock(lock,fcntl.LOCK_UN)
                    stdout,stderr=child.communicate(timeout=5)
            self.assertEqual(0,child.returncode,stderr)
            self.assertIn('entries',json.loads(target.read_text()))


if __name__=='__main__':
    unittest.main()
