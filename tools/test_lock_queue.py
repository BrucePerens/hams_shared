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
Tests [@ANCHOR: lock_queue:read_holders]
Tests [@ANCHOR: lock_queue:may_try]
Tests [@ANCHOR: lock_queue:format_duration]
Tests [@ANCHOR: lock_queue:describe_queue]
Tests [@ANCHOR: lock_queue:acquire]
Tests [@ANCHOR: lock_queue:wait_options_from_argv]
Tests [@ANCHOR: test:acquire_lock_via_queue]
Tests [@ANCHOR: test:try_slot_locks]
Tests [@ANCHOR: test:slot_handoff_args]
"""

import fcntl
import json
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
import runner_slots as rs  # noqa: E402

_TEST_PY = os.path.join(_HERE, "test.py")

# Tests that run the real test.py must never touch the shared default database: another run on the machine may
# own `hams_test` (and Redis, and the HTTP port) while a private HAMS_TEST_LOCK_PATH here says nobody does.
# test.py runs `DROP DATABASE hams_test WITH (FORCE)` once it has a module to test, whatever lock it holds, so
# every such run gets (1) its own scratch database name, checked by `_test_py_argv`, and (2) stand-ins for the
# commands that reach PostgreSQL, Redis and sudo, which record their use and do nothing.
SCRATCH_DB = "hams_locktest_scratch"
_DEFAULT_DB = "hams_test"
# test.py finds psql by absolute path (infrastructure.get_pg_bin), which a PATH stand-in cannot intercept, so the
# environment also points every PostgreSQL client at a socket directory that does not exist: even a run that
# got past the scratch-name check could not connect, let alone drop anything.
NO_POSTGRES_ENV = {"PGHOST": "/nonexistent_hams_locktest", "PGPORT": "1", "PGCONNECT_TIMEOUT": "2",
                   "REDIS_DB": "15"}
_DB_COMMANDS = ("psql", "createdb", "dropdb", "pg_dump", "pg_isready", "redis-cli", "sudo")


def _test_py_argv(*words):
    """argv for a real test.py run: always `--db SCRATCH_DB` first; refuses a command line that names the
    default database (a later --db would win in argparse)."""
    argv = [sys.executable, _TEST_PY, "--preflight-done", "--db", SCRATCH_DB, *words]
    given = [argv[i + 1] for i, w in enumerate(argv[:-1]) if w in ("-d", "--db")]
    if given[-1] == _DEFAULT_DB or any(w.startswith("--db=") and w.split("=", 1)[1] == _DEFAULT_DB
                                       for w in words):
        raise AssertionError("a test must not run test.py against the shared database %r" % _DEFAULT_DB)
    return argv


def _install_db_stand_ins(directory):
    """Write recording stand-ins for the database commands into `directory`; returns (PATH value, log path)."""
    bin_dir = os.path.join(directory, "stand_in_bin")
    os.makedirs(bin_dir, exist_ok=True)
    log = os.path.join(directory, "db_commands.log")
    for name in _DB_COMMANDS:
        path = os.path.join(bin_dir, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('#!/bin/sh\necho "%s $*" >> "%s"\nexit 0\n' % (name, log))
        os.chmod(path, 0o755)
    return bin_dir + os.pathsep + os.environ.get("PATH", ""), log


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


class _SlotHolderMixin:
    def write(self, slot, ticket=None, since=1.0, info="run"):
        """A holder record for another live process (a sleeping child)."""
        lq._ensure_dir(self.qdir)
        child = subprocess.Popen(["sleep", "60"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        start = lq.proc_start_time(child.pid)
        payload = {"pid": child.pid, "start": start, "since": since, "info": info}
        if slot is not None:
            payload["slot"] = slot
        if ticket:
            payload["ticket"] = ticket
        lq._write_atomic(self.qdir, "holder-%d-%d" % (child.pid, start), payload)
        return child


class TestSlotHolders(_SlotHolderMixin, _TmpDirCase):
    """Holder records with a slot and a ticket, and the per-slot view of them."""

    def test_slot_and_ticket_are_stored_only_when_given(self):
        name = lq.write_holder(self.qdir, "plain", now=lambda: 5.0)
        record = lq.read_holder(self.qdir)
        self.assertEqual(sorted(record), ["info", "pid", "since", "start"])
        lq.release_ticket(self.qdir, name)
        lq.write_holder(self.qdir, "slotted", now=lambda: 5.0, slot=2, ticket="T")
        record = lq.read_holder(self.qdir)
        self.assertEqual((record["slot"], record["ticket"]), (2, "T"))

    def test_read_holders_one_live_record_per_slot_in_slot_order(self):
        self.write(3, since=1.0, info="three")
        self.write(1, since=2.0, info="old one")
        self.write(1, since=9.0, info="new one")  # the re-exec'd child of the same run
        self.write(None, since=3.0, info="legacy")  # no slot field: slot 1, older than "new one"
        holders = lq.read_holders(self.qdir)
        self.assertEqual([h["slot"] for h in holders], [1, 3])
        self.assertEqual([h["info"] for h in holders], ["new one", "three"])

    def test_read_holders_drops_dead_records(self):
        pid, start = _dead_pid_and_start()
        lq._ensure_dir(self.qdir)
        lq._write_atomic(self.qdir, "holder-%d-%d" % (pid, start),
                         {"pid": pid, "start": start, "since": 1.0, "info": "dead", "slot": 2})
        self.assertEqual(lq.read_holders(self.qdir), [])
        self.assertEqual(os.listdir(self.qdir), [])

    def test_describe_queue_with_slots_lists_each_slot_and_only_real_waiters(self):
        running = lq.take_ticket(self.qdir, info="running", now=lambda: 0.0)
        child = self.write(2, ticket=running, since=0.0, info="test.py -u ham_shack")
        waiter = lq.take_ticket(self.qdir, info="test.py -u waiting", now=lambda: 100.0)
        lines = lq.describe_queue(self.qdir, my_ticket=waiter, now=lambda: 720.0, slots=3)
        self.assertEqual(lines[0], "slots: 3, 1 busy")
        self.assertEqual(lines[1], "slot 1: free")
        self.assertEqual(lines[2], "slot 2: pid %d running test.py -u ham_shack for 12 min" % child.pid)
        self.assertEqual(lines[3], "slot 3: free")
        self.assertEqual(len([line for line in lines if line.startswith("queue ")]), 1)
        self.assertIn("queue 1: pid %d test.py -u waiting waiting 10 min  <- you" % os.getpid(), lines[4])

    def test_describe_queue_names_a_slot_beyond_the_configured_count(self):
        self.write(4, since=0.0, info="left over")
        text = "\n".join(lq.describe_queue(self.qdir, now=lambda: 60.0, slots=2))
        self.assertIn("slots: 2, 1 busy", text)
        self.assertIn("slot 4: pid", text)
        self.assertIn("(beyond the configured 2)", text)

    def test_one_slot_status_is_the_original_text(self):
        self.assertEqual(lq.describe_queue(self.qdir, slots=1), ["holder: none recorded", "queue: empty"])


class TestMayTry(_SlotHolderMixin, _TmpDirCase):
    """Who is the head of the queue, with one slot and with several."""

    def tickets(self):
        return lq.live_tickets(self.qdir, clean=False)

    def test_one_slot_is_the_lowest_live_ticket_running_or_not(self):
        running = lq.take_ticket(self.qdir)
        self.write(None, ticket=running)
        waiter = lq.take_ticket(self.qdir)
        self.assertTrue(lq.may_try(self.qdir, self.tickets(), running, slots=1))
        self.assertFalse(lq.may_try(self.qdir, self.tickets(), waiter, slots=1))

    def test_two_slots_one_running_the_oldest_waiter_may_try(self):
        running = lq.take_ticket(self.qdir)
        self.write(1, ticket=running)
        first = lq.take_ticket(self.qdir)
        second = lq.take_ticket(self.qdir)
        self.assertTrue(lq.may_try(self.qdir, self.tickets(), first, slots=2))
        self.assertFalse(lq.may_try(self.qdir, self.tickets(), second, slots=2))

    def test_two_slots_both_running_nobody_may_try(self):
        a = lq.take_ticket(self.qdir)
        self.write(1, ticket=a)
        b = lq.take_ticket(self.qdir)
        self.write(2, ticket=b)
        waiter = lq.take_ticket(self.qdir)
        self.assertFalse(lq.may_try(self.qdir, self.tickets(), waiter, slots=2))

    def test_a_run_without_a_ticket_still_takes_a_slot(self):
        self.write(1)  # a fail-fast run: a holder record and no ticket
        waiter = lq.take_ticket(self.qdir)
        self.assertTrue(lq.may_try(self.qdir, self.tickets(), waiter, slots=2))
        self.write(2)
        self.assertFalse(lq.may_try(self.qdir, self.tickets(), waiter, slots=2))

    def test_a_reexecd_child_does_not_count_its_parents_slot_against_itself(self):
        mine = lq.take_ticket(self.qdir)
        self.write(2, ticket=mine)  # the parent's record: same ticket, slot 2
        other = lq.take_ticket(self.qdir)
        self.write(1, ticket=other)
        self.assertTrue(lq.may_try(self.qdir, self.tickets(), mine, slots=2))

    def test_a_ticket_ahead_that_is_waiting_blocks_us_only_when_slots_are_scarce(self):
        busy = lq.take_ticket(self.qdir)
        self.write(1, ticket=busy)
        ahead = lq.take_ticket(self.qdir)
        mine = lq.take_ticket(self.qdir)
        self.assertFalse(lq.may_try(self.qdir, self.tickets(), mine, slots=2))
        self.assertTrue(lq.may_try(self.qdir, self.tickets(), ahead, slots=2))

    def test_two_free_slots_let_two_waiters_try_so_a_stuck_head_blocks_nobody(self):
        ahead = lq.take_ticket(self.qdir)
        mine = lq.take_ticket(self.qdir)
        third = lq.take_ticket(self.qdir)
        self.assertTrue(lq.may_try(self.qdir, self.tickets(), ahead, slots=2))
        self.assertTrue(lq.may_try(self.qdir, self.tickets(), mine, slots=2))
        self.assertFalse(lq.may_try(self.qdir, self.tickets(), third, slots=2))

    def test_a_holder_record_without_its_ticket_still_makes_that_ticket_a_running_run(self):
        # What a run started with one slot used to write: the record has the pid and no ticket, while the
        # same process waited with a ticket. The slotted waiter behind it counted that ticket as the oldest
        # waiter and never tried although three of four slots were free.
        child = subprocess.Popen(["sleep", "60"])
        self.addCleanup(child.kill)
        self.addCleanup(child.wait)
        start = lq.proc_start_time(child.pid)
        running = lq.take_ticket(self.qdir, pid=child.pid, start=start)
        lq._write_atomic(self.qdir, "holder-%d-%d" % (child.pid, start),
                         {"pid": child.pid, "start": start, "since": 1.0, "info": "old style"})  # no slot, no ticket
        waiter = lq.take_ticket(self.qdir)                # this process
        self.assertEqual(self.tickets()[0]["name"], running)
        self.assertTrue(lq.may_try(self.qdir, self.tickets(), waiter, slots=4))
        self.assertFalse(lq.may_try(self.qdir, self.tickets(), waiter, slots=1))   # single slot: as it always was


class TestAcquireWithSlots(_TmpDirCase):
    """lock_queue.acquire in front of real slot lock files."""

    SLOTS = 2

    def setUp(self):
        super().setUp()
        self.held = []  # open locked files; closing one frees its slot

    def tearDown(self):
        for f in self.held:
            f.close()

    def slot_file(self, slot):
        f = open(rs.slot_lock_path(self.lock_path, slot), "a")
        self.addCleanup(f.close)
        return f

    def hold_slot(self, slot, ticket=None):
        lq._ensure_dir(self.qdir)
        f = self.slot_file(slot)
        self.assertTrue(self.try_flock(f))
        self.held.append(f)
        child = subprocess.Popen(["sleep", "60"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        start = lq.proc_start_time(child.pid)
        record = {"pid": child.pid, "start": start, "since": 1.0, "info": "holder", "slot": slot}
        if ticket:
            record["ticket"] = ticket
        name = "holder-%d-%d" % (child.pid, start)
        lq._write_atomic(self.qdir, name, record)
        return f, name

    def release_slot(self, held):
        f, record = held
        f.close()
        os.unlink(os.path.join(self.qdir, record))

    def make_try(self, taken, only=None):
        def try_lock():
            for slot in ([only] if only else range(1, self.SLOTS + 1)):
                f = self.slot_file(slot)
                if self.try_flock(f):
                    taken.append((slot, f))
                    return True
                f.close()
            return False
        return try_lock

    def acquire(self, taken, **kw):
        kw.setdefault("poll", 0.01)
        kw.setdefault("handle_signals", False)
        kw.setdefault("out", lambda *_: None)
        kw.setdefault("slots", self.SLOTS)
        only = kw.pop("only", None)
        return lq.acquire(self.qdir, self.make_try(taken, only), **kw)

    def run_waiter(self, taken, results, key, **kw):
        def target():
            results[key] = self.acquire(taken, **kw)
        thread = threading.Thread(target=target)
        thread.start()
        self.addCleanup(thread.join, 10)
        return thread

    def wait_for_tickets(self, count):
        for _ in range(500):
            if len(lq.live_tickets(self.qdir, clean=False)) >= count:
                return
            time.sleep(0.01)
        self.fail("waiters did not queue")

    def test_two_free_slots_two_runs_both_start_without_waiting_for_each_other(self):
        taken = []
        first = self.acquire(taken, timeout=5)
        self.assertEqual(first[0], lq.ACQUIRED)
        # What test.py does once it holds a slot: a record naming the slot and the ticket.
        lq.write_holder(self.qdir, "first", slot=taken[0][0], ticket=first[1])
        second = self.acquire(taken, timeout=5)
        self.assertEqual(second[0], lq.ACQUIRED)
        self.assertEqual(sorted(slot for slot, _ in taken), [1, 2])
        for _, f in taken:
            self.held.append(f)

    def test_the_lowest_free_slot_is_taken(self):
        self.hold_slot(1)
        taken = []
        self.assertEqual(self.acquire(taken, timeout=5)[0], lq.ACQUIRED)
        self.assertEqual([slot for slot, _ in taken], [2])
        self.held.append(taken[0][1])

    def test_a_third_run_waits_and_takes_the_slot_that_frees_in_fifo_order(self):
        one = self.hold_slot(1)
        self.hold_slot(2)
        taken, results = [], {}
        third = self.run_waiter(taken, results, "third", timeout=20)
        self.wait_for_tickets(1)
        fourth = self.run_waiter(taken, results, "fourth", timeout=3)
        self.wait_for_tickets(2)
        time.sleep(0.3)
        self.assertEqual(results, {}, "both slots are held: nobody may start")
        self.release_slot(one)
        third.join(10)
        self.assertEqual(results["third"][0], lq.ACQUIRED)
        self.assertEqual([slot for slot, _ in taken], [1])
        lq.write_holder(self.qdir, "third", slot=1, ticket=results["third"][1])
        self.held.append(taken[0][1])
        time.sleep(0.3)
        self.assertNotIn("fourth", results, "the freed slot is taken again: the fourth still waits")
        # Let the fourth give up cleanly so no thread is left behind.
        fourth.join(10)
        self.assertEqual(results["fourth"][0], lq.TIMEOUT)

    def test_the_gate_waits_until_a_slot_is_free(self):
        one = self.hold_slot(1)
        self.hold_slot(2)
        gate_calls, taken, results = [], [], {}
        waiter = self.run_waiter(taken, results, "w", timeout=20,
                                 on_head=lambda: gate_calls.append(1) or True)
        self.wait_for_tickets(1)
        time.sleep(0.3)
        self.assertEqual(gate_calls, [], "no slot is free: the load gate must not run yet")
        self.release_slot(one)
        waiter.join(10)
        self.assertEqual((results["w"][0], gate_calls), (lq.ACQUIRED, [1]))
        self.held.append(taken[0][1])

    def test_a_reexecd_child_takes_back_the_slot_its_parent_held(self):
        parent_ticket = lq.take_ticket(self.qdir)
        parent_slot = self.hold_slot(2, ticket=parent_ticket)
        other = self.hold_slot(1, ticket=lq.take_ticket(self.qdir))
        self.assertIsNotNone(other)
        parent_slot[0].close()  # the exec dropped the parent's flock; its record stays
        taken = []
        status, ticket = self.acquire(taken, ticket=parent_ticket, only=2, timeout=5)
        self.assertEqual((status, ticket, [slot for slot, _ in taken]), (lq.ACQUIRED, parent_ticket, [2]))
        self.held.append(taken[0][1])

    def test_a_reexecd_child_waits_if_another_run_took_its_slot(self):
        parent_ticket = lq.take_ticket(self.qdir)
        self.hold_slot(2, ticket=None)  # someone else now holds the parent's slot
        status, _ = self.acquire([], ticket=parent_ticket, only=2, timeout=0.2)
        self.assertEqual(status, lq.TIMEOUT)

    def test_one_slot_keeps_the_original_head_rule(self):
        holder_ticket = lq.take_ticket(self.qdir)
        self.hold_slot(1, ticket=holder_ticket)
        taken = []
        self.SLOTS = 1
        status, _ = self.acquire(taken, timeout=0.2, slots=1)
        self.assertEqual((status, taken), (lq.TIMEOUT, []))


class TestOptions(unittest.TestCase):
    def test_flags_and_env(self):
        o = lq.wait_options_from_argv(["-u", "a", "--wait", "--wait-timeout", "90"], environ={})
        self.assertEqual(o, {"wait": True, "timeout": 90.0, "status": False})
        self.assertEqual(lq.wait_options_from_argv(["-u", "a"], environ={})["wait"], False)
        self.assertTrue(lq.wait_options_from_argv([], environ={"HAMS_TEST_WAIT": "1"})["wait"])
        o = lq.wait_options_from_argv([], environ={"HAMS_TEST_WAIT_TIMEOUT": "5"})
        self.assertEqual((o["wait"], o["timeout"]), (True, 5.0))
        self.assertEqual(lq.wait_options_from_argv([], environ={"HAMS_TEST_WAIT_TIMEOUT": "x"})["timeout"], None)
        for text in ("nan", "inf", "-Infinity"):
            self.assertIsNone(lq.wait_options_from_argv([], environ={"HAMS_TEST_WAIT_TIMEOUT": text})["timeout"], text)
            with self.assertRaises(SystemExit, msg=text):
                lq.wait_options_from_argv(["--wait-timeout", text], environ={})
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
                  "HAMS_TEST_LOCK_HELD", "HAMS_TEST_SLOTS"):
            env.pop(k, None)
        env.update(extra_env or {})
        env["PATH"], self.db_log = _install_db_stand_ins(self._tmp.name)
        env.update(NO_POSTGRES_ENV)
        # --preflight-done: these tests are about the single-instance lock, which test.py takes after its pre-flight lint
        # pass; without the flag a lint finding anywhere in the surrounding repo (or a worktree without the sibling repo) stops
        # test.py before the lock and fails the tests for a reason that has nothing to do with the lock.
        # HAMS_TEST_SLOTS=1: the environment outranks /opt/hams/etc/test_slots (and --slots can only raise the count),
        # so these tests of the one-slot lock mean the same on a machine that has opted in to more slots.
        env["HAMS_TEST_SLOTS"] = "1"
        return subprocess.run(_test_py_argv("-u", "nonexistent_module", *args), env=env,
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
        p = subprocess.Popen(_test_py_argv("-u", "nonexistent_module", "--wait"),
                             env=dict(os.environ, HAMS_TEST_SLOTS="1", HAMS_TEST_LOCK_PATH=self.lock_path,
                                      HAMS_SKIP_CI_LOAD_GATE="1", HAMS_ISOLATED_NS="1",
                                      PATH=_install_db_stand_ins(self._tmp.name)[0], **NO_POSTGRES_ENV),
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


class TestSlotsWiring(_TmpDirCase):
    """test.py as a subprocess with several slots: which slot it takes, what it records, and that the
    re-exec'd phases take back only the slot their parent held. Each run stops soon after the lock (no
    such module), so no Odoo is started; the default (one slot) is covered by TestTestPyWiring."""

    def setUp(self):
        super().setUp()
        self.pids = {}

    def env(self, **extra):
        env = dict(os.environ, HAMS_TEST_LOCK_PATH=self.lock_path, HAMS_SKIP_CI_LOAD_GATE="1",
                   HAMS_ISOLATED_NS="1")
        for k in ("HAMS_TEST_WAIT", "HAMS_TEST_WAIT_TIMEOUT", "HAMS_TEST_QUEUE_DIR", "HAMS_TEST_QUEUE_TICKET",
                  "HAMS_TEST_LOCK_HELD", "HAMS_TEST_SLOTS"):
            env.pop(k, None)
        env.update(extra)
        env["PATH"], self.db_log = _install_db_stand_ins(self._tmp.name)
        env.update(NO_POSTGRES_ENV)
        return env

    def argv(self, *args):
        return _test_py_argv("-u", "nonexistent_module", *args)

    def run_test_py(self, *args, timeout=60, **extra_env):
        return subprocess.run(self.argv(*args), env=self.env(**extra_env), capture_output=True, text=True,
                              timeout=timeout, cwd=os.path.dirname(_HERE))

    def hold_slot(self, slot):
        """What a running test.py holds for its slot: the flock and a holder record naming the slot
        (written for a sleeping child, so each slot has a holder process of its own)."""
        f = open(rs.slot_lock_path(self.lock_path, slot), "a")
        self.addCleanup(f.close)
        self.assertTrue(self.try_flock(f))
        child = subprocess.Popen(["sleep", "60"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        self.pids[slot] = child.pid
        name = lq.write_holder(self.qdir, "test.py -u held_%d" % slot, pid=child.pid, slot=slot)
        self.addCleanup(lq.release_ticket, self.qdir, name)
        return f

    def slot_is_free(self, slot):
        f = open(rs.slot_lock_path(self.lock_path, slot), "a")
        try:
            return self.try_flock(f)
        finally:
            f.close()

    def test_a_free_second_slot_is_taken_when_the_first_is_held(self):
        self.hold_slot(1)
        r = self.run_test_py(HAMS_TEST_SLOTS="2")
        self.assertIn("Holding test slot 2 of 2 (lock %s.slot-2)" % self.lock_path, r.stdout, r.stdout + r.stderr)
        self.assertNotIn("Another instance", r.stdout)
        self.assertNotIn("slots are busy", r.stdout)
        self.assertEqual([h["slot"] for h in lq.read_holders(self.qdir)], [1], "the run removed its own record")

    def test_the_lowest_free_slot_is_taken(self):
        self.hold_slot(2)
        r = self.run_test_py(HAMS_TEST_SLOTS="3")
        self.assertIn("Holding test slot 1 of 3 (lock %s)" % self.lock_path, r.stdout, r.stdout + r.stderr)

    def test_the_slot_database_is_derived_from_the_slot(self):
        self.hold_slot(1)
        r = self.run_test_py("--db", "hams_locktest_relay", HAMS_TEST_SLOTS="2")
        self.assertIn("Test slot 2 of 2: database hams_locktest_relay_s2", r.stdout, r.stdout + r.stderr)

    def test_every_slot_held_fails_fast_and_names_the_slots(self):
        self.hold_slot(1)
        self.hold_slot(2)
        started = time.time()
        r = self.run_test_py(HAMS_TEST_SLOTS="2")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("All 2 test.py slots are busy", r.stdout)
        self.assertLess(time.time() - started, 30)
        self.assertEqual(lq.live_tickets(self.qdir), [], "fail-fast must not queue")

    def test_the_command_line_can_only_raise_the_slot_count(self):
        # An explicit --slots 1 on a machine set to 4 is a 4-slot run: it takes the free slot 2 instead of
        # fighting for slot 1 and queueing as if the others did not exist.
        self.hold_slot(1)
        r = self.run_test_py("--slots", "1", HAMS_TEST_SLOTS="4")
        self.assertIn("Holding test slot 2 of 4", r.stdout, r.stdout + r.stderr)
        r = self.run_test_py("--slots", "3", HAMS_TEST_SLOTS="2")
        self.assertIn("of 3", r.stdout, r.stdout + r.stderr)

    def test_a_bad_slot_count_is_an_error_not_one_slot(self):
        r = self.run_test_py(HAMS_TEST_SLOTS="lots")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("cannot determine the test slot count", r.stdout)
        self.assertIn("HAMS_TEST_SLOTS", r.stdout)
        r = self.run_test_py("--slots", "99")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)

    def test_a_held_slot_outside_the_count_is_a_usage_error(self):
        r = self.run_test_py("--slots", "2", "--slot-held", "3")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("--slot-held 3", r.stderr)

    def test_a_reexecd_phase_takes_only_the_slot_its_parent_held(self):
        # The root phase of the full pipeline is started with `--slots N --slot-held K`. Slot 1 is free
        # here, yet it must not take it: that is another run's database.
        self.hold_slot(2)
        r = self.run_test_py("--internal-ns-init", "--slots", "2", "--slot-held", "2")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("Another instance of test.py is already running", r.stdout)
        self.assertTrue(self.slot_is_free(1), "slot 1 must not have been taken or left locked")

    def test_a_reexecd_phase_takes_back_its_slot_when_free(self):
        r = self.run_test_py("--internal-ns-init", "--slots", "2", "--slot-held", "2")
        self.assertIn("Holding test slot 2 of 2", r.stdout, r.stdout + r.stderr)
        self.assertNotIn("Another instance", r.stdout)

    def test_wait_reports_every_slot_and_times_out_with_75(self):
        self.hold_slot(1)
        self.hold_slot(2)
        r = self.run_test_py("--wait", "--wait-timeout", "2", HAMS_TEST_SLOTS="2")
        self.assertEqual(r.returncode, lq.EXIT_WAIT_TIMEOUT, r.stdout + r.stderr)
        self.assertIn("queued: position 1 of 1, 2 of 2 slots busy: slot 1 pid %d running test.py -u held_1"
                      % self.pids[1], r.stdout)
        self.assertIn("slot 2 pid %d running test.py -u held_2" % self.pids[2], r.stdout)
        self.assertEqual(lq.live_tickets(self.qdir), [])

    def test_wait_starts_at_once_when_a_slot_is_free(self):
        self.hold_slot(1)
        started = time.time()
        r = self.run_test_py("--wait", "--wait-timeout", "30", HAMS_TEST_SLOTS="2")
        self.assertIn("Holding test slot 2 of 2", r.stdout, r.stdout + r.stderr)
        self.assertLess(time.time() - started, 25)

    def test_a_waiter_takes_the_slot_that_frees_up(self):
        self.hold_slot(1)
        two = self.hold_slot(2)
        p = subprocess.Popen(self.argv("--wait"), env=self.env(HAMS_TEST_SLOTS="2"), stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, cwd=os.path.dirname(_HERE))
        try:
            for _ in range(300):
                if lq.live_tickets(self.qdir, clean=False):
                    break
                time.sleep(0.1)
            time.sleep(0.5)
            self.assertIsNone(p.poll(), "must still be waiting while every slot is held")
            two.close()  # slot 2 is released
            out, _ = p.communicate(timeout=120)
        finally:
            if p.poll() is None:
                p.kill()
        self.assertIn("Holding test slot 2 of 2", out)
        self.assertNotIn("timed out waiting", out)
        self.assertEqual(lq.live_tickets(self.qdir), [])

    def test_queue_status_shows_each_slot(self):
        self.hold_slot(2)
        r = self.run_test_py("--queue-status", HAMS_TEST_SLOTS="3")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        lines = r.stdout.splitlines()
        self.assertEqual(lines[0], "slots: 3, 1 busy")
        self.assertEqual(lines[1], "slot 1: free")
        self.assertIn("slot 2: pid %d running test.py -u held_2" % self.pids[2], lines[2])
        self.assertEqual(lines[3], "slot 3: free")
        self.assertEqual(lines[-1], "queue: empty")
        self.assertTrue(self.slot_is_free(1) and self.slot_is_free(3), "--queue-status must not take a lock")

    def test_pause_on_fail_above_slot_one_is_refused_on_the_host_and_names_the_shortcut(self):
        self.hold_slot(1)
        r = self.run_test_py("--pause-on-fail", HAMS_TEST_SLOTS="2")   # HAMS_ISOLATED_NS=1: a run on the host
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("--pause-on-fail pins Chrome's debugging port 9222", r.stdout)
        self.assertIn("this run is on the host because HAMS_ISOLATED_NS=1 is set", r.stdout)

    def test_mixed_one_slot_and_slotted_runs_share_one_queue(self):
        # The case that left slots 2-4 idle: a run started with one slot (an older test.py, or HAMS_TEST_SLOTS=1)
        # holds slot 1 with a record that has no ticket and waited with a ticket of its own; a slotted run
        # queued behind it must start in slot 2 at once, with real lock files and a real test.py process.
        holder = self.hold_slot(1)
        ticket = lq.take_ticket(self.qdir, pid=self.pids[1], start=lq.proc_start_time(self.pids[1]))
        self.assertTrue(ticket)
        for record in os.listdir(self.qdir):                 # rewrite the holder record the old way: no slot, no ticket
            if record.startswith("holder-"):
                path = os.path.join(self.qdir, record)
                with open(path, encoding="utf-8") as handle:
                    payload = json.load(handle)
                payload.pop("slot", None)
                payload.pop("ticket", None)
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle)
        started = time.time()
        r = self.run_test_py("--wait", "--wait-timeout", "30", HAMS_TEST_SLOTS="4")
        self.assertIn("Holding test slot 2 of 4", r.stdout, r.stdout + r.stderr)
        self.assertNotEqual(r.returncode, lq.EXIT_WAIT_TIMEOUT)
        self.assertLess(time.time() - started, 25)
        holder.close()


class TestGuardAgainstTheSharedDatabase(_TmpDirCase):
    """The tests above run the real test.py; this proves they cannot reach the shared database, and that a
    command line test.py rejects is rejected before any database or Redis command (argument validation
    comes first in main(): a rejected run must not drop, create or flush anything)."""

    def test_the_helper_refuses_the_default_database(self):
        for words in (("--db", "hams_test"), ("-d", "hams_test"), ("--db=hams_test",)):
            with self.assertRaises(AssertionError, msg=words):
                _test_py_argv(*words)
        self.assertEqual(_test_py_argv()[3:5], ["--db", SCRATCH_DB])

    def test_the_stand_ins_record_and_succeed(self):
        path, log = _install_db_stand_ins(self._tmp.name)
        out = subprocess.run(["dropdb", "x"], env=dict(os.environ, PATH=path), capture_output=True, text=True)
        self.assertEqual(out.returncode, 0)
        with open(log, encoding="utf-8") as handle:
            self.assertEqual(handle.read().strip(), "dropdb x")

    def test_a_rejected_invocation_touches_no_database(self):
        env = dict(os.environ, HAMS_TEST_LOCK_PATH=self.lock_path, HAMS_SKIP_CI_LOAD_GATE="1",
                   HAMS_ISOLATED_NS="1")
        for k in ("HAMS_TEST_WAIT", "HAMS_TEST_QUEUE_DIR", "HAMS_TEST_QUEUE_TICKET", "HAMS_TEST_LOCK_HELD",
                  "HAMS_TEST_SLOTS"):
            env.pop(k, None)
        env["PATH"], log = _install_db_stand_ins(self._tmp.name)
        env.update(NO_POSTGRES_ENV)
        cases = [
            (["--slots", "99"], {}),
            (["--slots", "0"], {}),
            (["--slots", "lots"], {}),
            (["--slots", "2", "--slot-held", "3"], {}),
            ([], {"HAMS_TEST_SLOTS": "lots"}),
            ([], {"HAMS_TEST_SLOTS": "16"}),
        ]
        for words, extra in cases:
            r = subprocess.run(_test_py_argv("-u", "nonexistent_module", *words), env=dict(env, **extra),
                               capture_output=True, text=True, timeout=60, cwd=os.path.dirname(_HERE))
            self.assertEqual(r.returncode, 2, (words, extra, r.stdout, r.stderr))
            self.assertFalse(os.path.exists(log) and open(log, encoding="utf-8").read(),
                             "a rejected command line ran a database command: %r %r" % (words, extra))



if __name__ == "__main__":
    unittest.main()
