#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for chrome_teardown.py with real process trees (sh/python) standing in for Chrome."""
import logging
import os
import subprocess
import sys
import tempfile
import time
import types
import unittest

import chrome_teardown as ct

READY = "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text('x')"


def spawn(script, ready):
    """Start `sh -c script` as a group leader; wait until it wrote the ready file (traps are armed)."""
    proc = ct.GroupPopen(["sh", "-c", script], start_new_session=True)
    deadline = time.monotonic() + 5
    while not os.path.exists(ready) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert os.path.exists(ready), "fake chrome never became ready"
    return proc


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record.getMessage())


class TerminateGroupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ready = os.path.join(self.tmp.name, "ready")
        self.logger = logging.getLogger("test.chrome_teardown")
        self.logger.propagate = False
        self.capture = Capture()
        self.logger.handlers = [self.capture]

    def finish(self, proc):
        # whatever the test left behind must not outlive it
        ct._signal_group(proc.pid, 9)
        proc.wait()

    def test_tree_that_exits_quickly_returns_quickly(self):
        proc = spawn(f"sleep 60 & sleep 60 & touch {self.ready}; wait", self.ready)
        start = time.monotonic()
        survivors = ct.terminate_group(proc, cap=10, logger=self.logger)
        elapsed = time.monotonic() - start
        self.assertEqual(survivors, [])
        self.assertLess(elapsed, 1.0)
        self.assertFalse(ct._group_alive(proc.pid))
        self.assertEqual(self.capture.records, [])

    def test_leader_ignoring_sigterm_is_killed_after_the_cap_and_logged(self):
        proc = spawn(f"trap '' TERM; touch {self.ready}; while :; do sleep 0.1; done", self.ready)
        start = time.monotonic()
        survivors = ct.terminate_group(proc, cap=0.5, logger=self.logger)
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 0.5)
        self.assertLess(elapsed, 3.0)
        self.assertTrue(survivors)
        self.assertFalse(ct._group_alive(proc.pid))
        self.assertTrue(any("killing" in m and "pid" in m for m in self.capture.records))

    def test_child_that_outlives_the_leader_is_found_and_killed(self):
        # leader exits on SIGTERM; its child ignores SIGTERM and stays in the group
        script = (
            f"(trap '' TERM; while :; do sleep 0.1; done) & touch {self.ready}; wait"
        )
        proc = spawn(script, self.ready)
        survivors = ct.terminate_group(proc, cap=0.5, logger=self.logger)
        self.assertTrue(survivors, "the lingering child must be reported")
        self.assertFalse(ct._group_alive(proc.pid))
        joined = "\n".join(self.capture.records)
        self.assertIn("still alive", joined)

    def test_descendant_in_another_group_is_waited_for(self):
        # like `setsid -w unshare ... chrome`: the real browser is in a DIFFERENT process group than the leader
        script = f"setsid sleep 60 & touch {self.ready}; wait"
        proc = spawn(script, self.ready)
        time.sleep(0.2)
        tree = ct._snapshot(proc)
        self.assertGreaterEqual(len(tree), 2)
        start = time.monotonic()
        survivors = ct.terminate_group(proc, cap=5, logger=self.logger)
        self.assertEqual(survivors, [])
        self.assertLess(time.monotonic() - start, 1.0)
        self.assertFalse(any(ct._alive(p) for p in tree))

    def test_already_exited_leader(self):
        proc = ct.GroupPopen(["true"], start_new_session=True)
        proc.wait()
        self.assertEqual(ct.terminate_group(proc, cap=1, logger=self.logger), [])

    def test_group_members_lists_the_group(self):
        proc = spawn(f"sleep 60 & touch {self.ready}; wait", self.ready)
        try:
            names = " ".join(cmd for _, _, cmd in ct.group_members(proc.pid))
            self.assertIn("sleep 60", names)
        finally:
            self.finish(proc)


class InstallTests(unittest.TestCase):
    def test_install_makes_popen_start_a_new_session_and_is_idempotent(self):
        common = types.SimpleNamespace(subprocess=subprocess)
        ct.install(common)
        ct.install(common)
        self.assertIs(common.subprocess.DEVNULL, subprocess.DEVNULL)
        proc = common.subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"])
        try:
            self.assertEqual(os.getpgid(proc.pid), proc.pid)
            self.assertNotEqual(os.getpgid(proc.pid), os.getpgid(0))
            self.assertIsInstance(proc, ct.GroupPopen)
            start = time.monotonic()
            proc.terminate()
            self.assertLess(time.monotonic() - start, 1.0)
            self.assertIsNotNone(proc.poll())
        finally:
            ct._signal_group(proc.pid, 9)
            proc.wait()
        self.assertIs(subprocess.Popen, subprocess.Popen)  # the real module is untouched
        self.assertNotIn("_hams_chrome_teardown", vars(subprocess))


if __name__ == "__main__":
    unittest.main()
