#!/usr/bin/env python3
# Copyright © Bruce Perens K6BP. All Rights Reserved.
"""The `pytest devbox_tools/` lint step is told where the sibling hams_open checkout is."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import run_linters


class DevboxToolsEnvTests(unittest.TestCase):
    # Tests [@ANCHOR: run_linters:devbox_tools_env]
    def test_the_sibling_checkout_is_named_and_the_input_is_not_changed(self):
        with tempfile.TemporaryDirectory() as sibling:
            base = {"PATH": "/bin", "HAMS_OPEN_TREE": "/stale"}
            env = run_linters.devbox_tools_env(sibling, base)
            self.assertEqual(env["HAMS_OPEN_TREE"], os.path.realpath(sibling))
            self.assertEqual(env["PATH"], "/bin")
            self.assertEqual(base["HAMS_OPEN_TREE"], "/stale")

    # Tests [@ANCHOR: run_linters:devbox_tools_env]
    def test_no_sibling_leaves_the_environment_alone(self):
        self.assertEqual(run_linters.devbox_tools_env(None, {"A": "1"}), {"A": "1"})
        self.assertEqual(run_linters.devbox_tools_env("/no/such/dir", {"A": "1"}), {"A": "1"})


if __name__ == "__main__":
    unittest.main()
