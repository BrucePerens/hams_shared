#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Tests for hang_guard.py and its use in lint_exec.run_step. Real processes: every one is started by the test and ends with it.

Tests [@ANCHOR: hang_guard:step_env_name]
Tests [@ANCHOR: hang_guard:step_limit]
Tests [@ANCHOR: hang_guard:run_cap]
Tests [@ANCHOR: hang_guard:remember]
Tests [@ANCHOR: hang_guard:process_section]
Tests [@ANCHOR: hang_guard:write_hang_report]
Tests [@ANCHOR: hang_guard:stop_own_group]
Tests [@ANCHOR: hang_guard:run_limited]
"""

import os
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_procs  # noqa: E402
import hang_guard  # noqa: E402
import lint_exec  # noqa: E402

HANG = [sys.executable, "-c", "import socket,time; a,b=socket.socketpair(); a.recv(1)"]


class NumberFiniteTests(unittest.TestCase):
    def test_non_finite_and_non_positive_values_take_the_default(self):
        for text in ("nan", "inf", "-inf", "0", "-3", "x", ""):
            self.assertEqual(hang_guard.number({"K": text}, "K", 7.0), 7.0, text)
        self.assertEqual(hang_guard.number({"K": "12.5"}, "K", 7.0), 12.5)


class Limits(unittest.TestCase):
    def test_env_name(self):
        self.assertEqual(hang_guard.step_env_name("pytest daemons/relay_signer"), "HAMS_LINT_STEP_TIMEOUT_PYTEST_DAEMONS_RELAY_SIGNER")

    def test_derived_from_the_median_with_floor_and_cap(self):
        self.assertEqual(hang_guard.step_limit("s", [100, 200, 5000], {}), 800)  # median 200 x 4
        self.assertEqual(hang_guard.step_limit("s", [5], {}), 300)  # floor
        self.assertEqual(hang_guard.step_limit("s", [2000], {}), 3600)  # cap
        self.assertEqual(hang_guard.step_limit("s", [], {}), 1800)  # never seen
        self.assertEqual(hang_guard.step_limit("s", [200], {}, scale=2), 1600)  # stretched under load
        self.assertEqual(hang_guard.step_limit("s", [2000], {}, scale=4), 3600)  # the cap holds after the stretch

    def test_environment_overrides(self):
        self.assertEqual(hang_guard.step_limit("a b", [100], {"HAMS_LINT_STEP_TIMEOUT_A_B": "42"}, scale=4), 42)
        self.assertEqual(hang_guard.step_limit("s", [100], {"HAMS_LINT_STEP_FACTOR": "10"}), 1000)
        self.assertEqual(hang_guard.step_limit("s", [100], {"HAMS_LINT_STEP_CAP": "350"}), 350)
        self.assertEqual(hang_guard.step_limit("s", [100], {"HAMS_LINT_STEP_FACTOR": "junk"}), 400)
        self.assertEqual(hang_guard.step_limit("s", [100], {"HAMS_LINT_STEP_TIMEOUT_S": "0"}), 400)  # not positive: ignored

    def test_run_cap(self):
        self.assertEqual(hang_guard.run_cap({}), 7200)
        self.assertEqual(hang_guard.run_cap({"HAMS_LINT_RUN_CAP": "90"}), 90)

    def test_remember_keeps_the_last_runs(self):
        history = {}
        for n in range(10):
            hang_guard.remember(history, "s", n + 0.123)
        self.assertEqual(history["s"], [3.12, 4.12, 5.12, 6.12, 7.12, 8.12, 9.12])


class Reports(unittest.TestCase):
    def test_report_lists_the_tree_the_socket_and_the_wait(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = subprocess.Popen(HANG, start_new_session=True)
            self.addCleanup(proc.wait)
            self.addCleanup(proc.kill)
            for _ in range(50):  # until the child sits in recv
                if "unix_stream_read" in hang_guard._text(f"/proc/{proc.pid}/wchan") or "sk_wait" in hang_guard._text(f"/proc/{proc.pid}/wchan"):
                    break
                time.sleep(0.1)
            path = hang_guard.write_hang_report(proc.pid, "pytest x/y", "test reason", directory)
            text = open(path, encoding="utf-8").read()
        self.assertTrue(path.startswith(directory) and path.endswith("_pytest_x_y.txt"))
        self.assertIn("reason: test reason", text)
        self.assertIn(f"=== pid {proc.pid} ", text)
        self.assertIn("wchan:", text)
        self.assertIn("socket:[", text)  # the open socketpair
        self.assertIn("py-spy dump:", text)

    def test_unwritable_directory_returns_none(self):
        self.assertIsNone(hang_guard.write_hang_report(os.getpid(), "x", "r", "/proc/nonexistent/dir", section=lambda info: ""))

    def test_a_clock_that_returns_nan_or_inf_does_not_stop_the_report(self):
        for bad in (float("nan"), float("inf")):
            with tempfile.TemporaryDirectory() as directory:
                path = hang_guard.write_hang_report(os.getpid(), "x", "r", directory, section=lambda info: "", clock=lambda bad=bad: bad)
                self.assertTrue(os.path.isfile(path))


class Stopping(unittest.TestCase):
    def test_refuses_a_wrong_start_time_and_a_pid_that_leads_no_group(self):
        proc = subprocess.Popen(HANG, start_new_session=True)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        info = agent_procs.read_proc(proc.pid)
        self.assertFalse(hang_guard.stop_own_group(proc.pid, info.start_ticks + 1))
        self.assertIsNone(proc.poll())
        self.assertFalse(hang_guard.stop_own_group(os.getpid(), agent_procs.read_proc(os.getpid()).start_ticks))  # not a group leader/session of its own
        self.assertTrue(hang_guard.stop_own_group(proc.pid, info.start_ticks, grace=5))
        proc.wait(5)

    def test_a_vanished_process_counts_as_stopped(self):
        self.assertTrue(hang_guard.stop_own_group(2 ** 22 + 12345, 1))

    def test_run_limited_passes_a_quick_command_through(self):
        done, report, timed_out = hang_guard.run_limited([sys.executable, "-c", "print('hi')"], 30, "quick")
        self.assertEqual((done.returncode, done.stdout.strip(), report, timed_out), (0, "hi", None, False))

    def test_run_limited_stops_a_hang_and_the_grandchild_too(self):
        script = ("import subprocess,sys,time; c=subprocess.Popen([sys.executable,'-c','import time; time.sleep(600)']); "
                  "print(c.pid, flush=True); time.sleep(600)")
        with tempfile.TemporaryDirectory() as directory:
            started = time.monotonic()
            done, report, timed_out = hang_guard.run_limited([sys.executable, "-c", script], 2, "hang", directory)
            took = time.monotonic() - started
            self.assertTrue(timed_out and done.returncode == hang_guard.TIMED_OUT_CODE and report)
            self.assertLess(took, 30)
            grandchild = int(done.stdout.split()[0])
            self.assertIn("sleep(600)", open(report, encoding="utf-8").read())
        time.sleep(0.2)
        left = agent_procs.read_proc(grandchild)
        self.assertTrue(left is None or left.state == "Z")


class StepUse(unittest.TestCase):
    def test_a_step_over_its_limit_is_timed_out_and_never_rerun(self):
        step = lint_exec.Step("hang step", argv=HANG, env=dict(os.environ, HAMS_LINT_STEP_TIMEOUT_HANG_STEP="2",
                                                              HAMS_HANG_REPORT_DIR=tempfile.mkdtemp()), cacheable=False)
        result = lint_exec.run_step(step, {})
        self.assertTrue(result.timed_out)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TIMED OUT after", result.output)
        self.assertIn("Hang report: ", result.output)
        result.saturated = "load"
        self.assertFalse(lint_exec.timeout_suspect(result))

    def test_the_run_deadline_ends_a_step_early(self):
        step = lint_exec.Step("hang step 2", argv=HANG, env=dict(os.environ, HAMS_HANG_REPORT_DIR=tempfile.mkdtemp()), cacheable=False)
        result = lint_exec.run_step(step, {}, deadline=time.monotonic() + 2)
        self.assertTrue(result.timed_out)
        self.assertIn("whole-run cap", result.output)

    def test_history_is_kept_for_passing_and_timed_out_runs_only(self):
        cache = lint_exec.Cache(os.path.join(tempfile.mkdtemp(), "c.json"))
        cache.record(lint_exec.Result("a", 0, "", 10.0), "k")
        cache.record(lint_exec.Result("a", 1, "", 1.0), None)
        cache.record(lint_exec.Result("a", 124, "", 99.0, timed_out=True), None)
        self.assertEqual(cache.recent_seconds("a"), [10.0, 99.0])


if __name__ == "__main__":
    unittest.main()
