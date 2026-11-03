#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for lint_lock.py: the run lock (queue, stale holders, timeout, bypasses) and the load gate."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS)
import lint_lock  # noqa: E402
import lock_queue  # noqa: E402

HOLDER_SCRIPT = textwrap.dedent(
    """
    import os, sys, time
    sys.path.insert(0, {tools!r})
    import lint_lock
    lock = lint_lock.RunLock({{"XDG_CACHE_HOME": {cache!r}}})
    assert lock.acquire({info!r}), "child could not take the lock"
    print("held", flush=True)
    time.sleep(120)
    """
)


class Base(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.mkdtemp(prefix="lintlock-")
        self.env = {"XDG_CACHE_HOME": self.cache}
        self.children = []
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait()
        import shutil

        shutil.rmtree(self.cache, ignore_errors=True)
        os.environ.pop(lint_lock.HELD_ENV, None)

    def start_holder(self, info="/work/tree/A\trun_linters.py --changed"):
        child = subprocess.Popen(
            [sys.executable, "-c", HOLDER_SCRIPT.format(tools=TOOLS, cache=self.cache, info=info)],
            stdout=subprocess.PIPE,
            text=True,
        )
        self.children.append(child)
        self.assertEqual(child.stdout.readline().strip(), "held")
        return child


class DecisionTests(Base):
    def test_cheap_known_steps_take_no_lock(self):
        take, reason = lint_lock.lock_decision({}, ["a", "b"], {"a": 10.0, "b": 20.0})
        self.assertFalse(take)
        self.assertIn("cheap", reason)

    def test_threshold_is_the_sum_and_is_configurable(self):
        durations = {"a": 40.0, "b": 40.0}
        self.assertTrue(lint_lock.lock_decision({}, ["a", "b"], durations)[0])
        self.assertFalse(lint_lock.lock_decision({"HAMS_LINT_CHEAP_SECONDS": "100"}, ["a", "b"], durations)[0])

    def test_unknown_step_counts_as_heavy(self):
        self.assertTrue(lint_lock.lock_decision({}, ["a", "never_measured"], {"a": 1.0})[0])

    def test_nothing_pending_takes_no_lock(self):
        self.assertFalse(lint_lock.lock_decision({}, [], {})[0])

    def test_env_override_and_parent_hold_skip_the_lock(self):
        heavy = {"a": 500.0}
        self.assertEqual(lint_lock.lock_decision({"HAMS_LINT_NO_LOCK": "1"}, ["a"], heavy)[0], False)
        self.assertEqual(lint_lock.lock_decision({lint_lock.HELD_ENV: "123"}, ["a"], heavy)[0], False)
        self.assertTrue(lint_lock.lock_decision({}, ["a"], heavy)[0])

    def test_known_durations_is_the_median_over_cache_files(self):
        directory = os.path.join(self.cache, "hams-lint")
        os.makedirs(directory)
        for index, value in enumerate((10.0, 20.0, 1000.0)):
            with open(os.path.join(directory, "c%d.json" % index), "w", encoding="utf-8") as handle:
                json.dump({"seconds": {"step": value}}, handle)
        with open(os.path.join(directory, "bad.json"), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertEqual(lint_lock.known_durations(directory), {"step": 20.0})


class LockTests(Base):
    def test_second_run_waits_names_the_holder_and_times_out(self):
        child = self.start_holder()
        lines = []
        lock = lint_lock.RunLock(self.env)
        started = time.monotonic()
        got = lock.acquire("/work/tree/B\tx", timeout=2.5, out=lines.append, report_every=0.2, poll=0.1)
        self.assertFalse(got)
        self.assertLess(time.monotonic() - started, 20)
        self.assertTrue(lines)
        self.assertIn("waiting for the lint run in /work/tree/A (pid %d), position 1" % child.pid, lines[0])
        mine = [t for t in lock_queue.live_tickets(lock.qdir) if t["pid"] == os.getpid()]
        self.assertEqual(mine, [], "a timed-out waiter must leave the queue")

    def test_lock_is_taken_when_the_holder_exits_and_dead_holder_is_stale(self):
        child = self.start_holder()
        os.kill(child.pid, signal.SIGKILL)  # no cleanup: the holder record stays behind
        child.wait()
        lock = lint_lock.RunLock(self.env)
        self.assertTrue(lock.acquire("/work/tree/B\tx", timeout=10, out=lambda _l: None, poll=0.1))
        try:
            holder = lock_queue.read_holder(lock.qdir)
            self.assertEqual(holder["pid"], os.getpid())
            self.assertEqual(os.environ[lint_lock.HELD_ENV], str(os.getpid()))
        finally:
            lock.release()
        self.assertNotIn(lint_lock.HELD_ENV, os.environ)
        self.assertIsNone(lock_queue.read_holder(lock.qdir))

    def test_waiter_gets_the_lock_when_holder_finishes(self):
        child = self.start_holder()
        lock = lint_lock.RunLock(self.env)
        killer = subprocess.Popen([sys.executable, "-c", "import time,os,signal;time.sleep(1.5);os.kill(%d,9)" % child.pid])
        self.children.append(killer)
        self.assertTrue(lock.acquire("/work/tree/B\tx", timeout=30, out=lambda _l: None, poll=0.1))
        lock.release()

    def test_dead_waiter_ticket_does_not_count_for_position(self):
        qdir = lint_lock.queue_dir(self.env)
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        start = lock_queue.proc_start_time(os.getpid())
        os.makedirs(qdir, exist_ok=True)
        with open(os.path.join(qdir, "0000000001-%d-%d" % (dead.pid, start)), "w", encoding="utf-8") as handle:
            json.dump({"pid": dead.pid, "start": start, "queued_at": time.time(), "info": "x\ty"}, handle)
        self.assertIn("position 1", lint_lock.waiting_message(qdir))

    def test_begin_heavy_run_cheap_takes_no_lock_heavy_takes_it(self):
        ok, lock = lint_lock.begin_heavy_run(self.env, ["a"], {"a": 1.0}, "/wt", "cmd", out=lambda _l: None)
        self.assertEqual((ok, lock), (True, None))
        env = dict(self.env, HAMS_SKIP_CI_LOAD_GATE="1")
        ok, lock = lint_lock.begin_heavy_run(env, ["a"], {"a": 900.0}, "/wt", "cmd", out=lambda _l: None)
        self.assertTrue(ok)
        try:
            self.assertIsNotNone(lock)
            self.assertEqual(lock_queue.read_holder(lock.qdir)["info"], "/wt\tcmd")
        finally:
            lock.release()

    def test_begin_heavy_run_times_out_cleanly_behind_a_holder(self):
        self.start_holder()
        out = []
        ok, lock = lint_lock.begin_heavy_run(
            self.env, ["a"], {"a": 900.0}, "/wt", "cmd", lock_timeout=1.0, out=out.append
        )
        self.assertEqual((ok, lock), (False, None))
        self.assertTrue(any("gave up waiting" in line for line in out))


class LoadGateTests(unittest.TestCase):
    def test_returns_at_once_when_quiet(self):
        slept = []
        self.assertTrue(lint_lock.wait_for_load(8, 100, environ={}, loadavg=lambda: 3.0, sleep=slept.append, out=lambda _l: None))
        self.assertEqual(slept, [])

    def test_waits_until_load_falls(self):
        values = iter([20.0, 15.0, 7.0])
        clock = [0.0]

        def sleep(seconds):
            clock[0] += seconds

        self.assertTrue(
            lint_lock.wait_for_load(8, 600, environ={}, loadavg=lambda: next(values), sleep=sleep, now=lambda: clock[0], out=lambda _l: None)
        )
        self.assertGreater(clock[0], 0)

    def test_fails_clearly_at_the_timeout(self):
        clock = [0.0]
        lines = []

        def sleep(seconds):
            clock[0] += seconds

        ok = lint_lock.wait_for_load(8, 60, environ={}, loadavg=lambda: 30.0, sleep=sleep, now=lambda: clock[0], out=lines.append)
        self.assertFalse(ok)
        self.assertIn("ERROR", lines[-1])
        self.assertIn("HAMS_SKIP_CI_LOAD_GATE=1", lines[-1])

    def test_skip_env_is_the_same_switch_as_the_test_runners_gate(self):
        self.assertTrue(lint_lock.wait_for_load(1, 1, environ={"HAMS_SKIP_CI_LOAD_GATE": "1"}, loadavg=lambda: 99.0, out=lambda _l: None))

    def test_limit_and_timeout_precedence(self):
        self.assertEqual(lint_lock.load_limit({}, cpu_count=16), 16.0)
        self.assertEqual(lint_lock.load_limit({"HAMS_CI_LOAD_GATE_RATIO": "0.5"}, cpu_count=16), 8.0)
        self.assertEqual(lint_lock.load_limit({"HAMS_LINT_LOAD_LIMIT": "12", "HAMS_CI_LOAD_GATE_RATIO": "0.5"}, cpu_count=16), 12.0)
        self.assertEqual(lint_lock.load_limit({"HAMS_LINT_LOAD_LIMIT": "12"}, cpu_count=16, override=3), 3.0)
        self.assertEqual(lint_lock.load_limit({"HAMS_LINT_LOAD_LIMIT": "nan"}, cpu_count=4), 4.0)
        self.assertEqual(lint_lock.load_timeout({}), 900.0)
        self.assertEqual(lint_lock.load_timeout({"HAMS_CI_LOAD_GATE_TIMEOUT": "30"}), 30.0)
        self.assertEqual(lint_lock.load_timeout({"HAMS_LINT_LOAD_TIMEOUT": "5", "HAMS_CI_LOAD_GATE_TIMEOUT": "30"}), 5.0)


class RunnerIntegrationTests(Base):
    def test_lock_status_and_help_document_the_options(self):
        env = dict(os.environ, XDG_CACHE_HOME=self.cache)
        script = os.path.join(TOOLS, "run_linters.py")
        help_text = subprocess.run([sys.executable, script, "--help"], capture_output=True, text=True, env=env).stdout
        for flag in ("--lock-timeout", "--no-lock", "--lock-status", "--load-limit", "--load-timeout"):
            self.assertIn(flag, help_text)
        self.start_holder()
        status = subprocess.run([sys.executable, script, "--lock-status"], capture_output=True, text=True, env=env)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("holder: pid", status.stdout)


class DistributionTests(Base):
    def test_a_free_lock_and_a_quiet_box_is_not_busy(self):
        busy, _why = lint_lock.local_busy(self.env, loadavg=lambda: 0.5, cpu_count=16)
        self.assertFalse(busy)

    def test_a_run_holding_the_lock_makes_it_busy(self):
        self.start_holder()
        busy, why = lint_lock.local_busy(self.env, loadavg=lambda: 0.5, cpu_count=16)
        self.assertTrue(busy)
        self.assertIn("hold or wait", why)

    def test_load_above_the_limit_makes_it_busy_unless_the_gate_is_skipped(self):
        busy, why = lint_lock.local_busy(self.env, loadavg=lambda: 40.0, cpu_count=16)
        self.assertTrue(busy)
        self.assertIn("above the lint limit", why)
        env = dict(self.env, HAMS_SKIP_CI_LOAD_GATE="1")
        self.assertFalse(lint_lock.local_busy(env, loadavg=lambda: 40.0, cpu_count=16)[0])

    def test_a_machine_is_claimed_by_one_run_at_a_time_and_a_dead_claim_frees_it(self):
        first = lint_lock.try_claim("mac1-vm", self.env)
        self.assertIsNotNone(first)
        self.assertIsNone(lint_lock.try_claim("mac1-vm", self.env))
        self.assertIsNotNone(lint_lock.try_claim("bruce-yoga", self.env), "claims are per machine")
        self.assertEqual(lint_lock.claimed(["mac1-vm", "bruce-yoga"], self.env), {"mac1-vm"})
        first.close()
        self.assertEqual(lint_lock.claimed(["mac1-vm"], self.env), set())

    def test_a_claim_is_not_inherited_by_a_child_process(self):
        first = lint_lock.try_claim("mac1-vm", self.env)
        self.addCleanup(first.close)
        code = "import sys; sys.path.insert(0, %r); import lint_lock; print(lint_lock.try_claim('mac1-vm', {'XDG_CACHE_HOME': %r}) is None)" % (TOOLS, self.cache)
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(out, "True", "the child sees the claim as taken (it does not hold it)")
        first.close()
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(out, "False", "and gets it once the owner let go: no child kept it open")


if __name__ == "__main__":
    unittest.main()
