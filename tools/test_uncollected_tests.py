#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""uncollected_tests.find_uncollected: every test_*.py is either collected by a dedicated step, an Odoo module test, baselined, or reported to run."""
import os
import tempfile
import unittest

import uncollected_tests


def _touch(root, rel):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("")


class FindUncollectedTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name

    def test_new_suite_in_unknown_directory_is_reported(self):
        _touch(self.root, "pager_duty/daemon/test_main.py")
        run, baselined = uncollected_tests.find_uncollected(self.root)
        self.assertEqual(run, {"pager_duty/daemon": ["test_main.py"]})
        self.assertEqual(baselined, {})

    def test_directories_other_steps_collect_are_not_reported(self):
        for rel in (
            "tools/test_a.py", "scripts/test_a.py", "ingest/test_a.py", "devbox_tools/test_a.py", "daemons/test_a.py",
            "daemons/d/test_a.py", "daemons/d/tools/test_a.py", "daemons/d/tests/test_a.py", "daemons/d/sub/tests/test_a.py",
            "hams_shared/tools/test_a.py", "hams_shared/scripts/test_a.py",
        ):
            _touch(self.root, rel)
        self.assertEqual(uncollected_tests.find_uncollected(self.root), ({}, {}))

    def test_odoo_module_tests_are_not_reported(self):
        _touch(self.root, "ham_x/__manifest__.py")
        _touch(self.root, "ham_x/tests/test_a.py")
        _touch(self.root, "ham_x/tools/test_b.py")
        run, _baselined = uncollected_tests.find_uncollected(self.root)
        self.assertEqual(run, {"ham_x/tools": ["test_b.py"]})

    def test_deeper_directory_under_a_collected_one_is_reported(self):
        _touch(self.root, "devbox_tools/overnight/test_driver.py")
        run, _baselined = uncollected_tests.find_uncollected(self.root)
        self.assertEqual(run, {"devbox_tools/overnight": ["test_driver.py"]})

    def test_baselined_directory_is_listed_with_its_reason(self):
        _touch(self.root, "hardware/test_generate_netlist.py")
        run, baselined = uncollected_tests.find_uncollected(self.root)
        self.assertEqual((run, baselined), ({}, {"hardware": ["test_generate_netlist.py"]}))

    def test_every_baseline_entry_has_a_reason(self):
        for directory, reason in uncollected_tests.BASELINE.items():
            self.assertTrue(reason.strip(), directory)


if __name__ == "__main__":
    unittest.main()
