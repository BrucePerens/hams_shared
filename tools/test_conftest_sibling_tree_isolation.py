# SPDX-License-Identifier: AGPL-3.0-or-later
"""The tools conftest hides `HAMS_COM_TREE` / `HAMS_OPEN_TREE` from every test (burn-ignore-test-tags)."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))

PROBE = (
    "import os\n"
    "def test_the_override_is_hidden():\n"
    "    assert 'HAMS_OPEN_TREE' not in os.environ and 'HAMS_COM_TREE' not in os.environ\n"
    "def test_a_test_may_set_it_itself(monkeypatch):\n"
    "    monkeypatch.setenv('HAMS_OPEN_TREE', '/x')\n"
    "    assert os.environ['HAMS_OPEN_TREE'] == '/x'\n"
)


class SiblingTreeIsolationTests(unittest.TestCase):
    def test_exported_tree_variables_do_not_reach_a_test(self):
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copy(os.path.join(HERE, "conftest.py"), tmp)
            with open(os.path.join(tmp, "test_probe.py"), "w", encoding="utf-8") as f:
                f.write(PROBE)
            env = dict(os.environ, HAMS_OPEN_TREE="/somewhere/hams_open", HAMS_COM_TREE="/somewhere/hams_com")
            run = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_probe.py"],
                cwd=tmp, env=env, capture_output=True, text=True, timeout=120,
            )
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("2 passed", run.stdout)


if __name__ == "__main__":
    unittest.main()
