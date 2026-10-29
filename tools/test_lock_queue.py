#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for lock_queue.py, the opt-in FIFO queue in front of test.py's box-wide lock, and for how
test.py wires it in. No Odoo and no real lock: every test uses a temporary lock path and queue
directory, and the end-to-end tests run test.py as a subprocess against that temporary lock with
the lock already held, so the process always stops at the lock and never starts a test run.

Tests [@ANCHOR: lock_queue:queue_dir_for_lock]
Tests [@ANCHOR: lock_queue:proc_start_time]
Tests [@ANCHOR: lock_queue:is_live]
Tests [@ANCHOR: lock_queue:take_ticket]
Tests [@ANCHOR: lock_queue:live_tickets]
Tests [@ANCHOR: lock_queue:release_ticket]
Tests [@ANCHOR: lock_queue:write_holder]
Tests [@ANCHOR: lock_queue:read_holder]
Tests [@ANCHOR: lock_queue:format_duration]
Tests [@ANCHOR: lock_queue:describe_queue]
Tests [@ANCHOR: lock_queue:acquire]
Tests [@ANCHOR: lock_queue:wait_options_from_argv]
Tests [@ANCHOR: test:acquire_lock_via_queue]
"""

import fcntl
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import lock_queue as lq  # noqa: E402

_TEST_PY = os.path.join(_HERE, "test.py")


def _dead_pid_and_start():
    """pid and start time of a child that has been killed and reaped."""
    p = subprocess.Popen(["sleep", "60"])
    start = lq.proc_start_time(p.pid)
    p.kill()
    p.wait()
    return p.pid, start


class _TmpDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.lock_path = os.path.join(self._tmp.name, "test.lock")
        self.qdir = lq.queue_dir_for_lock(self.lock_path, environ={})

    def open_lock(self):
        f = open(self.lock_path, "a")
        self.addCleanup(f.close)
        return f

    @staticmethod
    def try_flock(f):
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False


class TestPaths(unittest.TestCase):
    def test_queue_dir_follows_lock_path_and_env_override(self):
        self.assertEqual(lq.queue_dir_for_lock("/x/l", environ={}), "/x/l.queue.d")
        self.assertEqual(lq.queue_dir_for_lock("/x/l", environ={"HAMS_TEST_QUEUE_DIR": "/y"}), "/y")

    def test_format_duration(self):
        self.assertEqual(lq.format_duration(5), "5 s")
        self.assertEqual(lq.format_duration(12 * 60), "12 min")
        self.assertEqual(lq.format_duration(2 * 3600 + 300), "2 h 05 min")
        self.assertEqual(lq.format_duration(-3), "0 s")


class TestLiveness(unittest.TestCase):
    def test_own_process_is_live_and_wrong_start_time_is_not(self):
        start = lq.proc_start_time(os.getpid())
        self.assertIsInstance(start, int)
        self.assertTrue(lq.is_live(os.getpid(), start))
        self.assertFalse(lq.is_live(os.getpid(), start + 1))  # a recycled pid

    def test_dead_and_zombie_processes_are_not_live(self):
        pid, start = _dead_pid_and_start()
        self.assertFalse(lq.is_live(pid, start))
        z = subprocess.Popen(["true"])
        for _ in range(100):  # exited but not reaped: a zombie
            if lq.proc_start_time(z.pid) is None:
                break
            time.sleep(0.05)
        try:
            self.assertIsNone(lq.proc_start_time(z.pid))
        finally:
            z.wait()

    def test_garbage_pid_is_not_live(self):
        self.assertIsNone(lq.proc_start_time("not-a-pid"))
        self.assertIsNone(lq.proc_start_time(2 ** 30))


class TestTickets(_TmpDirCase):
    def test_numbers_increase_and_format(self):
        a = lq.take_ticket(self.qdir)
        b = lq.take_ticket(self.qdir)
        self.assertEqual(a.split("-")[0], "0000000001")
        self.assertEqual(b.split("-")[0], "0000000002")
        self.assertEqual([t["name"] for t in lq.live_tickets(self.qdir)], [a, b])

    def test_concurrent_takers_never_share_a_number(self):
        names = []
        lock = threading.Lock()

        def take():
            n = lq.take_ticket(self.qdir)
            with lock:
                names.append(n)

        threads = [threading.Thread(target=take) for _ in range(30)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        seqs = sorted(int(n.split("-")[0]) for n in names)
        self.assertEqual(seqs, list(range(1, 31)))

    def test_ticket_content_is_complete_when_visible(self):
        name = lq.take_ticket(self.qdir, info="test.py -u foo")
        self.assertEqual(lq.live_tickets(self.qdir)[0]["info"], "test.py -u foo")
        self.assertEqual(lq.live_tickets(self.qdir)[0]["name"], name)
        self.assertEqual([n for n in os.listdir(self.qdir) if n.startswith(".tmp")], [])

    def test_dead_and_reused_pid_tickets_are_skipped_and_cleaned(self):
        pid, start = _dead_pid_and_start()
        dead = lq.take_ticket(self.qdir, pid=pid, start=start)
        reused = lq.take_ticket(self.qdir, pid=os.getpid(), start=lq.proc_start_time(os.getpid()) + 7)
        live = lq.take_ticket(self.qdir)
        self.assertEqual([t["name"] for t in lq.live_tickets(self.qdir, clean=False)], [live])
        self.assertEqual(sorted(os.listdir(self.qdir)), sorted([dead, reused, live]))
        lq.live_tickets(self.qdir)
        self.assertEqual(os.listdir(self.qdir), [live])

    def test_new_ticket_goes_after_the_highest_even_when_lower_ones_died(self):
        pid, start = _dead_pid_and_start()
        lq.take_ticket(self.qdir, pid=pid, start=start)
        lq.live_tickets(self.qdir)  # cleans seq 1
        self.assertEqual(lq.take_ticket(self.qdir).split("-")[0], "0000000001")  # nothing left: reuse is fine
        lq.take_ticket(self.qdir)
        self.assertEqual(lq.take_ticket(self.qdir).split("-")[0], "0000000003")

    def test_release_is_idempotent(self):
        name = lq.take_ticket(self.qdir)
        lq.release_ticket(self.qdir, name)
        lq.release_ticket(self.qdir, name)
        lq.release_ticket(self.qdir, None)
        self.assertEqual(lq.live_tickets(self.qdir), [])

    def test_missing_queue_dir_lists_empty(self):
        self.assertEqual(lq.live_tickets(os.path.join(self.qdir, "nope")), [])


class TestHolder(_TmpDirCase):
    def test_write_read_and_dead_holder_ignored(self):
        self.assertIsNone(lq.read_holder(self.qdir))
        name = lq.write_holder(self.qdir, "test.py -u foo", now=lambda: 1000.0)
        holder = lq.read_holder(self.qdir)
        self.assertEqual((holder["pid"], holder["info"], holder["since"]), (os.getpid(), "test.py -u foo", 1000.0))
        pid, start = _dead_pid_and_start()
        lq._write_atomic(self.qdir, "holder-%d-%d" % (pid, start),
                         {"pid": pid, "start": start, "since": 9999.0, "info": "dead"})
        self.assertEqual(lq.read_holder(self.qdir)["info"], "test.py -u foo")  # newer but dead: ignored
        self.assertFalse(os.path.exists(os.path.join(self.qdir, "holder-%d-%d" % (pid, start))))
        lq.release_ticket(self.qdir, name)
        self.assertIsNone(lq.read_holder(self.qdir))

    def test_holder_records_never_count_as_tickets(self):
        lq.write_holder(self.qdir, "x")
        self.assertEqual(lq.live_tickets(self.qdir), [])
        self.assertEqual(lq.take_ticket(self.qdir).split("-")[0], "0000000001")

    def test_describe_queue_names_holder_and_positions(self):
        lq.write_holder(self.qdir, "test.py -u ham_shack", now=lambda: 0.0)
        a = lq.take_ticket(self.qdir, info="test.py -u a", now=lambda: 100.0)
        b = lq.take_ticket(self.qdir, info="test.py -u b", now=lambda: 200.0)
        text = "\n".join(lq.describe_queue(self.qdir, my_ticket=b, now=lambda: 720.0))
        self.assertIn("holder: pid %d running test.py -u ham_shack for 12 min" % os.getpid(), text)
        self.assertIn("queue 1: pid %d test.py -u a waiting 10 min" % os.getpid(), text)
        self.assertIn("queue 2:", text)
        self.assertIn("<- you", text.split("queue 2:")[1])
        self.assertNotIn("<- you", text.split("queue 2:")[0])
        self.assertIn(a, os.listdir(self.qdir))

    def test_describe_empty(self):
        self.assertEqual(lq.describe_queue(self.qdir), ["holder: none recorded", "queue: empty"])


class TestAcquire(_TmpDirCase):
    def acquire(self, **kw):
        kw.setdefault("poll", 0.01)
        kw.setdefault("handle_signals", False)
        kw.setdefault("out", lambda *_: None)
        return lq.acquire(self.qdir, **kw)

    def test_free_lock_is_taken_at_once_and_ticket_kept(self):
        f = self.open_lock()
        status, ticket = self.acquire(try_lock=lambda: self.try_flock(f))
        self.assertEqual(status, lq.ACQUIRED)
        self.assertEqual([t["name"] for t in lq.live_tickets(self.qdir)], [ticket])

    def test_waiters_get_the_lock_in_arrival_order(self):
        holder = self.open_lock()
        self.assertTrue(self.try_flock(holder))
        order = []
        ordering = threading.Lock()
        n = 5

        def waiter(i):
            f = open(self.lock_path, "a")
            status, ticket = self.acquire(try_lock=lambda: self.try_flock(f))
            with ordering:
                order.append(i)
            self.assertEqual(status, lq.ACQUIRED)
            time.sleep(0.02)
            lq.release_ticket(self.qdir, ticket)
            f.close()

        threads = []
        for i in range(n):  # start them one at a time so the arrival order is known
            t = threading.Thread(target=waiter, args=(i,))
            t.start()
            threads.append(t)
            for _ in range(500):
                if len(lq.live_tickets(self.qdir, clean=False)) == i + 1:
                    break
                time.sleep(0.01)
        time.sleep(0.2)
        self.assertEqual(order, [], "nobody may get in while the lock is held")
        holder.close()
        for t in threads:
            t.join(10)
        self.assertEqual(order, list(range(n)))
        self.assertEqual(lq.live_tickets(self.qdir), [])

    def test_only_the_head_tries_the_lock(self):
        lq.take_ticket(self.qdir)  # an earlier, live waiter (this process)
        attempts = []
        status, _ = self.acquire(try_lock=lambda: attempts.append(1) or True, timeout=0.15)
        self.assertEqual(status, lq.TIMEOUT)
        self.assertEqual(attempts, [])

    def test_dead_head_ticket_does_not_block(self):
        pid, start = _dead_pid_and_start()
        lq.take_ticket(self.qdir, pid=pid, start=start)
        status, ticket = self.acquire(try_lock=lambda: True, timeout=5)
        self.assertEqual(status, lq.ACQUIRED)
        self.assertEqual([t["name"] for t in lq.live_tickets(self.qdir)], [ticket])

    def test_head_dying_while_we_wait_promotes_us(self):
        child = subprocess.Popen(["sleep", "60"])
        lq.take_ticket(self.qdir, pid=child.pid, start=lq.proc_start_time(child.pid))
        threading.Timer(0.2, lambda: (child.kill(), child.wait())).start()
        status, _ = self.acquire(try_lock=lambda: True, timeout=10)
        self.assertEqual(status, lq.ACQUIRED)

    def test_timeout_releases_the_ticket(self):
        status, ticket = self.acquire(try_lock=lambda: False, timeout=0.1)
        self.assertEqual(status, lq.TIMEOUT)
        self.assertEqual(lq.live_tickets(self.qdir), [])

    def test_exception_in_try_lock_releases_the_ticket(self):
        def boom():
            raise RuntimeError("boom")
        with self.assertRaises(RuntimeError):
            self.acquire(try_lock=boom)
        self.assertEqual(lq.live_tickets(self.qdir), [])

    def test_keyboard_interrupt_releases_the_ticket(self):
        calls = []

        def sleeper(_):
            calls.append(1)
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.acquire(try_lock=lambda: False, sleep=sleeper)
        self.assertEqual(lq.live_tickets(self.qdir), [])

    def test_gate_runs_once_at_the_head_and_can_refuse(self):
        gate_calls = []
        status, _ = self.acquire(try_lock=lambda: False, on_head=lambda: gate_calls.append(1) or False)
        self.assertEqual((status, gate_calls), (lq.GATE_FAILED, [1]))
        self.assertEqual(lq.live_tickets(self.qdir), [])
        gate_calls.clear()
        attempts = []
        status, _ = self.acquire(try_lock=lambda: attempts.append(1) or len(attempts) > 3,
                                 on_head=lambda: gate_calls.append(1) or True)
        self.assertEqual((status, gate_calls), (lq.ACQUIRED, [1]))

    def test_gate_is_not_run_while_someone_is_ahead(self):
        lq.take_ticket(self.qdir)
        gate_calls = []
        status, _ = self.acquire(try_lock=lambda: True, timeout=0.1, on_head=lambda: gate_calls.append(1) or True)
        self.assertEqual((status, gate_calls), (lq.TIMEOUT, []))

    def test_adopted_ticket_is_used_not_requeued(self):
        mine = lq.take_ticket(self.qdir)  # the parent's ticket, same pid in this test
        status, ticket = self.acquire(try_lock=lambda: True, ticket=mine, on_head=lambda: self.fail("gate"))
        self.assertEqual((status, ticket), (lq.ACQUIRED, mine))
        self.assertEqual(len(os.listdir(self.qdir)), 1)

    def test_unknown_adopted_ticket_queues_normally(self):
        status, ticket = self.acquire(try_lock=lambda: True, ticket="0000000099-1-1")
        self.assertEqual(status, lq.ACQUIRED)
        self.assertNotEqual(ticket, "0000000099-1-1")

    def test_lost_ticket_file_rejoins(self):
        state = {"n": 0}

        def try_lock():
            state["n"] += 1
            if state["n"] == 1:
                for t in lq.live_tickets(self.qdir, clean=False):
                    lq.release_ticket(self.qdir, t["name"])
                return False
            return True
        status, ticket = self.acquire(try_lock=try_lock, timeout=5)
        self.assertEqual(status, lq.ACQUIRED)
        self.assertIn(ticket, os.listdir(self.qdir))

    def test_position_line_is_printed_with_holder(self):
        lq.write_holder(self.qdir, "test.py -u foo")
        lines = []
        clock = {"t": 0.0}

        def sleep(s):
            clock["t"] += 100

        self.acquire(try_lock=lambda: False, timeout=250, sleep=sleep, now=lambda: clock["t"],
                     out=lines.append, report_every=60)
        queued = [ln for ln in lines if ln.startswith("[*] queued: position 1 of 1, holder pid %d running test.py -u foo" % os.getpid())]
        self.assertGreaterEqual(len(queued), 2)
        self.assertIn("gave up waiting", lines[-1])

    def test_sigterm_in_a_waiting_process_removes_its_ticket(self):
        code = (
            "import sys, time; sys.path.insert(0, %r); import lock_queue as lq\n"
            "lq.acquire(%r, lambda: False, poll=0.05, out=lambda *_: None)\n" % (_HERE, self.qdir)
        )
        p = subprocess.Popen([sys.executable, "-c", code])
        try:
            for _ in range(200):
                if lq.live_tickets(self.qdir, clean=False):
                    break
                time.sleep(0.05)
            self.assertEqual(len(lq.live_tickets(self.qdir, clean=False)), 1)
            p.send_signal(signal.SIGTERM)
            self.assertEqual(p.wait(10), 128 + signal.SIGTERM)
        finally:
            if p.poll() is None:
                p.kill()
        self.assertEqual(os.listdir(self.qdir), [])

    def test_sigkill_leaves_a_ticket_that_the_next_waiter_ignores(self):
        code = (
            "import sys; sys.path.insert(0, %r); import lock_queue as lq\n"
            "lq.acquire(%r, lambda: False, poll=0.05, out=lambda *_: None)\n" % (_HERE, self.qdir)
        )
        p = subprocess.Popen([sys.executable, "-c", code])
        for _ in range(200):
            if lq.live_tickets(self.qdir, clean=False):
                break
            time.sleep(0.05)
        p.kill()
        p.wait()
        self.assertEqual(len(os.listdir(self.qdir)), 1)
        status, _ = self.acquire(try_lock=lambda: True, timeout=5)
        self.assertEqual(status, lq.ACQUIRED)


class TestOptions(unittest.TestCase):
    def test_flags_and_env(self):
        o = lq.wait_options_from_argv(["-u", "a", "--wait", "--wait-timeout", "90"], environ={})
        self.assertEqual(o, {"wait": True, "timeout": 90.0, "status": False})
        self.assertEqual(lq.wait_options_from_argv(["-u", "a"], environ={})["wait"], False)
        self.assertTrue(lq.wait_options_from_argv([], environ={"HAMS_TEST_WAIT": "1"})["wait"])
        o = lq.wait_options_from_argv([], environ={"HAMS_TEST_WAIT_TIMEOUT": "5"})
        self.assertEqual((o["wait"], o["timeout"]), (True, 5.0))
        self.assertEqual(lq.wait_options_from_argv([], environ={"HAMS_TEST_WAIT_TIMEOUT": "x"})["timeout"], None)
        self.assertTrue(lq.wait_options_from_argv(["--queue-status"], environ={})["status"])

    def test_abbreviations_and_other_flags_are_not_swallowed(self):
        o = lq.wait_options_from_argv(["--w", "-m", "xml", "--profile"], environ={})
        self.assertFalse(o["wait"])


class TestTestPyWiring(_TmpDirCase):
    """test.py as a subprocess against a temp lock that is already held: it must stop at the lock."""

    def run_test_py(self, *args, timeout=60, extra_env=None):
        env = dict(os.environ, HAMS_TEST_LOCK_PATH=self.lock_path, HAMS_SKIP_CI_LOAD_GATE="1",
                   HAMS_ISOLATED_NS="1")
        for k in ("HAMS_TEST_WAIT", "HAMS_TEST_WAIT_TIMEOUT", "HAMS_TEST_QUEUE_DIR", "HAMS_TEST_QUEUE_TICKET",
                  "HAMS_TEST_LOCK_HELD"):
            env.pop(k, None)
        env.update(extra_env or {})
        # --preflight-done: these tests are about the single-instance lock, which test.py takes after its pre-flight lint
        # pass; without the flag a lint finding anywhere in the surrounding repo (or a worktree without the sibling repo) stops
        # test.py before the lock and fails the tests for a reason that has nothing to do with the lock.
        return subprocess.run([sys.executable, _TEST_PY, "--preflight-done", "-u", "nonexistent_module", *args], env=env,
                              capture_output=True, text=True, timeout=timeout, cwd=os.path.dirname(_HERE))

    def hold(self):
        f = self.open_lock()
        self.assertTrue(self.try_flock(f))
        lq.write_holder(self.qdir, "test.py -u held_module")
        return f

    def test_default_stays_fail_fast(self):
        self.hold()
        started = time.time()
        r = self.run_test_py()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("Another instance of test.py is already running", r.stdout)
        self.assertLess(time.time() - started, 30)
        self.assertEqual(lq.live_tickets(self.qdir), [], "fail-fast must not queue")

    def test_wait_with_timeout_gives_up_with_status_75_and_leaves_no_ticket(self):
        self.hold()
        r = self.run_test_py("--wait", "--wait-timeout", "2")
        self.assertEqual(r.returncode, lq.EXIT_WAIT_TIMEOUT, r.stdout + r.stderr)
        self.assertIn("queued: position 1 of 1, holder pid %d running test.py -u held_module" % os.getpid(), r.stdout)
        self.assertEqual(os.listdir(self.qdir)[0].startswith("holder-"), True)
        self.assertEqual(lq.live_tickets(self.qdir), [])

    def test_env_var_enables_wait(self):
        self.hold()
        r = self.run_test_py(extra_env={"HAMS_TEST_WAIT": "1", "HAMS_TEST_WAIT_TIMEOUT": "1"})
        self.assertEqual(r.returncode, lq.EXIT_WAIT_TIMEOUT, r.stdout + r.stderr)

    def test_waiter_takes_over_when_the_holder_releases(self):
        holder = self.hold()
        p = subprocess.Popen([sys.executable, _TEST_PY, "--preflight-done", "-u", "nonexistent_module", "--wait"],
                             env=dict(os.environ, HAMS_TEST_LOCK_PATH=self.lock_path,
                                      HAMS_SKIP_CI_LOAD_GATE="1", HAMS_ISOLATED_NS="1"),
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             cwd=os.path.dirname(_HERE))
        try:
            for _ in range(300):
                if lq.live_tickets(self.qdir, clean=False):
                    break
                time.sleep(0.1)
            time.sleep(0.5)
            self.assertIsNone(p.poll(), "must still be waiting while the lock is held")
            holder.close()  # releases the flock
            out, _ = p.communicate(timeout=120)
        finally:
            if p.poll() is None:
                p.kill()
        # It got past the lock and failed later (no such module / not in a hams checkout), not at the lock.
        self.assertNotIn("Another instance", out)
        self.assertNotIn("timed out waiting", out)
        self.assertEqual(lq.live_tickets(self.qdir), [])

    def test_queue_status_is_read_only_and_does_not_need_the_lock(self):
        f = self.hold()
        r = self.run_test_py("--queue-status")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("holder: pid %d running test.py -u held_module" % os.getpid(), r.stdout)
        self.assertIn("queue: empty", r.stdout)
        self.assertFalse(self.try_flock(self.open_lock()), "--queue-status must not have released or taken the lock")
        f.close()


if __name__ == "__main__":
    unittest.main()
