#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Tests for agent_procs.py with REAL processes (Bruce, 2026-11-08). Every process signalled here was started by the test that
signals it. Decoys carry a unique token in their arguments; a fake PostgreSQL is `exec -a postgres sleep`; pid reuse is simulated
by naming a wrong start time; the "waiter excludes itself" test runs the very loop shape that never ended on 2026-11-08.

Tests [@ANCHOR: agent_procs:ProcInfo]
Tests [@ANCHOR: agent_procs:JobRecord]
Tests [@ANCHOR: agent_procs:WaitResult]
Tests [@ANCHOR: agent_procs:user_name]
Tests [@ANCHOR: agent_procs:boot_time]
Tests [@ANCHOR: agent_procs:format_start]
Tests [@ANCHOR: agent_procs:parse_start_time]
Tests [@ANCHOR: agent_procs:check_start_time]
Tests [@ANCHOR: agent_procs:read_proc]
Tests [@ANCHOR: agent_procs:snapshot]
Tests [@ANCHOR: agent_procs:ancestry]
Tests [@ANCHOR: agent_procs:descendants]
Tests [@ANCHOR: agent_procs:self_family]
Tests [@ANCHOR: agent_procs:is_under]
Tests [@ANCHOR: agent_procs:caller_worktree]
Tests [@ANCHOR: agent_procs:chrome_data_dir]
Tests [@ANCHOR: agent_procs:protected_reason]
Tests [@ANCHOR: agent_procs:ps_line]
Tests [@ANCHOR: agent_procs:shell_clones]
Tests [@ANCHOR: agent_procs:find_procs]
Tests [@ANCHOR: agent_procs:read_pid_file]
Tests [@ANCHOR: agent_procs:jobs_dir]
Tests [@ANCHOR: agent_procs:check_job_name]
Tests [@ANCHOR: agent_procs:record_path]
Tests [@ANCHOR: agent_procs:write_record]
Tests [@ANCHOR: agent_procs:read_record]
Tests [@ANCHOR: agent_procs:list_records]
Tests [@ANCHOR: agent_procs:pid_alive]
Tests [@ANCHOR: agent_procs:signal_same_process]
Tests [@ANCHOR: agent_procs:job_state]
Tests [@ANCHOR: agent_procs:forward_signal]
Tests [@ANCHOR: agent_procs:wrapper_main]
Tests [@ANCHOR: agent_procs:parse_size]
Tests [@ANCHOR: agent_procs:limit_argv]
Tests [@ANCHOR: agent_procs:stop_group]
Tests [@ANCHOR: agent_procs:orphan_reason]
Tests [@ANCHOR: agent_procs:cmd_reap]
Tests [@ANCHOR: agent_procs:start_job]
Tests [@ANCHOR: agent_procs:wait_pid]
Tests [@ANCHOR: agent_procs:wait_matching]
Tests [@ANCHOR: agent_procs:wait_job]
Tests [@ANCHOR: agent_procs:wait_file]
Tests [@ANCHOR: agent_procs:group_members]
Tests [@ANCHOR: agent_procs:stop_job]
Tests [@ANCHOR: agent_procs:authorize_kill]
Tests [@ANCHOR: agent_procs:kill_pid]
Tests [@ANCHOR: agent_procs:cmd_find]
Tests [@ANCHOR: agent_procs:cmd_wait]
Tests [@ANCHOR: agent_procs:cmd_run]
Tests [@ANCHOR: agent_procs:cmd_jobs]
Tests [@ANCHOR: agent_procs:cmd_stop]
Tests [@ANCHOR: agent_procs:cmd_kill_pid]
Tests [@ANCHOR: agent_procs:build_parser]
Tests [@ANCHOR: agent_procs:parse_args]
Tests [@ANCHOR: agent_procs:main]
"""

import dataclasses
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_procs as ap  # noqa: E402

TOOL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_procs.py")


def cli(*args, env=None, cwd=None, timeout=60):
    """Run the tool as a subprocess; returns CompletedProcess with text output."""
    full = dict(os.environ, **(env or {}))
    return subprocess.run([sys.executable, TOOL] + list(args), capture_output=True, text=True, env=full, cwd=cwd,
                          timeout=timeout)


class ProcTestCase(unittest.TestCase):
    """Base class: a private jobs directory and cleanup of exactly the processes this test started."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="agent-procs-test-")
        self.jobs = os.path.join(self.tmp, "jobs")
        os.makedirs(self.jobs)
        self.env = {"HAMS_JOBS_DIR": self.jobs}
        self._old = os.environ.get("HAMS_JOBS_DIR")
        os.environ["HAMS_JOBS_DIR"] = self.jobs
        self.spawned = []
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for proc in self.spawned:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            proc.wait()
        for rec in ap.list_records(self.jobs):
            if rec.pgid in (os.getpgrp(), 0, 1):  # never the group of the test runner itself
                continue
            try:
                os.killpg(rec.pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        if self._old is None:
            os.environ.pop("HAMS_JOBS_DIR", None)
        else:
            os.environ["HAMS_JOBS_DIR"] = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def spawn(self, script, argv0=None, cwd=None, extra=()):
        """Start `sh -c script` (optionally with `exec -a argv0` in the script) in its own session; waits until the
        process shows the wanted argv0. Returns the Popen."""
        proc = subprocess.Popen(["bash", "-c", script] + list(extra), cwd=cwd, start_new_session=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.spawned.append(proc)
        if argv0:
            self.until(lambda: (ap.read_proc(proc.pid) or ap.ProcInfo(0, 0, 0, 0, 0, "", "", 0, "", ("",), None)).argv[0]
                       == argv0)
        return proc

    def decoy(self, token, cwd=None):
        """A python process sleeping 120s whose arguments contain `token`."""
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", token], cwd=cwd,
                                start_new_session=True)
        self.spawned.append(proc)
        self.until(lambda: ap.read_proc(proc.pid) is not None and token in " ".join(ap.read_proc(proc.pid).argv))
        return proc

    def find(self, **kwargs):
        """`ap.find_procs` as a separate process would see the table: `me` is a leaf process of ours, so the decoys (our
        children, not its) are listed and this test process (its ancestor) is not."""
        if not hasattr(self, "_leaf"):
            self._leaf = self.spawn("sleep 120")
        return ap.find_procs(ap.snapshot(), me=self._leaf.pid, **kwargs)

    def until(self, condition, seconds=10):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if condition():
                return
            time.sleep(0.05)
        self.fail("condition not reached in time")

    def alive(self, proc):
        return proc.poll() is None


class ProcReadingTests(ProcTestCase):
    """Reading /proc: ProcInfo, read_proc, snapshot, ancestry, descendants, self_family, boot_time, format_start,
    ps_line, user_name, is_under, chrome_data_dir."""

    def test_read_proc_of_self_and_a_missing_pid(self):
        info = ap.read_proc(os.getpid())
        self.assertEqual(info.pid, os.getpid())
        self.assertEqual(info.uid, os.getuid())
        self.assertEqual(info.user, ap.user_name(os.getuid()))
        self.assertGreater(info.start_ticks, 0)
        self.assertEqual(info.ppid, os.getppid())
        self.assertEqual(info.cwd, os.path.realpath(os.getcwd()))
        self.assertIsNone(ap.read_proc(2 ** 22 + 12345))

    def test_a_command_name_with_parentheses_does_not_break_the_start_time(self):
        weird = os.path.join(self.tmp, "we) (ird")
        shutil.copy(shutil.which("sleep"), weird)
        proc = subprocess.Popen([weird, "60"], start_new_session=True)
        self.spawned.append(proc)
        # Wait for the exec to settle too: a just-copied binary being paged in under load is briefly in state D, which failed this on a loaded box.
        self.until(lambda: (ap.read_proc(proc.pid) or ap.ProcInfo(0, 0, 0, 0, 0, "", "", 0, "", ("",), None)).comm
                   == "we) (ird" and ap.read_proc(proc.pid).state in ("S", "R"))
        info = ap.read_proc(proc.pid)
        self.assertEqual(info.state in ("S", "R"), True)
        self.assertEqual(info.ppid, os.getpid())
        self.assertGreater(info.start_ticks, 0)

    def test_start_time_is_stable_and_differs_between_processes(self):
        first = self.decoy("tokstable1")
        second = self.decoy("tokstable2")
        self.assertEqual(ap.read_proc(first.pid).start_ticks, ap.read_proc(first.pid).start_ticks)
        self.assertNotEqual(ap.read_proc(first.pid).pid, ap.read_proc(second.pid).pid)

    def test_snapshot_contains_self_and_children(self):
        child = self.decoy("toksnap")
        table = ap.snapshot()
        self.assertIn(os.getpid(), table)
        self.assertIn(child.pid, table)
        self.assertEqual(table[child.pid].ppid, os.getpid())

    def test_ancestry_descendants_and_self_family(self):
        child = self.decoy("tokfamily")
        table = ap.snapshot()
        up = ap.ancestry(table, os.getpid())
        self.assertIn(os.getpid(), up)
        self.assertIn(os.getppid(), up)
        self.assertIn(1, up)
        self.assertNotIn(child.pid, up)
        self.assertIn(child.pid, ap.descendants(table, os.getpid()))
        self.assertNotIn(os.getpid(), ap.descendants(table, os.getpid()))
        family = ap.self_family(table)
        self.assertTrue({os.getpid(), os.getppid(), child.pid} <= family)

    def test_boot_time_and_format_start(self):
        self.assertGreater(ap.boot_time(), 1_000_000_000)
        text = ap.format_start(ap.read_proc(os.getpid()).start_ticks)
        self.assertRegex(text, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")

    def test_format_start_of_a_corrupt_start_time_is_unknown_not_an_exception(self):
        # A record file can hold any number; none may raise out of format_start.
        for ticks in (10 ** 30, -(10 ** 30), 2 ** 62, float("inf"), float("nan")):
            self.assertEqual(ap.format_start(ticks), "unknown", ticks)

    def test_ps_line_names_pid_start_ticks_and_full_args(self):
        proc = self.decoy("tokpsline")
        line = ap.ps_line(ap.read_proc(proc.pid))
        self.assertIn(f"pid={proc.pid}", line)
        self.assertIn("ticks=", line)
        self.assertIn("tokpsline", line)

    def test_user_name_of_unknown_uid_is_the_number(self):
        self.assertEqual(ap.user_name(4000000001), "4000000001")
        self.assertEqual(ap.user_name(0), "root")

    def test_is_under_resolves_links_and_rejects_siblings(self):
        real = os.path.join(self.tmp, "real")
        os.makedirs(os.path.join(real, "sub"))
        os.symlink(real, os.path.join(self.tmp, "link"))
        self.assertTrue(ap.is_under(os.path.join(self.tmp, "link", "sub"), real))
        self.assertTrue(ap.is_under(real, real))
        self.assertFalse(ap.is_under(real + "x", real))
        self.assertFalse(ap.is_under(None, real))
        self.assertFalse(ap.is_under(real, ""))

    def test_chrome_data_dir(self):
        self.assertEqual(ap.chrome_data_dir(["chrome", "--user-data-dir=/x/y", "--foo"]), "/x/y")
        self.assertIsNone(ap.chrome_data_dir(["chrome"]))


class CallerWorktreeTests(ProcTestCase):
    """caller_worktree."""

    def test_environment_override_then_dot_git_walk_then_none(self):
        self.assertEqual(ap.caller_worktree({"HAMS_AGENT_WORKTREE": "/w"}), "/w")
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(repo, ".git"))
        os.makedirs(os.path.join(repo, "a", "b"))
        self.assertEqual(ap.caller_worktree({}, os.path.join(repo, "a", "b")), os.path.realpath(repo))
        self.assertIsNone(ap.caller_worktree({}, "/proc"))


class ProtectedReasonTests(ProcTestCase):
    """protected_reason with real fake-service processes (exec -a)."""

    def fake(self, argv0):
        return ap.read_proc(self.spawn(f'exec -a "{argv0}" sleep 120', argv0, cwd=self.tmp).pid)

    def test_fake_postgres_names_are_protected(self):
        for name in ("postgres", "postgres: hams_test hams_pf_probe [local] idle", "/usr/lib/postgresql/18/bin/postmaster"):
            reason = ap.protected_reason(self.fake(name), tmpdir=self.tmp)
            self.assertIsNotNone(reason, name)
            self.assertIn("shared service", reason)

    def test_other_protected_programs(self):
        for name in ("redis-server *:6379", "rabbitmq-server", "beam.smp -W w", "epmd -daemon", "/lib/systemd/systemd --user",
                     "sshd: bruce", "gdm-session-worker", "gnome-shell", "claude-desktop"):
            self.assertIsNotNone(ap.protected_reason(self.fake(name)), name)

    def test_claude_application_is_protected(self):
        reason = ap.protected_reason(self.fake("claude"))
        self.assertIn("claude application", reason)

    def test_test_py_outside_the_worktree_is_protected_inside_is_not(self):
        info = self.fake("python3 tools/test.py -u x")
        self.assertIn("test.py", ap.protected_reason(info, worktree=self.tmp + "/elsewhere"))
        self.assertIn("test.py", ap.protected_reason(info, worktree=None))
        self.assertIsNone(ap.protected_reason(info, worktree=os.path.realpath(self.tmp)))

    def test_chrome_is_protected_unless_under_the_callers_tmpdir(self):
        udd = os.path.join(self.tmp, "profile")
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "chrome", f"--user-data-dir={udd}"],
                                start_new_session=True)
        self.spawned.append(proc)
        self.until(lambda: ap.read_proc(proc.pid) is not None and "chrome" in ap.read_proc(proc.pid).argv)
        info = ap.read_proc(proc.pid)
        self.assertIn("TMPDIR", ap.protected_reason(info, tmpdir=None))
        self.assertIn("TMPDIR", ap.protected_reason(info, tmpdir="/nonexistent-other"))
        self.assertIsNone(ap.protected_reason(info, tmpdir=self.tmp))

    def test_root_owned_pid_1_and_ancestry_are_protected(self):
        table = ap.snapshot()
        self.assertIn("pid 1", ap.protected_reason(table[1]))
        parent = table[os.getppid()]
        self.assertIn("ancestors", ap.protected_reason(parent, family={os.getppid()}))
        root_proc = next(info for info in table.values() if info.uid == 0 and info.pid != 1)
        self.assertIn("owned by", ap.protected_reason(root_proc))

    def test_an_ordinary_own_process_is_not_protected(self):
        self.assertIsNone(ap.protected_reason(ap.read_proc(self.decoy("tokordinary").pid)))


class FindTests(ProcTestCase):
    """find_procs, read_pid_file, cmd_find, build_parser: a decoy whose args hold the pattern."""

    def test_find_excludes_itself_its_ancestors_and_descendants(self):
        decoy = self.decoy("tokfindA")
        script = f"{sys.executable} {TOOL} find --cmd-contains tokfindA"
        done = subprocess.run(["bash", "-c", script + "  # tokfindA in the parent shell too"], capture_output=True,
                              text=True, env=dict(os.environ, **self.env))
        self.assertEqual(done.returncode, 0, done.stderr)
        lines = done.stdout.strip().splitlines()
        self.assertEqual(len(lines), 1, done.stdout)
        self.assertIn(f"pid={decoy.pid} ", lines[0])
        self.assertNotIn("PROTECTED", lines[0])

    def test_find_in_process_never_lists_this_process_though_its_args_match(self):
        found = ap.find_procs(ap.snapshot(), contains="python")
        self.assertNotIn(os.getpid(), [info.pid for info, _ in found])

    def test_find_exit_code_one_when_nothing_matches(self):
        done = cli("find", "--cmd-contains", "no-such-token-zz9", "--quiet")
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout, "")

    def test_cwd_under_and_regex_and_literal_are_different(self):
        sub = os.path.join(self.tmp, "wd")
        os.makedirs(sub)
        inside = self.decoy("tokcwdA", cwd=sub)
        outside = self.decoy("tokcwdA")
        found = [info.pid for info, _ in self.find(cwd_under=sub, contains="tokcwdA")]
        self.assertEqual(found, [inside.pid])
        both = [info.pid for info, _ in self.find(contains="tokcwdA")]
        self.assertEqual(sorted(both), sorted([inside.pid, outside.pid]))
        self.assertEqual([info.pid for info, _ in self.find(regex=r"tokcwd[A]$", cwd_under=sub)],
                         [inside.pid])
        self.assertEqual(self.find(contains="tokcwd[A]"), [])  # a literal, not a pattern
        with self.assertRaises(ap.ProcError):
            self.find(regex="(")

    def test_other_users_are_never_listed_without_any_user_and_are_flagged_with_it(self):
        mine = {info.uid for info, _ in self.find()}
        self.assertEqual(mine, {os.getuid()})
        everyone = self.find(any_user=True)
        root_rows = [(info, why) for info, why in everyone if info.uid == 0]
        self.assertTrue(root_rows)
        self.assertTrue(all(why for _, why in root_rows))
        with self.assertRaises(ap.ProcError):
            self.find(user="root")
        done = cli("find", "--user", "root")
        self.assertEqual(done.returncode, 2)
        self.assertIn("--any-user", done.stderr)

    def test_fake_postgres_is_listed_with_protected_flag(self):
        proc = self.spawn('exec -a "postgres: hams_test tokpgdb [local] idle" sleep 120',
                          "postgres: hams_test tokpgdb [local] idle")
        done = cli("find", "--cmd-contains", "tokpgdb")
        self.assertEqual(done.returncode, 0)
        self.assertIn(f"pid={proc.pid} ", done.stdout)
        self.assertIn("PROTECTED", done.stdout)

    def test_pid_file_with_and_without_start_time(self):
        decoy = self.decoy("tokpidfile")
        ticks = ap.read_proc(decoy.pid).start_ticks
        path = os.path.join(self.tmp, "pids")
        with open(path, "w") as handle:
            handle.write(f"# comment\n{decoy.pid}:{ticks}\n\n")
        self.assertEqual(ap.read_pid_file(path), {decoy.pid: ticks})
        self.assertEqual([i.pid for i, _ in self.find(pids=ap.read_pid_file(path))], [decoy.pid])
        with open(path, "w") as handle:
            handle.write(f"{decoy.pid}:{ticks + 1}\n")  # a reused pid: wrong start time
        self.assertEqual(self.find(pids=ap.read_pid_file(path)), [])
        with open(path, "w") as handle:
            handle.write(f"{decoy.pid}\n")
        self.assertEqual(ap.read_pid_file(path), {decoy.pid: None})
        with open(path, "w") as handle:
            handle.write("12 34\n")
        with self.assertRaises(ap.ProcError):
            ap.read_pid_file(path)
        with self.assertRaises(ap.ProcError):
            ap.read_pid_file(os.path.join(self.tmp, "missing"))

    def test_json_output_and_parser(self):
        decoy = self.decoy("tokjson")
        done = cli("find", "--cmd-contains", "tokjson", "--json")
        self.assertEqual(done.returncode, 0)
        rows = json.loads(done.stdout)
        self.assertEqual([row["pid"] for row in rows], [decoy.pid])
        self.assertIsNone(rows[0]["protected"])


class JobRecordTests(ProcTestCase):
    """jobs_dir, check_job_name, record_path, write_record, read_record, list_records, JobRecord."""

    def test_jobs_dir_default_and_override(self):
        self.assertEqual(ap.jobs_dir({"HAMS_JOBS_DIR": self.jobs}), self.jobs)
        fresh = os.path.join(self.tmp, "new", "dir")
        self.assertEqual(ap.jobs_dir({"HAMS_JOBS_DIR": fresh}), fresh)
        self.assertEqual(oct(os.stat(fresh).st_mode & 0o777), "0o700")
        self.assertTrue(ap.jobs_dir({"HOME": self.tmp}).endswith(os.path.join(".cache", "hams-jobs"))
                        or "HAMS_JOBS_DIR" in os.environ)

    def test_job_names_are_validated(self):
        for good in ("a", "build-1", "x.y_z", "A" * 64):
            self.assertEqual(ap.check_job_name(good), good)
        for bad in ("", ".hidden", "-x", "a/b", "a b", "A" * 65, "../x", None, "guard"):
            with self.assertRaises(ap.ProcError, msg=repr(bad)):
                ap.check_job_name(bad)

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_write_record_failure_keeps_old_record(self):
        # Tests [@ANCHOR: agent_procs:write_record]
        rec = ap.JobRecord("one", "agent-1", 11, 11, 11, 5, "/w", ["x", "y"], "/l", 100.0, "bruce")
        ap.write_record(rec, self.jobs)
        os.chmod(self.jobs, 0o555)
        try:
            with self.assertRaises(OSError):
                ap.write_record(dataclasses.replace(rec, pid=99), self.jobs)
        finally:
            os.chmod(self.jobs, 0o700)
        self.assertEqual(ap.read_record("one", self.jobs), rec)

    def test_write_read_list_roundtrip_and_bad_files(self):
        rec = ap.JobRecord("one", "agent-1", 11, 11, 11, 5, "/w", ["x", "y"], "/l", 100.0, "bruce")
        ap.write_record(rec, self.jobs)
        self.assertEqual(ap.record_path("one", self.jobs), os.path.join(self.jobs, "one.json"))
        self.assertEqual(ap.read_record("one", self.jobs), rec)
        later = ap.JobRecord("two", "", 12, 12, 12, 6, "/w", ["z"], "/l", 200.0, "bruce", exit_code=0)
        ap.write_record(later, self.jobs)
        with open(os.path.join(self.jobs, "broken.json"), "w") as handle:
            handle.write("{not json")
        self.assertIsNone(ap.read_record("broken", self.jobs))
        self.assertIsNone(ap.read_record("absent", self.jobs))
        self.assertEqual([r.name for r in ap.list_records(self.jobs)], ["one", "two"])
        self.assertEqual([f for f in os.listdir(self.jobs) if f.endswith(".tmp")], [])


class ProcStateTests(ProcTestCase):
    """pid_alive, job_state, group_members."""

    def test_pid_alive_needs_the_right_start_time(self):
        decoy = self.decoy("tokalive")
        ticks = ap.read_proc(decoy.pid).start_ticks
        self.assertTrue(ap.pid_alive(decoy.pid, ticks))
        self.assertFalse(ap.pid_alive(decoy.pid, ticks + 1))  # pid reuse simulated
        decoy.kill()
        decoy.wait()
        self.assertFalse(ap.pid_alive(decoy.pid, ticks))

    def test_a_zombie_is_not_alive(self):
        proc = subprocess.Popen(["true"])
        self.until(lambda: (ap.read_proc(proc.pid) or ap.read_proc(os.getpid())).state == "Z")
        self.assertFalse(ap.pid_alive(proc.pid, ap.read_proc(proc.pid).start_ticks))
        proc.wait()

    def test_job_state_and_group_members(self):
        decoy = self.decoy("tokstate")
        info = ap.read_proc(decoy.pid)
        rec = ap.JobRecord("s", "", decoy.pid, decoy.pid, decoy.pid, info.start_ticks, "/", ["x"], "/l", 0.0, "u")
        self.assertEqual(ap.job_state(rec), "running")
        self.assertEqual([m.pid for m in ap.group_members(ap.snapshot(), decoy.pid, decoy.pid)], [decoy.pid])
        self.assertEqual(ap.group_members(ap.snapshot(), decoy.pid, decoy.pid + 1), [])
        rec.start_ticks += 1
        self.assertEqual(ap.job_state(rec), "lost")
        rec.exit_code = 0
        self.assertEqual(ap.job_state(rec), "exited")


class WaitTests(ProcTestCase):
    """wait_pid, wait_job, wait_file, cmd_wait: never a pattern match on a command line."""

    def test_wait_pid_returns_when_the_pid_ends(self):
        proc = self.spawn("sleep 1")
        started = time.monotonic()
        result = ap.wait_pid(proc.pid, timeout=20, interval=0.05)
        self.assertEqual(result.code, 0, result.message)
        self.assertLess(time.monotonic() - started, 10)

    def test_wait_pid_times_out_with_75(self):
        decoy = self.decoy("toktimeout")
        result = ap.wait_pid(decoy.pid, timeout=0.3, interval=0.05)
        self.assertEqual(result.code, 75)
        self.assertIn("still running", result.message)

    def test_wait_pid_on_a_reused_pid_reports_the_original_gone(self):
        decoy = self.decoy("tokreuse")
        wrong = ap.read_proc(decoy.pid).start_ticks - 1000
        result = ap.wait_pid(decoy.pid, start_ticks=wrong, timeout=5, interval=0.05)
        self.assertEqual(result.code, 0)
        self.assertIn("was reused", result.message)
        self.assertNotIn("still running", result.message)
        self.assertTrue(self.alive(decoy))  # and nothing was signalled

    def test_wait_pid_with_a_start_time_that_cannot_be_this_pid_is_an_error_not_done(self):
        decoy = self.decoy("tokfuture")
        later = ap.read_proc(decoy.pid).start_ticks + 1
        with self.assertRaises(ap.ProcError) as caught:
            ap.wait_pid(decoy.pid, start_ticks=later, timeout=5, interval=0.05)
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("ticks=", str(caught.exception))

    def test_wait_pid_unknown_pid_is_already_gone(self):
        self.assertIn("already gone", ap.wait_pid(2 ** 22 + 99).message)

    def test_wait_pid_refuses_its_own_ancestry(self):
        done = cli("wait", "--pid", str(os.getpid()), "--timeout", "2")
        self.assertEqual(done.returncode, 3)
        self.assertIn("watching the watcher", done.stdout)
        self.assertEqual(ap.wait_pid(os.getppid(), timeout=0.1).code, 3)

    def test_a_waiter_loop_whose_own_text_holds_the_pattern_ends(self):
        """The shape that never ended on 2026-11-08: `until ! <search for TOKEN>; do sleep; done` in a bash -c that
        itself contains TOKEN."""
        decoy = self.decoy("tokselfwait")
        loop = (f"until ! {sys.executable} {TOOL} find --quiet --cmd-contains tokselfwait; do sleep 0.2; done; echo ended")
        waiter = subprocess.Popen(["bash", "-c", loop], stdout=subprocess.PIPE, text=True,
                                  env=dict(os.environ, **self.env))
        self.spawned.append(waiter)
        time.sleep(1.0)
        self.assertIsNone(waiter.poll(), "the waiter must still wait while the decoy runs (and not match itself and end)")
        decoy.kill()
        decoy.wait()
        self.assertEqual(waiter.wait(timeout=20), 0)
        self.assertEqual(waiter.stdout.read().strip(), "ended")

    def test_wait_file_exists_contains_fails_and_times_out(self):
        path = os.path.join(self.tmp, "out.log")
        timer = threading.Timer(0.4, lambda: open(path, "w").write("starting\n"))
        timer.start()
        self.assertEqual(ap.wait_file(path, timeout=10, interval=0.05).code, 0)
        timer.join()
        threading.Timer(0.4, lambda: open(path, "a").write("RESULT=ok\n")).start()
        self.assertEqual(ap.wait_file(path, contains=r"RESULT=\w+", timeout=10, interval=0.05).code, 0)
        with open(path, "a") as handle:
            handle.write("FATAL: boom\n")
        self.assertEqual(ap.wait_file(path, contains=r"^RESULT=ok$", timeout=5, interval=0.05).code, 0)  # per line
        self.assertEqual(ap.wait_file(path, contains="NEVER", fail_contains="FATAL", timeout=5, interval=0.05).code, 1)
        self.assertEqual(ap.wait_file(path, contains="NEVER", timeout=0.3, interval=0.05).code, 75)
        self.assertEqual(ap.wait_file(os.path.join(self.tmp, "absent"), timeout=0.3, interval=0.05).code, 75)

    def test_wait_file_with_a_bad_regular_expression_is_a_usage_error_not_a_traceback(self):
        path = os.path.join(self.tmp, "x.log")
        for kwargs in ({"contains": "("}, {"fail_contains": "[a-"}):
            with self.assertRaises(ap.ProcError, msg=repr(kwargs)):
                ap.wait_file(path, timeout=0.1, interval=0.05, **kwargs)
        done = cli("wait", "--file", path, "--contains", "(", "--timeout", "1")
        self.assertEqual(done.returncode, 2)
        self.assertNotIn("Traceback", done.stderr)

    def test_wait_job_whose_record_vanishes_reports_it_instead_of_crashing(self):
        rec = ap.JobRecord("vanish", "", os.getpid(), os.getpid(), os.getpid(), ap.read_proc(os.getpid()).start_ticks, "/w", ["x"], "/l", 1.0, "bruce")
        ap.write_record(rec, self.jobs)
        calls = []

        def sleeper(seconds):
            calls.append(seconds)
            os.remove(ap.record_path("vanish", self.jobs))

        result = ap.wait_job("vanish", timeout=5, interval=0.01, sleep=sleeper, directory=self.jobs)
        self.assertEqual(result.code, 1)
        self.assertIn("disappeared", result.message)

    def test_wait_file_fails_when_the_producer_dies_first(self):
        producer = self.spawn("sleep 0.5")
        result = ap.wait_file(os.path.join(self.tmp, "never"), while_pid=producer.pid, timeout=20, interval=0.05)
        self.assertEqual(result.code, 1)
        self.assertIn("ended before", result.message)

    def test_cmd_wait_needs_exactly_one_target(self):
        out = io.StringIO()
        with self.assertRaises(ap.ProcError):
            ap.cmd_wait(ap.parse_args(["wait"]), out)
        with self.assertRaises(ap.ProcError):
            ap.cmd_wait(ap.parse_args(["wait", "--pid", "1", "--job", "x"]), out)
        path = os.path.join(self.tmp, "there")
        open(path, "w").close()
        self.assertEqual(ap.cmd_wait(ap.parse_args(["wait", "--file", path]), out), 0)
        self.assertIn("ready", out.getvalue())

    def test_wait_job_unknown_is_a_usage_error(self):
        with self.assertRaises(ap.ProcError):
            ap.wait_job("nosuchjob", directory=self.jobs)


def stat_ticks(pid):
    """Field 22 of /proc/PID/stat read directly (not through agent_procs), or None when the pid is gone."""
    try:
        with open(f"/proc/{pid}/stat") as handle:
            raw = handle.read()
    except OSError:
        return None
    return int(raw[raw.rindex(")") + 2:].split()[19])


class WaitStartTimeTests(ProcTestCase):
    """`wait --pid P --start-time T` through the real command line, for processes started in different ways.

    Regression of 2026-11-09: a start time that did not equal the process's was taken for "pid reused, the original is gone"
    and `wait` returned 0 at once while the target (a long gate_snapshot.py create) was still running."""

    LIFETIME = 2.5

    def started(self, how):
        """(pid to wait on, the process that ends, Popen to clean up) for a long-lived child started the way `how` says."""
        code = f"import time; time.sleep({self.LIFETIME})"
        if how == "plain":
            proc = subprocess.Popen([sys.executable, "-c", code])
        elif how == "nohup":
            proc = subprocess.Popen(["nohup", sys.executable, "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif how == "setsid":  # from a plain parent setsid execs in place
            proc = subprocess.Popen(["setsid", sys.executable, "-c", code])
        elif how == "shell":  # bash runs the last command of -c in the same pid
            proc = subprocess.Popen(["bash", "-c", f"{sys.executable} -c '{code}'"])
        elif how == "shell-wrapper-with-child":  # the shell stays as the parent of the real work: wait on the shell
            proc = subprocess.Popen(["bash", "-c", f"{sys.executable} -c '{code}'; true"])
        else:
            raise AssertionError(how)
        self.spawned.append(proc)
        self.until(lambda: stat_ticks(proc.pid) is not None)
        return proc

    def reparented(self):
        """A python sleeper started by a shell that exits at once: its parent is gone, init (or a subreaper) adopts it."""
        out = subprocess.run(["bash", "-c", f"{sys.executable} -c 'import time; time.sleep({self.LIFETIME})' "
                              f"</dev/null >/dev/null 2>&1 & echo $!"], capture_output=True, text=True, timeout=20)
        pid = int(out.stdout.strip())
        self.until(lambda: stat_ticks(pid) is not None)
        return pid

    def wait_cli(self, pid, start, timeout="30"):
        began = time.monotonic()
        done = cli("wait", "--pid", str(pid), "--start-time", str(start), "--timeout", timeout, "--interval", "0.1",
                   env=self.env)
        return done, time.monotonic() - began

    def test_waits_until_the_process_ends_for_every_way_of_starting_it(self):
        for how in ("plain", "nohup", "setsid", "shell", "shell-wrapper-with-child"):
            with self.subTest(how=how):
                proc = self.started(how)
                ticks = stat_ticks(proc.pid)  # field 22 as read by hand
                done, took = self.wait_cli(proc.pid, ticks)
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertIn("ended", done.stdout)
                self.assertGreater(took, self.LIFETIME - 1.5, f"returned after {took:.2f}s, the process lives {self.LIFETIME}s")
                self.assertIsNotNone(proc.wait(timeout=5))

    def test_waits_for_a_reparented_process(self):
        pid = self.reparented()
        done, took = self.wait_cli(pid, stat_ticks(pid))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertGreater(took, self.LIFETIME - 1.5)
        self.assertIsNone(stat_ticks(pid))

    def test_accepts_every_format_find_prints(self):
        decoy = self.decoy("tokformats")
        line = cli("find", "--cmd-contains", "tokformats", env=self.env).stdout.split()
        fields = dict(part.split("=", 1) for part in line if "=" in part and part.split("=", 1)[0] in ("start", "ticks"))
        for given in (fields["ticks"], f"ticks={fields['ticks']}", fields["start"], f"start={fields['start']}"):
            with self.subTest(given=given):
                done = cli("wait", "--pid", str(decoy.pid), "--start-time", given, "--timeout", "0.5", "--interval", "0.1",
                           env=self.env)
                self.assertEqual(done.returncode, 75, done.stdout + done.stderr)
                self.assertIn("still running", done.stdout)
        self.assertTrue(self.alive(decoy))

    def test_a_wrong_start_time_is_an_error_naming_the_format_never_done(self):
        decoy = self.decoy("tokwrongfmt")
        ticks = stat_ticks(decoy.pid)
        now_ticks = int((time.time() - ap.boot_time()) * ap.CLK_TCK)
        wrong = {
            "one tick later": str(ticks + 1),
            "seconds since boot": str(ticks // ap.CLK_TCK),
            "epoch seconds": str(int(time.time())),
            "epoch milliseconds": str(int(time.time() * 1000)),
            "a later start= time": ap.format_start(now_ticks + 10 * ap.CLK_TCK),
            "not a number": "soon",
            "empty": "",
            "negative": "-5",
            "hex": hex(ticks),
        }
        for label, value in wrong.items():
            with self.subTest(label=label):
                done = cli("wait", "--pid", str(decoy.pid), "--start-time", value, "--timeout", "0.5", "--interval", "0.1",
                           env=self.env)
                self.assertEqual(done.returncode, 2, f"{label}: {done.stdout}{done.stderr}")
                self.assertNotIn("ended", done.stdout)
                self.assertNotIn("gone", done.stdout)
                if label != "negative":  # argparse itself refuses "-5" as a value, with its own usage text
                    self.assertIn("ticks=", done.stderr)
        self.assertTrue(self.alive(decoy))

    def test_a_gone_pid_with_a_plausible_start_time_is_done(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        self.spawned.append(proc)
        ticks = stat_ticks(proc.pid)
        proc.wait(timeout=10)
        done, took = self.wait_cli(proc.pid, ticks)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("already gone", done.stdout)
        self.assertLess(took, 10)

    def test_a_gone_pid_with_an_impossible_start_time_is_still_an_error(self):
        done, _ = self.wait_cli(2 ** 22 + 99, int(time.time() * 1000))
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)

    def test_a_reused_pid_is_done_and_never_still_running(self):
        """Pid reuse simulated: the pid now holds a process that started after the one the caller named."""
        decoy = self.decoy("tokreused2")
        earlier = stat_ticks(decoy.pid) - 3 * ap.CLK_TCK - 1  # the "original" ended; this pid belongs to someone newer
        done, took = self.wait_cli(decoy.pid, earlier)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("was reused", done.stdout)
        self.assertNotIn("still running", done.stdout)
        self.assertLess(took, 10)
        self.assertTrue(self.alive(decoy))  # never signalled

    def test_a_wrapper_that_exits_is_done_while_the_real_work_runs(self):
        """The pid of a wrapper (`nohup sh -c 'work &'`, `setsid` run by a group leader) ends at once while the work goes on.
        `wait` is right to say that pid ended: the documentation says to wait on the worker's own pid (found with `find`)
        or to start it with `run`."""
        wrapper = subprocess.Popen(["bash", "-c", f"{sys.executable} -c 'import time; time.sleep(3)' </dev/null >/dev/null 2>&1 &"])
        self.spawned.append(wrapper)
        wrapper_ticks = stat_ticks(wrapper.pid)
        wrapper.wait(timeout=10)
        done = cli("wait", "--pid", str(wrapper.pid), "--start-time", str(wrapper_ticks), "--timeout", "1", env=self.env)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("already gone", done.stdout)


class WaitSelectorTests(ProcTestCase):
    """`wait` by selector: one match waits, several is an error (or --all), none is an error (or --allow-missing)."""

    def sleeper(self, token, seconds):
        proc = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})", token], start_new_session=True)
        self.spawned.append(proc)
        self.until(lambda: ap.read_proc(proc.pid) is not None and token in " ".join(ap.read_proc(proc.pid).argv))
        return proc

    def wait_sel(self, *args):
        began = time.monotonic()
        done = cli("wait", *args, "--interval", "0.1", env=self.env)
        return done, time.monotonic() - began

    def test_one_match_waits_until_it_ends(self):
        self.sleeper("toksel1", 2)
        done, took = self.wait_sel("--cmd-contains", "toksel1", "--timeout", "30")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertGreater(took, 1.0)

    def test_two_matches_is_an_error_listing_both_and_how_to_name_one(self):
        first = self.sleeper("toksel2", 30)
        second = self.sleeper("toksel2", 30)
        done, took = self.wait_sel("--cmd-contains", "toksel2", "--timeout", "30")
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertLess(took, 10)
        self.assertIn("ambiguous: 2 matches", done.stderr)
        for proc in (first, second):
            self.assertIn(f"pid={proc.pid} ticks={stat_ticks(proc.pid)}", done.stderr)
        self.assertIn("user=", done.stderr)
        self.assertIn("--pid P --start-time T", done.stderr)
        self.assertTrue(self.alive(first) and self.alive(second))

    def test_all_waits_for_every_match(self):
        self.sleeper("toksel3", 1.5)
        self.sleeper("toksel3", 3)
        done, took = self.wait_sel("--cmd-contains", "toksel3", "--all", "--timeout", "30")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertGreater(took, 2.0, "must wait for the longer one too")

    def test_all_times_out_while_one_still_runs(self):
        self.sleeper("toksel3b", 1)
        self.sleeper("toksel3b", 60)
        done, _ = self.wait_sel("--cmd-contains", "toksel3b", "--all", "--timeout", "2")
        self.assertEqual(done.returncode, 75, done.stdout + done.stderr)

    def test_no_match_is_an_error_unless_allowed(self):
        done, took = self.wait_sel("--cmd-contains", "tokselnothingruns", "--timeout", "30")
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn("nothing matches", done.stderr)
        self.assertLess(took, 10)
        allowed, _ = self.wait_sel("--cmd-contains", "tokselnothingruns", "--allow-missing", "--timeout", "30")
        self.assertEqual(allowed.returncode, 0, allowed.stdout + allowed.stderr)

    def test_the_waiter_its_ancestors_and_its_own_shell_never_match(self):
        """The text of the selector sits in the waiter's own command line, in the `bash -c` that holds it and in this test's
        runner command line: none of them may count, so a selector that only they match is 'nothing matches'."""
        done = subprocess.run(["bash", "-c", f"{sys.executable} {TOOL} wait --cmd-contains tokselselfonly --timeout 5"],
                              capture_output=True, text=True, env=dict(os.environ, **self.env), timeout=30)
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn("nothing matches", done.stderr)

    def test_a_forked_copy_of_the_waiters_shell_never_matches(self):
        """`wait --cmd-contains X | tail` in a shell: the pipeline's subshell copy holds the whole script text (and X)."""
        script = (f"for i in 1; do {sys.executable} {TOOL} wait --cmd-contains tokselclone --timeout 5 2>&1 | cat; done; "
                  "# tokselclone")
        done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=dict(os.environ, **self.env), timeout=30)
        self.assertIn("nothing matches", done.stdout)
        clone = ap.ProcInfo(900001, 900000, 1, 1, 0, "u", "S", 1, "bash", ("bash", "-c", "t"), None)
        anc = ap.ProcInfo(900000, 1, 1, 1, 0, "u", "S", 1, "bash", ("bash", "-c", "t"), None)
        other = ap.ProcInfo(900002, 900000, 1, 1, 0, "u", "S", 1, "bash", ("bash", "-c", "different"), None)
        fake = {900000: anc, 900001: clone, 900002: other}
        self.assertEqual(ap.shell_clones(fake, {900000}), {900001})

    def test_a_selector_wait_never_signals_and_all_flags_need_a_selector(self):
        victim = self.sleeper("toksel6", 30)
        done = cli("wait", "--pid", str(victim.pid), "--all", "--timeout", "1", env=self.env)
        self.assertEqual(done.returncode, 2)
        self.assertIn("selector", done.stderr)
        self.assertTrue(self.alive(victim))
        both = cli("wait", "--cmd-contains", "toksel6", "--job", "x", env=self.env)
        self.assertEqual(both.returncode, 2)
        self.assertTrue(self.alive(victim))


class JobTests(ProcTestCase):
    """start_job, wrapper_main, wait_job, stop_job, cmd_run, cmd_jobs, cmd_stop, forward_signal."""

    def test_run_wait_success_and_failure_exit_codes_and_log(self):
        done = cli("run", "okjob", "--tag", "agent-x", "--", "sh", "-c", "echo hello-log; exit 0", env=self.env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("job okjob started", done.stdout)
        self.assertEqual(cli("wait", "--job", "okjob", "--timeout", "30", env=self.env).returncode, 0)
        done = cli("run", "badjob", "--", "sh", "-c", "echo oops >&2; exit 3", env=self.env)
        self.assertEqual(cli("wait", "--job", "badjob", "--timeout", "30", env=self.env).returncode, 1)
        rec = ap.read_record("badjob", self.jobs)
        self.assertEqual(rec.exit_code, 3)
        self.assertEqual(ap.read_record("okjob", self.jobs).tag, "agent-x")
        with open(ap.read_record("okjob", self.jobs).log) as handle:
            self.assertIn("hello-log", handle.read())
        with open(rec.log) as handle:
            self.assertIn("oops", handle.read())  # standard error goes to the log too

    def test_start_job_refuses_a_wrapper_whose_start_time_cannot_be_read(self):
        with mock.patch.object(ap, "read_proc", return_value=None):
            with self.assertRaises(ap.ProcError):
                ap.start_job("noticks", ["true"], directory=self.jobs)
        self.assertIsNone(ap.read_record("noticks", self.jobs))

    def test_a_job_runs_in_its_own_session_and_group(self):
        done = cli("run", "sess", "--", "sleep", "60", env=self.env)
        self.assertEqual(done.returncode, 0, done.stderr)
        rec = ap.read_record("sess", self.jobs)
        info = ap.read_proc(rec.pid)
        self.assertEqual((info.pgid, info.sid), (rec.pid, rec.pid))
        self.assertNotEqual(info.sid, ap.read_proc(os.getpid()).sid)
        self.assertEqual(rec.start_ticks, info.start_ticks)

    def test_signal_death_is_recorded_as_128_plus_n_and_missing_program_as_127(self):
        cli("run", "sigdeath", "--", "sh", "-c", "kill -TERM $$", env=self.env)
        self.assertEqual(cli("wait", "--job", "sigdeath", "--timeout", "30", env=self.env).returncode, 1)
        self.assertEqual(ap.read_record("sigdeath", self.jobs).exit_code, 128 + 15)
        cli("run", "nosuch", "--", "/nonexistent/program", env=self.env)
        self.assertEqual(cli("wait", "--job", "nosuch", "--timeout", "30", env=self.env).returncode, 1)
        self.assertEqual(ap.read_record("nosuch", self.jobs).exit_code, 127)

    def test_duplicate_running_name_is_refused_but_a_finished_name_can_be_reused(self):
        self.assertEqual(cli("run", "dup", "--", "sleep", "60", env=self.env).returncode, 0)
        again = cli("run", "dup", "--", "sleep", "60", env=self.env)
        self.assertEqual(again.returncode, 1)
        self.assertIn("already running", again.stderr)
        self.assertEqual(cli("stop", "dup", "--grace", "10", env=self.env).returncode, 0)
        self.assertEqual(cli("run", "dup", "--", "true", env=self.env).returncode, 0)
        self.assertEqual(cli("wait", "--job", "dup", "--timeout", "30", env=self.env).returncode, 0)

    def test_a_stray_file_in_the_jobs_directory_does_not_break_listing(self):
        with open(os.path.join(self.jobs, ".stray.json"), "w") as handle:
            handle.write("{}")
        self.assertEqual(cli("run", "okstray", "--", "true", env=self.env).returncode, 0)
        self.assertEqual([rec.name for rec in ap.list_records(self.jobs)], ["okstray"])
        self.assertEqual(cli("jobs", env=self.env).returncode, 0)

    def test_a_missing_working_directory_is_a_usage_error_and_starts_nothing(self):
        done = cli("run", "nodir", "--cwd", os.path.join(self.tmp, "absent"), "--", "true", env=self.env)
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn("not a directory", done.stderr)
        self.assertNotIn("Traceback", done.stderr)
        self.assertIsNone(ap.read_record("nodir", self.jobs))

    def test_bad_names_and_empty_commands_are_refused(self):
        self.assertEqual(cli("run", "../evil", "--", "true", env=self.env).returncode, 2)
        self.assertEqual(cli("run", "nocmd", env=self.env).returncode, 2)

    def test_stop_kills_the_whole_group_with_sigterm_and_records_it(self):
        cli("run", "tree", "--", "sh", "-c", "sleep 300 & sleep 300 & wait", env=self.env)
        rec = ap.read_record("tree", self.jobs)
        self.until(lambda: len(ap.group_members(ap.snapshot(), rec.pgid, rec.sid)) >= 3)
        done = cli("stop", "tree", "--grace", "10", env=self.env)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("sent SIGTERM", done.stdout)
        self.assertNotIn("SIGKILL", done.stdout)
        self.assertEqual(ap.group_members(ap.snapshot(), rec.pgid, rec.sid), [])
        self.assertEqual(ap.read_record("tree", self.jobs).stopped, "term")

    def test_sigkill_needs_force_and_only_the_verified_group_is_hit(self):
        bystander = self.decoy("tokbystander")
        cli("run", "stubborn", "--", "sh", "-c", 'trap "" TERM; while :; do sleep 1; done', env=self.env)
        rec = ap.read_record("stubborn", self.jobs)
        self.until(lambda: len(ap.group_members(ap.snapshot(), rec.pgid, rec.sid)) >= 2)
        soft = cli("stop", "stubborn", "--grace", "0.5", env=self.env)
        self.assertEqual(soft.returncode, 1)
        self.assertIn("--force", soft.stdout)
        self.assertTrue(ap.group_members(ap.snapshot(), rec.pgid, rec.sid))
        hard = cli("stop", "stubborn", "--grace", "0.5", "--force", env=self.env)
        self.assertEqual(hard.returncode, 0, hard.stdout)
        self.assertIn("SIGKILL", hard.stdout)
        self.assertEqual(ap.read_record("stubborn", self.jobs).stopped, "kill")
        self.assertEqual(ap.group_members(ap.snapshot(), rec.pgid, rec.sid), [])
        self.assertTrue(self.alive(bystander))

    def test_stop_with_a_stale_record_signals_nothing(self):
        """Pid reuse: the record names a pid and group that now belong to an unrelated process in another session."""
        victim = self.decoy("tokvictim")
        info = ap.read_proc(victim.pid)
        stale = ap.JobRecord("stale", "", victim.pid, victim.pid, victim.pid + 7, info.start_ticks - 5, "/", ["x"], "/l",
                             0.0, "u")
        ap.write_record(stale, self.jobs)
        done = cli("stop", "stale", env=self.env)
        self.assertEqual(done.returncode, 0)
        self.assertIn("nothing to stop", done.stdout)
        self.assertTrue(self.alive(victim))

    def test_stop_with_a_reused_group_leader_pid_signals_nothing(self):
        """The recorded pid is now a session leader of an unrelated process: same group and session numbers, other start time."""
        victim = self.decoy("tokreused")
        info = ap.read_proc(victim.pid)
        self.assertEqual((info.pgid, info.sid), (victim.pid, victim.pid))
        stale = ap.JobRecord("reused", "", victim.pid, victim.pid, victim.pid, info.start_ticks - 5, "/", ["x"], "/l", 0.0, "u")
        ap.write_record(stale, self.jobs)
        out = io.StringIO()
        self.assertEqual(ap.stop_job("reused", out=out, directory=self.jobs), 0)
        self.assertIn("nothing to stop", out.getvalue())
        self.assertTrue(self.alive(victim))

    def test_stop_unknown_job_is_a_usage_error(self):
        self.assertEqual(cli("stop", "nosuchjob", env=self.env).returncode, 2)

    def test_stop_refuses_a_job_holding_a_fake_postgres_unless_the_owner_flag(self):
        cli("run", "pgjob", "--", "bash", "-c", 'exec -a "postgres: hams_test tokpgjob [local] idle" sleep 300', env=self.env)
        rec = ap.read_record("pgjob", self.jobs)
        self.until(lambda: any("postgres" in " ".join(m.argv) for m in ap.group_members(ap.snapshot(), rec.pgid, rec.sid)))
        refused = cli("stop", "pgjob", env=self.env)
        self.assertEqual(refused.returncode, 3, refused.stdout + refused.stderr)
        self.assertIn("REFUSED", refused.stderr)
        self.assertTrue(ap.group_members(ap.snapshot(), rec.pgid, rec.sid))
        allowed = cli("stop", "pgjob", "--i-am-the-owner-and-checked", "--grace", "10", env=self.env)
        self.assertEqual(allowed.returncode, 0, allowed.stdout + allowed.stderr)
        self.assertEqual(ap.group_members(ap.snapshot(), rec.pgid, rec.sid), [])

    def test_stop_refuses_a_group_that_holds_the_callers_own_shell(self):
        mine = ap.JobRecord("mine", "", os.getpgrp(), os.getpgrp(), ap.read_proc(os.getpid()).sid, 1, "/", ["x"], "/l", 0.0, "u")
        ap.write_record(mine, self.jobs)
        with self.assertRaises(ap.ProcError) as caught:
            ap.stop_job("mine", out=io.StringIO(), directory=self.jobs)
        self.assertEqual(caught.exception.code, 3)

    def test_jobs_lists_states_and_json(self):
        cli("run", "listed", "--", "sh", "-c", "exit 0", env=self.env)
        cli("wait", "--job", "listed", "--timeout", "30", env=self.env)
        cli("run", "alive", "--tag", "t1", "--", "sleep", "60", env=self.env)
        text = cli("jobs", env=self.env).stdout
        self.assertRegex(text, r"listed exited code=0")
        self.assertRegex(text, r"alive running pid=\d+")
        rows = {row["name"]: row for row in json.loads(cli("jobs", "--json", env=self.env).stdout)}
        self.assertEqual(rows["alive"]["state"], "running")
        self.assertEqual(rows["listed"]["state"], "exited")
        self.assertEqual(rows["alive"]["tag"], "t1")

    def test_a_killed_wrapper_is_reported_lost_and_wait_job_fails(self):
        cli("run", "gone", "--", "sleep", "60", env=self.env)
        rec = ap.read_record("gone", self.jobs)
        os.killpg(rec.pgid, signal.SIGKILL)
        self.until(lambda: not ap.pid_alive(rec.pid, rec.start_ticks))
        self.assertEqual(ap.job_state(rec), "lost")
        self.assertEqual(cli("wait", "--job", "gone", "--timeout", "5", env=self.env).returncode, 1)

    def test_wait_job_times_out_with_75(self):
        cli("run", "slow", "--", "sleep", "60", env=self.env)
        self.assertEqual(cli("wait", "--job", "slow", "--timeout", "0.5", "--interval", "0.1", env=self.env).returncode, 75)

    def test_forward_signal_reaches_the_command_and_tolerates_none(self):
        child = subprocess.Popen(["sleep", "60"])
        try:
            ap.forward_signal([None], signal.SIGTERM, None)
            ap.forward_signal([], signal.SIGTERM, None)
            self.assertIsNone(child.poll())
            ap.forward_signal([child], signal.SIGTERM, None)
            self.assertEqual(child.wait(timeout=10), -signal.SIGTERM)
            ap.forward_signal([child], signal.SIGTERM, None)  # already ended: no error
        finally:
            child.kill()
            child.wait()

    def test_signal_same_process_signals_only_the_same_process(self):
        child = subprocess.Popen(["sleep", "60"])
        try:
            info = ap.read_proc(child.pid)
            with self.assertRaises(ProcessLookupError):
                ap.signal_same_process(child.pid, info.start_ticks + 1, signal.SIGTERM)  # "reused" pid
            self.assertIsNone(child.poll())
            ap.signal_same_process(child.pid, info.start_ticks, signal.SIGTERM)
            self.assertEqual(child.wait(timeout=10), -signal.SIGTERM)
        finally:
            child.kill()
            child.wait()

    def test_wrapper_forwards_quit_and_usr1_to_the_command(self):
        for name in ("QUIT", "USR1"):
            job = "fw" + name
            ready = os.path.join(self.tmp, "ready-" + name)
            cli("run", job, "--", "sh", "-c", f"touch {ready}; exec sleep 60", env=self.env)
            rec = ap.read_record(job, self.jobs)
            self.until(lambda: os.path.exists(ready), 60)  # the command is running (the wrapper had started it)
            ap.signal_same_process(rec.pid, rec.start_ticks, ap.ALLOWED_SIGNALS[name])
            self.until(lambda: ap.read_record(job, self.jobs).exit_code is not None, 30)
            self.assertEqual(ap.read_record(job, self.jobs).exit_code, 128 + int(ap.ALLOWED_SIGNALS[name]))

    def test_wrapper_holds_a_signal_that_arrives_while_the_command_is_starting(self):
        """Tests [@ANCHOR: agent_procs:wrapper_main] and [@ANCHOR: agent_procs:forward_signal]: a signal sent to the wrapper
        between the fork and the exec of the command (slowed here by a sleep in the start hook) still reaches the command."""
        started = os.path.join(self.tmp, "starting")
        script = (
            "import sys, time, pathlib, agent_procs as ap\n"
            "orig = ap._die_with_parent\n"
            f"def slow():\n    pathlib.Path({started!r}).touch()\n    time.sleep(1)\n    orig()\n"
            "ap._die_with_parent = slow\n"
            "sys.exit(ap.wrapper_main('early', ['sleep', '60'], None))\n")
        wrapper = subprocess.Popen([sys.executable, "-c", script], cwd=os.path.dirname(TOOL), stdin=subprocess.PIPE, text=True,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.spawned.append(wrapper)
        wrapper.stdin.write("go\n")
        wrapper.stdin.flush()
        self.until(lambda: os.path.exists(started), 30)
        wrapper.send_signal(signal.SIGUSR1)
        self.assertEqual(wrapper.wait(timeout=30), 128 + int(signal.SIGUSR1))

    def test_wrapper_main_refuses_to_run_without_the_go_line(self):
        code = subprocess.run([sys.executable, TOOL, "_wrap", "never", "--", "touch", os.path.join(self.tmp, "ran")],
                              input="", text=True, env=dict(os.environ, **self.env)).returncode
        self.assertEqual(code, 126)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "ran")))

    def test_cmd_run_cmd_jobs_cmd_stop_in_process(self):
        out = io.StringIO()
        args = ap.parse_args(["run", "inproc", "--tag", "me", "--", "sleep", "60"])
        self.assertEqual(ap.cmd_run(args, out), 0)
        self.assertIn("wait: agent_procs.py wait --job inproc", out.getvalue())
        out = io.StringIO()
        self.assertEqual(ap.cmd_jobs(ap.parse_args(["jobs"]), out), 0)
        self.assertIn("inproc running", out.getvalue())
        out = io.StringIO()
        self.assertEqual(ap.cmd_stop(ap.parse_args(["stop", "inproc", "--grace", "10"]), out), 0)
        self.assertIn("stopped", out.getvalue())
        for rec in ap.list_records(self.jobs):  # the job was our child: reap it
            try:
                os.waitpid(rec.pid, os.WNOHANG)
            except ChildProcessError:
                pass


class LimitTests(ProcTestCase):
    """run --timeout / --mem-max / --nice, the parent-death rule and the limits shown by jobs."""

    def group_gone(self, rec):
        return not ap.group_members(ap.snapshot(), rec.pgid, rec.sid)

    def test_a_job_that_sleeps_past_its_timeout_is_stopped_and_recorded(self):
        done = cli("run", "slow", "--timeout", "2", "--mem-max", "none", "--", "sh", "-c", "sleep 60; sleep 61", env=self.env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(cli("wait", "--job", "slow", "--timeout", "30", env=self.env).returncode, 1)
        rec = ap.read_record("slow", self.jobs)
        self.assertEqual(rec.exit_reason, "timeout")
        self.assertEqual(rec.timeout, 2)
        self.until(lambda: self.group_gone(rec))

    def test_a_job_that_ignores_sigterm_is_killed_after_the_grace(self):
        rec = ap.start_job("stubborn", ["sh", "-c", "trap '' TERM; while :; do sleep 1; done"], timeout=1, mem_max=None,
                           directory=self.jobs, kill_grace=1)
        self.until(lambda: ap.read_record("stubborn", self.jobs).exit_reason == "timeout", 30)
        self.until(lambda: self.group_gone(rec))

    def test_a_job_over_its_memory_cap_is_stopped(self):
        code = "import time\nx = []\nwhile True:\n    x.append(b'x' * 50_000_000)\n    time.sleep(0.01)\n"
        done = cli("run", "hog", "--timeout", "60", "--mem-max", "200M", "--", sys.executable, "-c", code, env=self.env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(cli("wait", "--job", "hog", "--timeout", "50", env=self.env).returncode, 1)
        rec = ap.read_record("hog", self.jobs)
        self.assertNotEqual(rec.exit_code, 0)
        self.assertNotEqual(rec.exit_reason, "timeout")
        self.until(lambda: self.group_gone(rec))

    def test_defaults_and_opt_outs_are_recorded_and_shown_by_jobs(self):
        cli("run", "dflt", "--", "true", env=self.env)
        out = cli("run", "optout", "--timeout", "0", "--mem-max", "none", "--", "true", env=self.env).stdout
        self.assertIn("NO TIMEOUT", out)
        self.assertIn("NO MEMORY CAP", out)
        for name in ("dflt", "optout"):
            cli("wait", "--job", name, "--timeout", "30", env=self.env)
        dflt, optout = ap.read_record("dflt", self.jobs), ap.read_record("optout", self.jobs)
        self.assertEqual((dflt.timeout, dflt.mem_max), (1800, "6G"))
        self.assertEqual((optout.timeout, optout.mem_max), (0, None))
        listing = cli("jobs", env=self.env).stdout
        self.assertIn("timeout=1800s mem=6G", listing)
        self.assertIn("timeout=none mem=none", listing)

    def test_the_command_ends_when_its_wrapper_dies(self):
        rec = ap.start_job("orphan", ["sleep", "61.5"], mem_max=None, directory=self.jobs)
        mine = lambda: [i for i in ap.snapshot().values() if i.argv[:2] == ("sleep", "61.5")]  # noqa: E731
        self.until(lambda: mine())
        os.kill(rec.pid, signal.SIGKILL)  # the wrapper of our own job
        self.until(lambda: not mine(), 20)

    def test_parse_size_and_limit_argv(self):
        self.assertEqual(ap.parse_size("6G"), 6 * 1024 ** 3)
        self.assertEqual(ap.parse_size("512M"), 512 * 1024 ** 2)
        with self.assertRaises(ap.ProcError):
            ap.parse_size("lots")
        self.assertEqual(ap.limit_argv(["x"], None, 0), ["x"])
        self.assertEqual(ap.limit_argv(["x"], None, 5), ["nice", "-n", "5", "x"])
        capped = ap.limit_argv(["x"], "1G", 0, have_systemd=True)
        self.assertEqual(capped[:3], ["systemd-run", "--user", "--scope"])
        self.assertIn("MemoryMax=1G", capped)
        self.assertIn("MemorySwapMax=0", capped)
        fallback = ap.limit_argv(["x"], "1G", 0, have_systemd=False)
        self.assertEqual(fallback[0], "sh")
        self.assertIn("ulimit -v 1048576", fallback[2])


class ReapTests(ProcTestCase):
    """reap: orphans of deleted agent worktrees, nothing else."""

    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.tmp, "agent_work")
        os.makedirs(self.root)
        old = ap.AGENT_WORK_ROOT
        ap.AGENT_WORK_ROOT = self.root
        self.addCleanup(setattr, ap, "AGENT_WORK_ROOT", old)

    def work_proc(self, name, token):
        """A python sleeper whose cwd is the directory `name` under the fake agent_work root."""
        path = os.path.join(self.root, name)
        os.makedirs(path)
        proc = subprocess.Popen([sys.executable, "-c", "import time; print('up', flush=True); time.sleep(120)", token],
                                cwd=path, start_new_session=True, stdout=subprocess.PIPE, text=True)
        self.spawned.append(proc)
        self.assertEqual(proc.stdout.readline().strip(), "up")  # python has started (its cwd is read), so deleting the directory cannot break the start
        self.until(lambda: ap.read_proc(proc.pid) is not None and token in " ".join(ap.read_proc(proc.pid).argv))
        return proc, path

    def test_reap_finds_the_deleted_cwd_process_and_only_that(self):
        gone, gone_path = self.work_proc("wt-gone", "tokreap1")
        live, _ = self.work_proc("wt-live", "tokreap2")
        elsewhere = self.decoy("tokreap3", cwd=self.tmp)
        shutil.rmtree(gone_path)
        found = [info.pid for info, _why in ap.find_orphans(ap.snapshot(), min_age=0)]
        self.assertIn(gone.pid, found)
        self.assertNotIn(live.pid, found)
        self.assertNotIn(elsewhere.pid, found)
        out = io.StringIO()
        self.assertEqual(ap.cmd_reap(ap.parse_args(["reap", "--min-age", "0"]), out), 0)
        self.assertIn(str(gone.pid), out.getvalue())
        self.assertTrue(self.alive(gone))  # a dry run signals nothing
        out = io.StringIO()
        self.assertEqual(ap.cmd_reap(ap.parse_args(["reap", "--min-age", "0", "--apply"]), out), 0)
        self.until(lambda: not self.alive(gone))
        self.assertTrue(self.alive(live))
        self.assertTrue(self.alive(elsewhere))

    def test_min_age_keeps_young_processes(self):
        gone, gone_path = self.work_proc("wt-young", "tokreap4")
        shutil.rmtree(gone_path)
        self.assertEqual([i for i, _ in ap.find_orphans(ap.snapshot(), min_age=900) if i.pid == gone.pid], [])

    def test_a_running_recorded_job_is_not_an_orphan_and_a_protected_one_is_never_listed(self):
        rec = ap.start_job("keep", ["sleep", "61.7"], cwd=self.root, mem_max=None, directory=self.jobs)
        fake_pg = subprocess.Popen(["bash", "-c", 'exec -a postgres sleep 120'], cwd=self.root, start_new_session=True)
        self.spawned.append(fake_pg)
        self.until(lambda: ap.read_proc(fake_pg.pid) is not None and ap.read_proc(fake_pg.pid).argv[0] == "postgres")
        shutil.rmtree(self.root)  # every cwd above is now deleted
        found = [i.pid for i, _ in ap.find_orphans(ap.snapshot(), min_age=0)]
        self.assertNotIn(fake_pg.pid, found)
        self.assertEqual([p for p in found if ap.read_proc(p).sid == rec.sid], [])

    def test_orphan_reason_rules(self):
        now = time.time()
        boot = ap.boot_time()
        old_ticks = int((now - 5000 - boot) * ap.CLK_TCK)
        sysd = ap.ProcInfo(777, 1, 777, 777, os.getuid(), "me", "S", 1, "systemd", ("/lib/systemd/systemd", "--user"), "/")
        base = dict(pid=778, pgid=778, sid=778, uid=os.getuid(), user="me", state="S", start_ticks=old_ticks, comm="x",
                    argv=("x",))
        table = {777: sysd}
        child = ap.ProcInfo(ppid=777, cwd=os.path.join(self.root, "wt"), **base)
        self.assertIn("systemd", ap.orphan_reason(child, table, 900, now, boot))
        self.assertIsNone(ap.orphan_reason(ap.ProcInfo(ppid=999, cwd=os.path.join(self.root, "wt"), **base), table, 900, now, boot))
        self.assertIsNone(ap.orphan_reason(ap.ProcInfo(ppid=777, cwd="/home", **base), table, 900, now, boot))
        self.assertIn("deleted", ap.orphan_reason(ap.ProcInfo(ppid=999, cwd=os.path.join(self.root, "wt") + " (deleted)",
                                                              **base), table, 900, now, boot))


class KillPidTests(ProcTestCase):
    """kill_pid, authorize_kill, cmd_kill_pid: one exact verified pid."""

    def test_kill_with_the_right_start_time_ends_that_pid_only(self):
        target = self.decoy("tokkill1")
        other = self.decoy("tokkill2")
        ticks = ap.read_proc(target.pid).start_ticks
        out = io.StringIO()
        self.assertEqual(ap.kill_pid(target.pid, start_ticks=ticks, grace=10, out=out, directory=self.jobs), 0)
        self.assertIn(f"pid={target.pid}", out.getvalue())  # the ps line was printed first
        self.assertIn("start time confirmed", out.getvalue())
        target.wait(timeout=10)
        self.assertTrue(self.alive(other))

    def test_a_reused_pid_wrong_start_time_is_refused_and_survives(self):
        target = self.decoy("tokkill3")
        wrong = ap.read_proc(target.pid).start_ticks - 99
        done = cli("kill-pid", str(target.pid), "--start-time", str(wrong), env=self.env)
        self.assertEqual(done.returncode, 3)
        self.assertIn("was reused", done.stderr)
        self.assertTrue(self.alive(target))

    def test_a_start_time_later_than_the_process_is_a_usage_error_and_survives(self):
        target = self.decoy("tokkill3b")
        wrong = ap.read_proc(target.pid).start_ticks + 99
        done = cli("kill-pid", str(target.pid), "--start-time", str(wrong), env=self.env)
        self.assertEqual(done.returncode, 2)
        self.assertIn("ticks=", done.stderr)
        self.assertTrue(self.alive(target))

    def test_without_proof_of_identity_it_is_refused(self):
        target = self.decoy("tokkill4")
        done = cli("kill-pid", str(target.pid), env=self.env)
        self.assertEqual(done.returncode, 3)
        self.assertIn("neither a recorded job", done.stderr)
        self.assertTrue(self.alive(target))

    def test_cwd_under_authorizes(self):
        sub = os.path.join(self.tmp, "work")
        os.makedirs(sub)
        target = self.decoy("tokkill5", cwd=sub)
        wrong_dir = cli("kill-pid", str(target.pid), "--cwd-under", os.path.join(self.tmp, "elsewhere"), env=self.env)
        self.assertEqual(wrong_dir.returncode, 3)
        self.assertTrue(self.alive(target))
        done = cli("kill-pid", str(target.pid), "--cwd-under", self.tmp, "--grace", "10", env=self.env)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        target.wait(timeout=10)

    def test_a_recorded_job_pid_is_authorized_without_a_start_time(self):
        cli("run", "recorded", "--", "sleep", "60", env=self.env)
        rec = ap.read_record("recorded", self.jobs)
        done = cli("kill-pid", str(rec.pid), "--grace", "10", env=self.env)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("recorded job", done.stdout)

    def test_fake_postgres_is_refused_even_with_the_right_start_time(self):
        proc = self.spawn('exec -a "postgres: hams_test hams_pf_probe [local] idle" sleep 120',
                          "postgres: hams_test hams_pf_probe [local] idle")
        ticks = ap.read_proc(proc.pid).start_ticks
        done = cli("kill-pid", str(proc.pid), "--start-time", str(ticks), "--force", env=self.env)
        self.assertEqual(done.returncode, 3)
        self.assertIn("REFUSED", done.stderr)
        self.assertIn("postgres", done.stderr)
        self.assertTrue(self.alive(proc))
        owner = cli("kill-pid", str(proc.pid), "--start-time", str(ticks), "--i-am-the-owner-and-checked", env=self.env)
        self.assertEqual(owner.returncode, 3)  # the override is for recorded jobs only
        self.assertTrue(self.alive(proc))

    def test_other_protected_processes_are_refused(self):
        for argv0 in ("claude", "python3 tools/test.py -u m", "chrome --user-data-dir=/somewhere/else"):
            proc = self.spawn(f'exec -a "{argv0}" sleep 120', argv0, cwd=self.tmp)
            ticks = ap.read_proc(proc.pid).start_ticks
            elsewhere = dict(self.env, TMPDIR=os.path.join(self.tmp, "another-tmpdir"))  # the cwd is not under it
            done = cli("kill-pid", str(proc.pid), "--start-time", str(ticks), env=elsewhere)
            self.assertEqual(done.returncode, 3, argv0)
            self.assertTrue(self.alive(proc), argv0)

    def test_pid_1_other_users_and_own_ancestry_are_refused(self):
        self.assertEqual(cli("kill-pid", "1", "--start-time", "1", env=self.env).returncode, 3)
        self.assertEqual(cli("kill-pid", "0", "--start-time", "1", env=self.env).returncode, 3)
        root_pid = next(i.pid for i in ap.snapshot().values() if i.uid == 0 and i.pid != 1)
        ticks = ap.read_proc(root_pid).start_ticks
        done = cli("kill-pid", str(root_pid), "--start-time", str(ticks), env=self.env)
        self.assertEqual(done.returncode, 3)
        self.assertIn("owned by", done.stderr)
        parent = cli("kill-pid", str(os.getpid()), "--start-time", "1", env=self.env)  # the test process is its ancestor
        self.assertEqual(parent.returncode, 3)
        self.assertIn("ancestry", parent.stderr)

    def test_only_one_pid_is_accepted(self):
        done = cli("kill-pid", "123", "456", "--start-time", "1", env=self.env)
        self.assertEqual(done.returncode, 2)

    def test_missing_pid_is_already_gone_and_bad_signal_is_a_usage_error(self):
        out = io.StringIO()
        self.assertEqual(ap.kill_pid(2 ** 22 + 77, start_ticks=1, out=out, directory=self.jobs), 0)
        self.assertIn("already gone", out.getvalue())
        with self.assertRaises(ap.ProcError) as caught:
            ap.kill_pid(2 ** 22 + 77, sig="KILL", out=out)
        self.assertEqual(caught.exception.code, 2)

    def test_term_ignoring_pid_needs_force_for_sigkill_and_start_time_is_rechecked(self):
        proc = self.spawn('trap "" TERM; while :; do sleep 1; done')
        info = ap.read_proc(proc.pid)
        out = io.StringIO()
        self.assertEqual(ap.kill_pid(proc.pid, start_ticks=info.start_ticks, grace=0.5, out=out, directory=self.jobs), 1)
        self.assertIn("--force", out.getvalue())
        out = io.StringIO()
        self.assertEqual(ap.kill_pid(proc.pid, start_ticks=info.start_ticks, grace=0.5, force=True, out=out,
                                     directory=self.jobs), 0)
        self.assertIn("SIGKILL", out.getvalue())
        proc.wait(timeout=10)

    def test_authorize_kill_reasons(self):
        decoy = self.decoy("tokauth")
        info = ap.read_proc(decoy.pid)
        self.assertEqual(ap.authorize_kill(info, info.start_ticks, None, self.jobs), "start time confirmed")
        self.assertEqual(ap.authorize_kill(info, None, os.getcwd(), self.jobs).startswith("cwd under"), True)
        with self.assertRaises(ap.ProcError):
            ap.authorize_kill(info, None, None, self.jobs)
        rec = ap.JobRecord("auth", "", decoy.pid, decoy.pid, decoy.pid, info.start_ticks, "/", ["x"], "/l", 0.0, "u")
        ap.write_record(rec, self.jobs)
        self.assertEqual(ap.authorize_kill(info, None, None, self.jobs), "recorded job")

    def test_cmd_kill_pid_in_process(self):
        target = self.decoy("tokcmdkill")
        ticks = ap.read_proc(target.pid).start_ticks
        args = ap.parse_args(["kill-pid", str(target.pid), "--start-time", str(ticks), "--grace", "10"])
        self.assertEqual(ap.cmd_kill_pid(args, io.StringIO()), 0)
        target.wait(timeout=10)


class MainTests(ProcTestCase):
    """main: exit codes and error output."""

    def test_main_returns_codes_and_prints_errors_on_stderr(self):
        self.assertEqual(ap.main(["find", "--cmd-contains", "no-such-token-main-1", "--quiet"]), 1)
        self.assertEqual(ap.main(["wait", "--job", "absent-job"]), 2)
        done = cli("wait", env=self.env)
        self.assertEqual(done.returncode, 2)
        self.assertIn("agent_procs:", done.stderr)
        self.assertEqual(cli(env=self.env).returncode, 2)  # no subcommand

    def test_help_mentions_every_subcommand(self):
        text = cli("--help").stdout
        for word in ("find", "wait", "run", "jobs", "stop", "kill-pid"):
            self.assertIn(word, text)


if __name__ == "__main__":
    unittest.main()

# Class anchors (ADR 0113) exercised by the tests above.
# Tests [@ANCHOR: agent_procs:agent_procs_proc_error_class]
