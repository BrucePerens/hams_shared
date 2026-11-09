#!/usr/bin/env python3
# Copyright © Bruce Perens K6BP. All Rights Reserved.
"""Tests for overnight_common.py: the load verdict, the named-job runner (real agent_procs jobs, short commands), the targets file reader and the small helpers.

Tests [@ANCHOR: overnight_common:read_load]
Tests [@ANCHOR: overnight_common:run_job]
Tests [@ANCHOR: overnight_common:load_targets]
Tests [@ANCHOR: overnight_common:log_line]
Tests [@ANCHOR: overnight_common:write_json]
Tests [@ANCHOR: overnight_common:slug]
Tests [@ANCHOR: overnight_common:reports_dir]
Tests [@ANCHOR: overnight_common:Load]
"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import overnight_common as common  # noqa: E402

heavy = unittest.skipIf(os.environ.get("HAMS_SKIP_HEAVY"), "heavy: starts real agent_procs jobs")
NOW = 1_800_000_000.0


def write_stats(path, state="OK", running=2, slots=8, lint_running=1, age=1):
    with open(path, "w") as handle:
        json.dump({"written_epoch": NOW - age, "test_slots": slots, "queues": {"test_running": running, "lint_running": lint_running},
                   "dispatch": {"state": state, "reasons": ["why"]}}, handle)


class LoadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stats = os.path.join(self.tmp.name, "stats.json")
        self.meminfo = os.path.join(self.tmp.name, "meminfo")
        with open(self.meminfo, "w") as handle:
            handle.write("MemTotal: 100 kB\nMemAvailable: %d kB\n" % (32 * 1048576))

    def load(self, **kwargs):
        return common.read_load(self.stats, self.meminfo, now=lambda: NOW, lint_slots=4)

    def test_ok_load_reports_free_slots(self):
        write_stats(self.stats)
        load = self.load()
        self.assertIsNone(load.critical())
        self.assertEqual((load.test_free(), load.lint_free()), (6, 3))
        self.assertAlmostEqual(load.mem_available_gb, 32.0)

    def test_hold_is_critical_and_names_the_reason(self):
        write_stats(self.stats, state="HOLD")
        self.assertIn("load HOLD: why", self.load().critical())

    def test_missing_stats_is_hold(self):
        self.assertIn("HOLD", self.load().critical())

    def test_stale_stats_is_hold(self):
        write_stats(self.stats, age=600)
        self.assertIn("HOLD", self.load().critical())

    def test_low_memory_is_critical(self):
        write_stats(self.stats)
        with open(self.meminfo, "w") as handle:
            handle.write("MemAvailable: %d kB\n" % (2 * 1048576))
        self.assertIn("memory low", self.load().critical())

    def test_full_slots_leave_none_free_never_negative(self):
        write_stats(self.stats, running=12, slots=8, lint_running=9)
        load = self.load()
        self.assertEqual((load.test_free(), load.lint_free()), (0, 0))


class TargetsTests(unittest.TestCase):
    def write(self, text):
        handle = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        self.addCleanup(os.unlink, handle.name)
        handle.write(text)
        handle.close()
        return handle.name

    def test_reads_targets_expands_tilde_and_drops_disabled(self):
        path = self.write('[[target]]\nname="a"\nroot="~/x"\n[[target]]\nname="b"\nenabled=false\n')
        targets = common.load_targets(path, home="/h")
        self.assertEqual(targets, [{"name": "a", "root": "/h/x"}])

    def test_refuses_missing_key_duplicate_name_no_table_and_bad_toml(self):
        for text in ('[[target]]\nroot="x"\n', '[[target]]\nname="a"\n[[target]]\nname="a"\n', 'x = 1\n', 'not toml ['):
            with self.assertRaises(ValueError, msg=text):
                common.load_targets(self.write(text))
        with self.assertRaises(ValueError):
            common.load_targets("/nonexistent/targets.toml")


class SmallHelperTests(unittest.TestCase):
    def test_slug_is_safe_and_never_empty(self):
        self.assertEqual(common.slug("a b/c:d"), "a-b-c-d")
        self.assertEqual(common.slug("///"), "x")
        self.assertEqual(len(common.slug("a" * 200)), 48)

    def test_reports_dir_honours_the_environment(self):
        self.assertEqual(common.reports_dir({"HAMS_REPORTS_DIR": "/r"}), "/r")
        self.assertTrue(common.reports_dir({}).endswith("workspace/tmp/reports"))

    def test_log_line_appends_one_timestamped_line_and_folds_newlines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sub", "o.log")
            common.log_line(path, "a\nb  c", now=lambda: NOW)
            common.log_line(path, "second", now=lambda: NOW)
            lines = open(path).read().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].endswith(" a b c"))

    def test_write_json_is_atomic_and_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "d", "x.json")
            common.write_json(path, {"b": 1})
            self.assertEqual(common.read_json(path), {"b": 1})
            self.assertEqual(os.listdir(os.path.dirname(path)), ["x.json"])
            self.assertEqual(common.read_json(os.path.join(tmp, "none.json"), 5), 5)


@heavy
class RunJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"HAMS_JOBS_DIR": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_zero_and_nonzero_exits_are_ok_and_failed_with_the_exit_code(self):
        ok = common.run_job("t-ok", [sys.executable, "-c", "print('hi')"], timeout=60)
        self.assertEqual((ok.status, ok.exit_code), ("ok", 0))
        self.assertIn("hi", common.tail(ok.log))
        bad = common.run_job("t-bad", [sys.executable, "-c", "import sys; sys.exit(7)"], timeout=60)
        self.assertEqual((bad.status, bad.exit_code), ("failed", 7))

    def test_a_job_over_its_time_limit_is_stopped_through_agent_procs(self):
        started = time.time()
        slow = common.run_job("t-slow", [sys.executable, "-c", "import time; time.sleep(120)"], timeout=2, grace=5)
        self.assertEqual(slow.status, "timeout")
        self.assertLess(time.time() - started, 60)

    def test_a_command_that_cannot_start_is_an_error(self):
        self.assertEqual(common.run_job("t-none", ["/nonexistent/command"], timeout=30).status in ("error", "failed"), True)


if __name__ == "__main__":
    unittest.main()
