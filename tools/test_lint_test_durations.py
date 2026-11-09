#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for lint_test_durations: the split is balanced by measured time, loses no file, repeats exactly, and
estimates a new file from its size."""

import json
import os
import tempfile
import unittest

import lint_test_durations as ltd


class BalancedChunksTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def make(self, name, size):
        path = os.path.join(self._tmp.name, name)
        with open(path, "w") as handle:
            handle.write("x" * size)
        return path

    def test_every_file_is_in_exactly_one_chunk(self):
        files = [self.make(f"test_{i}.py", 10 + i) for i in range(11)]
        chunks = ltd.balanced_chunks(files, 4, {})
        self.assertEqual(sorted(sum(chunks, [])), sorted(files))

    def test_measured_time_beats_file_size(self):
        big_but_fast = self.make("test_big_fast.py", 100_000)
        small_but_slow = self.make("test_small_slow.py", 100)
        others = [self.make(f"test_o{i}.py", 1000) for i in range(2)]
        table = {"test_big_fast.py": 1.0, "test_small_slow.py": 300.0, "test_o0.py": 100.0, "test_o1.py": 100.0}
        chunks = ltd.balanced_chunks([big_but_fast, small_but_slow] + others, 2, table)
        seconds = ltd.estimate([big_but_fast, small_but_slow] + others, table)
        loads = sorted(sum(seconds[f] for f in chunk) for chunk in chunks)
        self.assertEqual(loads, [201.0, 300.0])  # slow alone against the two others plus the fast one

    def test_deterministic(self):
        files = [self.make(f"test_{i}.py", 50 + 7 * i) for i in range(9)]
        table = {os.path.basename(f): float(i) for i, f in enumerate(files)}
        self.assertEqual(ltd.balanced_chunks(files, 3, table), ltd.balanced_chunks(list(reversed(files)), 3, table))

    def test_unmeasured_file_is_estimated_from_its_size(self):
        known = self.make("test_known.py", 1000)
        new = self.make("test_new.py", 3000)
        seconds = ltd.estimate([known, new], {"test_known.py": 10.0})
        self.assertAlmostEqual(seconds[new], 30.0)

    def test_refresh_sums_the_tests_of_a_file(self):
        xml = os.path.join(self._tmp.name, "j.xml")
        with open(xml, "w") as handle:
            handle.write('<testsuites><testsuite>'
                         '<testcase classname="test_a" name="t1" time="1.5"/>'
                         '<testcase classname="tools.test_a.TestX" name="t2" time="2.0"/>'
                         '<testcase classname="test_b" name="t" time="0.25"/></testsuite></testsuites>')
        table = os.path.join(self._tmp.name, "t.json")
        ltd.refresh(xml, table)
        self.assertEqual(ltd.load_table(table), {"test_a.py": 3.5, "test_b.py": 0.25})

    def test_shipped_table_names_real_files(self):
        table = ltd.load_table()
        existing = {os.path.basename(f) for f in os.listdir(ltd.HERE) if f.startswith("test_")}
        self.assertTrue(set(table) <= existing | {"test.py"}, sorted(set(table) - existing))


class DaemonShardsTest(unittest.TestCase):
    """A daemon test directory listed in the table runs as several balanced sessions that lose and repeat no file."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.table = os.path.join(self._tmp.name, "table.json")
        with open(self.table, "w") as handle:
            json.dump({"seconds": {}, "daemon_shards": {"daemons/d/tests": {
                "count": 3, "seconds": {"test_a.py": 100, "test_b.py": 90, "test_c.py": 10, "test_d.py": 10}}}}, handle)
        self.files = [f"/r/daemons/d/tests/test_{c}.py" for c in "abcdef"]

    def shards(self, rel="daemons/d/tests", **kw):
        return ltd.daemon_shards(rel, self.files, path=self.table, **kw)

    def test_a_listed_directory_is_split_into_count_sessions_with_every_file_once(self):
        sessions = self.shards()
        self.assertEqual(len(sessions), 3)
        self.assertEqual(sorted(sum(sessions, [])), sorted(self.files))

    def test_the_slow_files_are_in_different_sessions(self):
        sessions = self.shards()
        a = next(s for s in sessions if "/r/daemons/d/tests/test_a.py" in s)
        self.assertNotIn("/r/daemons/d/tests/test_b.py", a)

    def test_the_split_repeats_exactly(self):
        self.assertEqual(self.shards(), self.shards())

    def test_an_unlisted_directory_a_disabled_split_and_a_single_file_stay_one_session(self):
        self.assertEqual(self.shards("daemons/other/tests"), [self.files])
        self.assertEqual(self.shards(disabled=True), [self.files])
        self.assertEqual(ltd.daemon_shards("daemons/d/tests", self.files[:1], path=self.table), [self.files[:1]])

    def test_a_missing_or_broken_table_is_one_session(self):
        self.assertEqual(ltd.daemon_shards("daemons/d/tests", self.files, path=os.path.join(self._tmp.name, "none.json")), [self.files])

    def test_never_more_sessions_than_files(self):
        self.assertEqual(len(ltd.daemon_shards("daemons/d/tests", self.files[:2], path=self.table)), 2)

    def test_refresh_keeps_the_daemon_section(self):
        junit = os.path.join(self._tmp.name, "j.xml")
        with open(junit, "w") as handle:
            handle.write('<testsuites><testsuite><testcase classname="test_x" name="t" time="1.5"/></testsuite></testsuites>')
        ltd.refresh(junit, self.table)
        with open(self.table) as handle:
            data = json.load(handle)
        self.assertIn("daemons/d/tests", data["daemon_shards"])
        self.assertEqual(data["seconds"], {"test_x.py": 1.5})

    def test_the_shipped_table_lists_the_simulated_bots_tests(self):
        entry = ltd.json.load(open(ltd.TABLE))["daemon_shards"]["daemons/hams_simulated_bots/tests"]
        self.assertGreaterEqual(entry["count"], 2)


class MypyPluginStepTest(unittest.TestCase):
    """test_odoo_mypy_plugin.py (10 cold mypy runs of 22-61 s) has a step of its own, spread over workers by test, and is
    in no chunk; HAMS_LINT_MYPY_PLUGIN_SPLIT=0 puts it back in the chunks."""

    def steps(self, **env):
        import subprocess
        import sys

        # `--list-json` from a hams_shared tree first builds the sibling layout (clones, symlinks): seconds on a quiet box, and above the old
        # 120 s on a saturated one (2026-11-08, load above 90), where the test failed with a TimeoutExpired though nothing was wrong. The
        # limit is only a safety net against a hang now.
        run = subprocess.run([sys.executable, os.path.join(ltd.HERE, "run_linters.py"), "--list-json"], capture_output=True,
                             text=True, env={**os.environ, **env}, timeout=900)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        return {step["name"]: step["argv"] for step in json.loads(run.stdout)}

    def test_the_file_has_its_own_step_with_one_test_per_worker_dispatch_and_is_in_no_chunk(self):
        steps = self.steps()
        if "pytest tools/[mypy-plugin]" not in steps:
            self.skipTest("this tree has no tools/test_odoo_mypy_plugin.py")
        argv = steps["pytest tools/[mypy-plugin]"]
        self.assertTrue(any(a.endswith("test_odoo_mypy_plugin.py") for a in argv))
        if "-n" in argv:  # only when the interpreter has pytest-xdist
            self.assertEqual(argv[argv.index("--dist") + 1], "load")
        for name, other in steps.items():
            if name.startswith("pytest tools/[") and name != "pytest tools/[mypy-plugin]":
                self.assertFalse(any(a.endswith("test_odoo_mypy_plugin.py") for a in other), name)

    def test_the_switch_restores_the_old_chunks(self):
        steps = self.steps(HAMS_LINT_MYPY_PLUGIN_SPLIT="0")
        self.assertNotIn("pytest tools/[mypy-plugin]", steps)
        self.assertTrue(any(a.endswith("test_odoo_mypy_plugin.py") for argv in steps.values() for a in (argv or [])))


if __name__ == "__main__":
    unittest.main()
