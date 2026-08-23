#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit test for run_linters.py's step-27 test-suite discovery glob.

run_linters.py's main() is one large, non-decomposed, side-effecting
function -- it hardcodes dir_path from its own real location, runs
~27 real linter/subprocess steps against the real repo tree, and has
no way to be pointed at a fixture. Exercising the whole orchestrator
per test run is neither isolated nor fast, so this test extracts and
execs just the two-statement discovery snippet (the exact source lines,
not a re-implementation of them) against a controlled fixture dir_path,
to lock in the specific regression this step exists to prevent: a
future edit narrowing the glob back to `test_check_*.py` (which is
exactly how the "nothing runs these tests" bug happened the first
time), or removing a runner script from the exclusion set so pytest
tries to collect test.py/test_cf.py/test_mcp_server.py and executes
their module-level Odoo-launching code instead of running unit tests.
"""

import glob
import os
import re
import shutil
import tempfile
import textwrap
import unittest

_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_linters.py")


def _extract_discovery_snippet():
    with open(_SCRIPT, encoding="utf-8") as f:
        source = f.read()
    match = re.search(
        r"([ \t]*_RUNNER_SCRIPTS_NOT_SUITES = \{.*?\n[ \t]*\)\n)",
        source,
        re.DOTALL,
    )
    if not match:
        raise AssertionError(
            "Could not locate the test-suite discovery snippet in run_linters.py -- "
            "its source shape changed; update this test's extraction regex."
        )
    return textwrap.dedent(match.group(1))


def _write(path, content=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


class DiscoverySnippetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.snippet = _extract_discovery_snippet()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_snippet(self):
        namespace = {"glob": glob, "os": os, "dir_path": self.tmp}
        exec(self.snippet, namespace)  # noqa: S102 -- real source, not user input
        return namespace["tool_test_files"]

    def test_a_real_test_suite_file_is_collected(self):
        _write(os.path.join(self.tmp, "tools", "test_check_dependency_cycles.py"))
        self.assertEqual(
            self._run_snippet(),
            [os.path.join(self.tmp, "tools", "test_check_dependency_cycles.py")],
        )

    def test_a_suite_not_named_after_a_check_script_is_still_collected(self):
        # The exact regression this step's own commit message documents:
        # a `test_check_*` glob would silently skip a suite like this one.
        _write(os.path.join(self.tmp, "tools", "test_run_linters.py"))
        self.assertEqual(
            self._run_snippet(),
            [os.path.join(self.tmp, "tools", "test_run_linters.py")],
        )

    def test_test_py_the_odoo_runner_script_is_excluded(self):
        _write(os.path.join(self.tmp, "tools", "test.py"))
        self.assertEqual(self._run_snippet(), [])

    def test_test_cf_py_the_odoo_runner_script_is_excluded(self):
        _write(os.path.join(self.tmp, "tools", "test_cf.py"))
        self.assertEqual(self._run_snippet(), [])

    def test_test_mcp_server_py_is_excluded(self):
        _write(os.path.join(self.tmp, "tools", "test_mcp_server.py"))
        self.assertEqual(self._run_snippet(), [])

    def test_a_mix_of_runner_scripts_and_real_suites_keeps_only_the_suites_sorted(self):
        _write(os.path.join(self.tmp, "tools", "test.py"))
        _write(os.path.join(self.tmp, "tools", "test_cf.py"))
        _write(os.path.join(self.tmp, "tools", "test_mcp_server.py"))
        _write(os.path.join(self.tmp, "tools", "test_zzz_last.py"))
        _write(os.path.join(self.tmp, "tools", "test_aaa_first.py"))
        result = self._run_snippet()
        self.assertEqual(
            result,
            [
                os.path.join(self.tmp, "tools", "test_aaa_first.py"),
                os.path.join(self.tmp, "tools", "test_zzz_last.py"),
            ],
        )

    def test_no_matching_files_yields_an_empty_list(self):
        os.makedirs(os.path.join(self.tmp, "tools"))
        self.assertEqual(self._run_snippet(), [])


if __name__ == "__main__":
    unittest.main()
