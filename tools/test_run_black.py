# Copyright © Bruce Perens K6BP. All Rights Reserved. This software is proprietary and confidential.
"""run_black.sh must work when VENV_PYTHON is not set (it called an empty command name)."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("run_black.sh")


class RunBlackInterpreterTest(unittest.TestCase):
    def test_unset_venv_python_falls_back_to_python3(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d, "calls")
            stub = Path(d, "python3")
            stub.write_text('#!/bin/sh\necho "$1 $2" >> "%s"\n' % log)
            stub.chmod(0o755)
            env = {"PATH": d + os.pathsep + "/usr/bin:/bin", "HOME": d}
            result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("-m black", log.read_text())


if __name__ == "__main__":
    unittest.main()
