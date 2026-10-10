"""Public scripts preserve write/load isolation without real deployment hosts."""
import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('submission_test',Path(__file__).resolve().parents[1]/'scripts/submission_test.py')
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class IsolatedTargetSafety(unittest.TestCase):
    def test_loopback_targets_allowed(self):
        for base in ('http://127.0.0.1:8000','http://localhost:8000','http://[::1]:8000'):
            module.require_local_isolated_target(base)

    def test_remote_targets_refused(self):
        for base in ('https://example.org','http://192.168.1.2:8000','http://localhost.example.org','file:///tmp/test'):
            with self.assertRaises(SystemExit):module.require_local_isolated_target(base)

if __name__=='__main__':unittest.main()
