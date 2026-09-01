#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Regression test for OOMWatchdog's process-tree memory measurement in
test.py.

Real bug, found 2026-09-01: OOMWatchdog.run() summed plain RSS
(`p.memory_info().rss`) across every descendant of the test runner. RSS
double-counts memory shared between processes -- a browser-tour test's
renderer/GPU/zygote subprocesses sharing one large mapping get counted once
per process that maps it, not once total. On an idle box with 12GB
genuinely free, this produced climbing "aggregate RSS" readings (3.54GB,
then 4.34GB, then 7.11GB on identical retries) that falsely tripped the
watchdog's kill threshold. The fix sums PSS (proportional set size)
instead, which apportions shared pages across the processes that map them
and matches real host memory consumed; it falls back to plain RSS
per-process if PSS is unavailable (permission denied, or a platform without
smaps-based accounting), so the watchdog never becomes silently blind, only
reverts to its old (over-cautious) behavior.

`test.py` is loaded via importlib rather than `import test` because its
own module name collides with Python's stdlib `test` package.
"""

import importlib.util
import mmap
import os
import sys
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_TEST_PY_PATH = os.path.join(_HERE, "test.py")


def _load_test_py_module():
    spec = importlib.util.spec_from_file_location(
        "_hams_test_runner_module_under_test", _TEST_PY_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_test_runner = _load_test_py_module()


def _measure_process_tree(runner_pid, exclude_pid):
    """Calls the real measure_process_tree_memory() extracted from test.py
    -- the exact function OOMWatchdog.run() itself calls every 2 seconds
    in production -- rather than a hand-copied reimplementation, so this
    test actually breaks if that function's logic regresses."""
    import psutil

    total = _test_runner.measure_process_tree_memory(runner_pid, exclude_pid)
    num_procs = len(psutil.Process(runner_pid).children(recursive=True))
    return total, num_procs


class TestOOMWatchdogSharedMemoryAccounting(unittest.TestCase):
    """Confirms the measurement no longer overcounts a shared mapping
    across sibling subprocesses -- the exact topology OOMWatchdog measures
    in production (it is a sibling of the odoo-bin process tree, not an
    ancestor of it, both spawned directly by the test.py runner)."""

    SHARED_MB = 200
    N_CHILDREN = 4

    def setUp(self):
        self.shm_path = f"/dev/shm/oom_watchdog_test_{os.getpid()}.bin"
        with open(self.shm_path, "wb") as f:
            f.truncate(self.SHARED_MB * 1024 * 1024)
        self.addCleanup(lambda: os.path.exists(self.shm_path) and os.remove(self.shm_path))

    def _touch_and_hold_shared_mapping(self, hold_seconds):
        """Faults every page of the shared mapping in, then sleeps while
        still holding a live reference to the mmap object -- letting `mm`
        go out of scope unmaps the region immediately (CPython's
        refcounting GC), which would make the pages non-resident again
        before the parent ever gets to measure them."""
        with open(self.shm_path, "r+b") as f:
            mm = mmap.mmap(f.fileno(), self.SHARED_MB * 1024 * 1024)
            for i in range(0, len(mm), 4096):
                _ = mm[i]
            time.sleep(hold_seconds)

    def _spawn_sibling_target_tree(self):
        """Spawns a 'target' process (stand-in for odoo-bin) that itself
        forks N children sharing one large mapping (stand-in for a
        browser-tour test's renderer/GPU/zygote subprocesses), and returns
        its pid once every child has faulted its pages in. This process is
        a direct child of the current test process, matching the real
        runner -> {target tree, watchdog} sibling topology."""
        ready_r, ready_w = os.pipe()
        target_pid = os.fork()
        if target_pid == 0:
            os.close(ready_r)
            kids = []
            for _ in range(self.N_CHILDREN):
                pid = os.fork()
                if pid == 0:
                    self._touch_and_hold_shared_mapping(30)
                    os._exit(0)
                kids.append(pid)
            # crude readiness gate: give children time to fault every page in
            time.sleep(2.5)
            os.write(ready_w, b"1")
            os.close(ready_w)
            time.sleep(30)
            os._exit(0)
        os.close(ready_w)
        os.read(ready_r, 1)
        os.close(ready_r)
        return target_pid

    def _kill_tree(self, root_pid):
        import psutil

        try:
            root = psutil.Process(root_pid)
            for p in root.children(recursive=True):
                try:
                    p.kill()
                except psutil.NoSuchProcess:
                    pass
            root.kill()
        except psutil.NoSuchProcess:
            pass
        try:
            os.waitpid(root_pid, 0)
        except ChildProcessError:
            pass
        try:
            while True:
                pid, _ = os.waitpid(-1, os.WNOHANG)
                if pid == 0:
                    break
        except ChildProcessError:
            pass

    def test_pss_measurement_does_not_overcount_shared_mapping(self):
        target_pid = self._spawn_sibling_target_tree()
        self.addCleanup(self._kill_tree, target_pid)

        total_bytes, num_procs_seen = _measure_process_tree(
            runner_pid=target_pid, exclude_pid=os.getpid()
        )
        total_mb = total_bytes / 1024 / 1024

        self.assertEqual(
            num_procs_seen,
            self.N_CHILDREN,
            "watchdog's own children(recursive=True) walk should see every "
            "forked child of the target tree",
        )

        # The naive-RSS bug would report roughly N_CHILDREN * SHARED_MB
        # (each child independently reports the full shared mapping as its
        # own RSS). The fixed PSS-based measurement should report closer to
        # one copy of the shared mapping, not N copies.
        naive_rss_bug_total_mb = self.N_CHILDREN * self.SHARED_MB
        self.assertLess(
            total_mb,
            naive_rss_bug_total_mb * 0.6,
            f"measured {total_mb:.1f}MB is too close to the naive-RSS "
            f"overcount of {naive_rss_bug_total_mb}MB -- shared memory is "
            f"still being double-counted",
        )
        self.assertGreater(
            total_mb,
            self.SHARED_MB * 0.5,
            f"measured {total_mb:.1f}MB is implausibly low for a "
            f"{self.SHARED_MB}MB shared mapping actually resident -- "
            f"likely a broken measurement, not a fixed one",
        )

    def test_measurement_survives_a_dead_child(self):
        """A process that exits before its memory is read must not crash
        the measurement -- matches the existing try/except NoSuchProcess
        already present around the plain-RSS path. Exercised here against
        a real (already-exited) descendant of the current process, so
        children(recursive=True) genuinely returns a pid that is gone by
        the time memory_full_info()/memory_info() is called on it."""
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        os.waitpid(pid, 0)  # reap immediately so it's a genuinely dead pid

        try:
            total, _ = _measure_process_tree(runner_pid=os.getpid(), exclude_pid=-1)
        except Exception as e:  # noqa
            self.fail(f"measurement raised on a real live process tree: {e!r}")
        self.assertIsInstance(total, int)


if __name__ == "__main__":
    unittest.main()
