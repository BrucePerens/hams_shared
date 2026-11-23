#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""test.py's no-output limit scales with the machine's load (round 191 flakes)."""
import importlib.util
import os
import unittest
from unittest.mock import patch

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test.py")


def _load():
    spec = importlib.util.spec_from_file_location("_hams_test_runner_idle_limit", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestIdleLimit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load()

    def test_idle_box_keeps_sixty_seconds(self):
        # Tests [@ANCHOR: test:idle_limit_seconds]
        with patch("os.getloadavg", return_value=(3.0, 0, 0)), patch("os.cpu_count", return_value=32):
            self.assertEqual(self.mod.idle_limit_seconds(), 60.0)

    def test_busy_box_waits_longer(self):
        # Tests [@ANCHOR: test:idle_limit_seconds]
        with patch("os.getloadavg", return_value=(75.0, 0, 0)), patch("os.cpu_count", return_value=32):
            self.assertEqual(self.mod.idle_limit_seconds(), 180.0)


if __name__ == "__main__":
    unittest.main()
