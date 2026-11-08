#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Regression tests for test.py's run_cmd() and main()'s --mode dispatch.

`test.py` is loaded via importlib rather than `import test` because its own
module name collides with Python's stdlib `test` package -- same technique
`test_test_oom_watchdog.py` already uses in this same directory.

Before this bug-hunt pass, test.py had NO pytest coverage at all outside
OOMWatchdog's own narrow memory-measurement fix (confirmed via
`grep -rl "run_cmd\\b" tools/test_*.py`, which matched nothing) -- these
tests cover the two real bugs this pass found and fixed in run_cmd() and
main(), not an exhaustive suite of the whole file.
"""

import contextlib
import fcntl
import importlib.util
import io
import inspect
import itertools
import os
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_TEST_PY_PATH = os.path.join(_HERE, "test.py")


def _load_test_py_module():
    spec = importlib.util.spec_from_file_location(
        "_hams_test_runner_module_under_test_2", _TEST_PY_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_test_runner = _load_test_py_module()


class DefaultTestTmpdirTests(unittest.TestCase):
    """The Odoo test process keeps its temporary files (and Chrome profiles) on the roomy disk, not the small /tmp partition."""

    def test_an_unset_tmpdir_is_pointed_at_the_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            environ = {}
            self.assertEqual(_test_runner.apply_default_test_tmpdir(environ, directory), directory)
            self.assertEqual(environ["TMPDIR"], directory)

    def test_a_tmpdir_the_caller_already_chose_is_never_overridden(self):
        with tempfile.TemporaryDirectory() as directory:
            environ = {"TMPDIR": "/somewhere/else"}
            self.assertEqual(_test_runner.apply_default_test_tmpdir(environ, directory), "/somewhere/else")
            self.assertEqual(environ["TMPDIR"], "/somewhere/else")

    def test_a_missing_directory_leaves_tmpdir_alone(self):
        environ = {}
        self.assertIsNone(_test_runner.apply_default_test_tmpdir(environ, "/no/such/directory/for/tests"))
        self.assertNotIn("TMPDIR", environ)

    def test_the_default_is_the_odoo_account_temporary_directory(self):
        self.assertEqual(_test_runner.DEFAULT_TEST_TMPDIR, "/var/lib/odoo/tmp")


class OdooCpuLimitTests(unittest.TestCase):
    """The Odoo test process's CPU-time limit is a backstop a full suite cannot reach, and a death by it is reported as
    such, never as a test outcome. It was 20 CPU-minutes with soft == hard: combined runs were SIGKILLed mid-module."""

    def test_the_backstop_is_hours_and_the_soft_limit_comes_first(self):
        self.assertGreaterEqual(_test_runner.ODOO_CPU_LIMIT_SOFT_SECONDS, 6 * 3600)
        self.assertGreater(_test_runner.ODOO_CPU_LIMIT_HARD_SECONDS, _test_runner.ODOO_CPU_LIMIT_SOFT_SECONDS)
        source = open(_TEST_PY_PATH, encoding="utf-8").read()
        self.assertIn("(ODOO_CPU_LIMIT_SOFT_SECONDS, ODOO_CPU_LIMIT_HARD_SECONDS)", source)
        self.assertNotIn("RLIMIT_CPU, (1200, 1200)", source)

    def test_a_child_that_burns_past_a_tiny_limit_is_reported_as_the_cpu_limit(self):
        def tiny_limit():
            resource.setrlimit(resource.RLIMIT_CPU, (1, 3))

        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        proc = subprocess.Popen([sys.executable, "-I", "-c", "while True: pass"], preexec_fn=tiny_limit)
        try:
            returncode = proc.wait(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        cpu = _test_runner.children_cpu_seconds(before, resource.getrusage(resource.RUSAGE_CHILDREN))
        self.assertEqual(returncode, -signal.SIGXCPU, "the soft limit must arrive first, as SIGXCPU")
        self.assertGreaterEqual(cpu, 0.9)
        line = _test_runner.cpu_limit_verdict(returncode, cpu, soft_limit=1)
        self.assertIsNotNone(line)
        self.assertIn("CPU-time limit reached after", line)
        self.assertIn("NOT a test verdict", line)

    def test_a_sigkill_counts_only_near_the_limit_and_an_ordinary_exit_never(self):
        verdict = _test_runner.cpu_limit_verdict
        self.assertIsNotNone(verdict(-signal.SIGKILL, 990, soft_limit=1000))
        self.assertIsNone(verdict(-signal.SIGKILL, 10, soft_limit=1000), "an ordinary kill is not the CPU limit")
        self.assertIsNone(verdict(0, 5000, soft_limit=1000))
        self.assertIsNone(verdict(1, 5000, soft_limit=1000))

    def test_the_run_summary_says_incomplete(self):
        source = open(_TEST_PY_PATH, encoding="utf-8").read()
        self.assertIn("extractor.cpu_limit_line = cpu_limit_line", source)
        self.assertIn("TEST RUN INCOMPLETE", source)


class SignalExitVerdictTests(unittest.TestCase):
    """Tests [@ANCHOR: test:signal_exit_verdict]

    Exit 247 (`sys.exit(-9)`) was unreadable, and a run SIGKILLed after "0 failed" looked like a plain failure (2026-11-04)."""

    RESULT = [{"failed": 0, "errors": 0, "total": 267}]

    def test_a_clean_or_failing_exit_is_unchanged_and_has_no_line(self):
        self.assertEqual(_test_runner.signal_exit_verdict(0, False, None), (0, None))
        self.assertEqual(_test_runner.signal_exit_verdict(1, True, self.RESULT), (1, None))

    def test_a_sigkill_from_the_watchdog_exits_137_and_says_so(self):
        status, line = _test_runner.signal_exit_verdict(-signal.SIGKILL, True, None)
        self.assertEqual(status, 137)
        self.assertIn("killed by SIGKILL", line)
        self.assertIn("OOM watchdog: yes", line)
        self.assertIn("no test verdict exists", line)

    def test_a_kill_after_zero_failed_names_the_verdict_and_stays_a_failure(self):
        status, line = _test_runner.signal_exit_verdict(-signal.SIGKILL, False, self.RESULT)
        self.assertEqual(status, 137)
        self.assertIn("OOM watchdog: no", line)
        self.assertIn("0 failed, 0 error(s) of 267 tests", line)
        self.assertIn("still a failure", line)

    def test_a_real_killed_child_gives_the_same_status_a_shell_reports(self):
        proc = subprocess.Popen(["sleep", "60"])
        proc.kill()
        returncode = proc.wait(timeout=10)
        self.assertEqual(_test_runner.signal_exit_verdict(returncode, False, None)[0], 128 + signal.SIGKILL)

    def test_the_watchdog_flag_is_not_set_until_it_fires(self):
        watchdog = _test_runner.OOMWatchdog(os.getpid(), os.getpid())
        self.assertFalse(watchdog.fired.is_set())


class RabbitMqWrapperTrapTests(unittest.TestCase):
    """The shell that runs RabbitMQ stops its process group on SIGTERM and exits cleanly. Its trap used to re-enter itself
    (its own `kill -TERM 0` reached it) until bash segfaulted on a stack overflow at the end of every test run."""

    def _run_and_terminate(self, script):
        proc = subprocess.Popen(["bash", "-c", script], start_new_session=True)
        try:
            time.sleep(0.5)
            proc.send_signal(signal.SIGTERM)
            return proc.wait(timeout=15), proc.pid
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()

    def test_sigterm_stops_the_server_and_the_shell_exits_zero_without_a_segfault(self):
        returncode, pgid = self._run_and_terminate(_test_runner.rabbitmq_wrapper_script("sleep 30"))
        self.assertEqual(returncode, 0, "the wrapper must exit 0 on SIGTERM, not die of a signal (-11 was the segfault)")
        left = subprocess.run(["pgrep", "-g", str(pgid)], capture_output=True, text=True).stdout.split()
        self.assertEqual(left, [], "the server in the wrapper's process group must be gone too")

    def test_the_old_self_reentering_trap_did_segfault(self):
        # The control: the previous script dies of SIGSEGV here, so the test above is not vacuous.
        returncode, _ = self._run_and_terminate("trap 'kill -TERM 0 2>/dev/null; exit 0' TERM; sleep 30 & wait $!")
        self.assertEqual(returncode, -signal.SIGSEGV)

    def test_rabbitmq_is_started_through_the_wrapper(self):
        source = open(_TEST_PY_PATH, encoding="utf-8").read()
        self.assertIn('rabbitmq_wrapper_script("rabbitmq-server")', source)
        self.assertNotIn("trap 'kill -TERM 0", source.replace("RABBITMQ_WRAPPER_SCRIPT", ""))


class ChromeNoDiskCacheTests(unittest.TestCase):
    """Headless Chrome in the test runs never writes an HTTP, media or code cache to disk."""

    def test_every_chrome_args_default_carries_the_no_cache_flags(self):
        source = open(_TEST_PY_PATH, encoding="utf-8").read()
        self.assertGreaterEqual(source.count("CHROME_NO_CACHE_ARGS +"), 3)
        for flag in ("--disk-cache-dir=/dev/null", "--disk-cache-size=1", "--media-cache-size=1"):
            self.assertIn(flag, _test_runner.CHROME_NO_CACHE_ARGS)


class SkipGoogleFontsDownloadTests(unittest.TestCase):
    """A test database build does not download Google's fonts (ham_init); production, which never runs test.py, still does."""

    def test_the_switch_is_on_by_default_for_the_processes_the_runner_starts(self):
        environ = {}
        self.assertEqual(_test_runner.apply_skip_google_fonts_download(environ), "1")
        self.assertEqual(environ["HAMS_SKIP_GOOGLE_FONTS_DOWNLOAD"], "1")

    def test_a_value_the_caller_chose_is_kept(self):
        environ = {"HAMS_SKIP_GOOGLE_FONTS_DOWNLOAD": "0"}
        self.assertEqual(_test_runner.apply_skip_google_fonts_download(environ), "0")

    def test_main_applies_it_before_anything_is_spawned(self):
        source = inspect.getsource(_test_runner.main)
        self.assertIn("apply_skip_google_fonts_download(os.environ)", source)


class BlockPskTransportTests(unittest.TestCase):
    """The runner blocks the real PSK Reporter transports in every process it starts; production never sets the switch."""

    def test_the_switch_is_on_by_default(self):
        environ = {}
        self.assertEqual(_test_runner.apply_block_psk_transport(environ), "1")
        self.assertEqual(environ["HAMS_PSK_BLOCK_TRANSPORT"], "1")

    def test_a_value_the_caller_chose_is_kept(self):
        environ = {"HAMS_PSK_BLOCK_TRANSPORT": "0"}
        self.assertEqual(_test_runner.apply_block_psk_transport(environ), "0")

    def test_main_applies_it_before_anything_is_spawned(self):
        self.assertIn("apply_block_psk_transport(os.environ)", inspect.getsource(_test_runner.main))


class RunCmdHangRecoveryTests(unittest.TestCase):
    """run_cmd()'s 60-second no-output branch used to kill headless chrome
    and retry FOREVER on every timeout -- `force_killed` was assigned
    `False` once and never set `True` again anywhere in the function (a
    2026-06-24 commit, e24ff25, removed the only site that used to set it,
    without bounding the new retry-forever replacement it introduced), so
    a process that stays hung even after chrome is killed made run_cmd --
    and the whole test.py invocation -- never terminate.

    These tests spawn a real, silent, long-running subprocess (`sleep
    3600`, which never produces stdout and never exits on its own) and
    mock time.time() to jump far forward on every call, so the 60-second
    branch fires on the very first check instead of requiring a real
    60-second wait. RUN_CMD_MAX_HANG_RECOVERY_ATTEMPTS is patched to 1 so
    the bound is reached after two timeout events instead of six,
    keeping this test to a handful of real seconds (each iteration still
    costs one real second: the no-output branch is only reached after
    queue.Queue.get(timeout=1.0) raises Empty, which is real wall-clock
    time, not something time.time() mocking affects).
    """

    def _fake_clock(self):
        # Starts at the real current time (captured here, BEFORE the
        # caller's patch("time.time", ...) context is entered -- so any
        # code elsewhere in the process that also reads time.time() during
        # this test sees a plausible value) and jumps forward by 1000s on
        # every call, so "time.time() - last_output_time > 60.0" is true
        # from the very first post-spawn check onward.
        counter = itertools.count()
        start = time.time()

        def _clock():
            return start + next(counter) * 1000.0

        return _clock

    def test_a_process_that_never_recovers_is_eventually_force_killed_and_reported_as_failed(self):
        with patch.object(
            _test_runner, "RUN_CMD_MAX_HANG_RECOVERY_ATTEMPTS", 1
        ), patch("time.time", side_effect=self._fake_clock()):
            rc = _test_runner.run_cmd(["sleep", "3600"])
        self.assertEqual(
            rc, 1, "a process that never recovers from repeated un-hang "
            "attempts must be reported as a failure, not silently retried "
            "forever nor treated as a pass"
        )

    def test_force_killed_is_reported_as_failure_even_with_zero_captured_errors(self):
        # Before this pass's second fix, `if force_killed: return 1 if
        # final_errors > initial_errors else 0` would report SUCCESS for a
        # force-killed (hung, never-completed) run that happened to
        # capture no error text -- a hang is itself a failure regardless
        # of whether any assertion-style error string was seen first.
        extractor = MagicMock()
        extractor.captured_blocks = []
        extractor.mcp_mode = False
        with patch.object(
            _test_runner, "RUN_CMD_MAX_HANG_RECOVERY_ATTEMPTS", 1
        ), patch("time.time", side_effect=self._fake_clock()):
            rc = _test_runner.run_cmd(["sleep", "3600"], extractor=extractor)
        self.assertEqual(rc, 1)

    def test_the_lifetime_cap_alone_also_bounds_the_loop(self):
        # A real hang/kill-chrome cycle might not stay perfectly silent --
        # killing chrome could itself provoke a line or two of real output
        # (a websocket/CDP error, a tour-teardown message) each time, which
        # would reset the CONSECUTIVE counter to 0 forever while the run is,
        # in substance, still stuck in the same cycle. This test proves the
        # separate lifetime cap (RUN_CMD_MAX_TOTAL_HANG_RECOVERY_ATTEMPTS)
        # bounds the loop on its own even when the consecutive cap is set
        # high enough that it would never fire by itself.
        with patch.object(
            _test_runner, "RUN_CMD_MAX_HANG_RECOVERY_ATTEMPTS", 1000
        ), patch.object(
            _test_runner, "RUN_CMD_MAX_TOTAL_HANG_RECOVERY_ATTEMPTS", 1
        ), patch("time.time", side_effect=self._fake_clock()):
            rc = _test_runner.run_cmd(["sleep", "3600"])
        self.assertEqual(rc, 1)


class MainModeDispatchTests(unittest.TestCase):
    """--mode has accepted "xml" and "downloads" as valid argparse choices
    since test.py's very first commit (confirmed via `git log -p --follow`
    on this file), but neither one was ever given a dispatch branch in
    main() -- only "standard" and "individual" were. Before this fix,
    either value silently skipped check_linters()'s only real gate,
    rebuild_db(), and run_cmd() entirely, falling straight through to the
    Chrome-cleanup/audio-sink checks with final_rc still at its initial 0:
    `test.py --mode xml -u <module>` reported a clean, passing exit code
    having run zero actual Odoo tests (bug class 19: no-default-dispatch).

    main() does a great deal before reaching the mode dispatch (privilege
    re-exec, namespace setup, the single-instance lock, linters) -- every
    real side-effecting piece of that is mocked here, following the same
    philosophy test_provision.py already uses for provision(): only the
    control-flow decision under test (which branch --mode reaches) runs
    for real.
    """

    def _run_main(self, mode):
        # HAMS_ISOLATED_NS=1 skips the whole sudo/unshare re-exec dance
        # (this project's own standing convention for direct,
        # already-isolated invocation -- see
        # hams-odoo-test-runner-sudo-and-polkit); HAMS_TEST_LOCK_HELD=1
        # skips the single-instance lock's real file acquisition.
        env_overrides = {
            "HAMS_ISOLATED_NS": "1",
            "HAMS_TEST_LOCK_HELD": "1",
        }
        # main()'s very first real-filesystem check is is_git_checkout_root()
        # ORed with a literal os.path.isfile("<cwd>/tools/test.py") check --
        # a real, disposable directory satisfying both is simpler and more
        # robust to this test's own invocation cwd than mocking os.path.isfile
        # selectively.
        fake_repo = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(fake_repo, ".git"))
            os.makedirs(os.path.join(fake_repo, "tools"))
            open(os.path.join(fake_repo, "tools", "test.py"), "w").close()
            orig_cwd = os.getcwd()
            os.chdir(fake_repo)
            try:
                with patch.dict(os.environ, env_overrides), \
                     patch.object(sys, "argv", ["test.py", "--mode", mode, "-u", "dummy_mod"]), \
                     patch.object(_test_runner.host_requirements, "require_test_host"), \
                     patch.object(_test_runner, "get_addons_path", return_value="/dummy/addons"), \
                     patch.object(_test_runner, "load_ignore_file", return_value=[]), \
                     patch.object(_test_runner, "get_local_modules", return_value=["dummy_mod"]), \
                     patch.object(_test_runner, "FailureExtractor", return_value=MagicMock()), \
                     patch.object(_test_runner, "check_linters") as mock_check_linters, \
                     patch.object(_test_runner, "rebuild_db") as mock_rebuild_db, \
                     patch.object(_test_runner, "run_cmd") as mock_run_cmd:
                    with self.assertRaises(SystemExit) as ctx:
                        _test_runner.main()
            finally:
                os.chdir(orig_cwd)
        finally:
            shutil.rmtree(fake_repo, ignore_errors=True)
        return ctx.exception.code, mock_check_linters, mock_rebuild_db, mock_run_cmd

    def test_an_unimplemented_mode_exits_nonzero_instead_of_reporting_a_false_pass(self):
        for mode in ("xml", "downloads"):
            with self.subTest(mode=mode):
                code, _, mock_rebuild_db, mock_run_cmd = self._run_main(mode)
                self.assertNotEqual(
                    code, 0,
                    f"--mode {mode} must not exit 0 without ever running a test"
                )
                mock_rebuild_db.assert_not_called()
                mock_run_cmd.assert_not_called()

    def test_standard_mode_still_dispatches_normally(self):
        code, mock_check_linters, mock_rebuild_db, mock_run_cmd = self._run_main("standard")
        mock_check_linters.assert_called_once()
        mock_rebuild_db.assert_called_once()
        mock_run_cmd.assert_called_once()


class SlotWiringMainTests(unittest.TestCase):
    """Tests [@ANCHOR: test:slot_handoff_args]
    Tests [@ANCHOR: test:enforce_slot_refusals]
    Tests [@ANCHOR: test:broker_users_sharing_the_host]
    Tests [@ANCHOR: test:apply_slot_resources]
    Tests [@ANCHOR: test:try_slot_locks]

    main() with the heavy steps mocked (as MainModeDispatchTests does): what a run in a given slot hands
    to rebuild_db(), Odoo's command line, its environment and the failure extractor, with the default
    (one slot) asserted to be what it was before slots existed."""

    def _run_main(self, argv, env_overrides, hold_slots=(), broker=False):
        calls = {"rebuild": [], "cmd": [], "env": [], "extractor": []}

        def fake_run_cmd(cmd, extractor=None, **kwargs):
            calls["cmd"].append(list(cmd))
            calls["env"].append(dict(os.environ))
            return 0

        def fake_extractor(*args, **kwargs):
            calls["extractor"].append(kwargs)
            extractor = MagicMock()
            extractor.odoo_result_lines = [{"failed": 0, "errors": 0, "total": 1}]
            return extractor

        fake_repo = tempfile.mkdtemp()
        lock_dir = tempfile.mkdtemp()
        held = []
        try:
            os.makedirs(os.path.join(fake_repo, ".git"))
            os.makedirs(os.path.join(fake_repo, "tools"))
            open(os.path.join(fake_repo, "tools", "test.py"), "w").close()
            lock_path = os.path.join(lock_dir, "test.lock")
            for slot in hold_slots:
                handle = open(f"{lock_path}.slot-{slot}" if slot > 1 else lock_path, "a")
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held.append(handle)
            env = {"HAMS_ISOLATED_NS": "1", "HAMS_TEST_LOCK_PATH": lock_path, "HAMS_SKIP_CI_LOAD_GATE": "1"}
            env.update(env_overrides)
            orig_cwd = os.getcwd()
            os.chdir(fake_repo)
            try:
                with patch.dict(os.environ, env), \
                     patch.object(_test_runner, "_CHROME_KILL_PATTERN", _test_runner._CHROME_KILL_PATTERN), \
                     patch.object(_test_runner, "_CHROME_PROFILE_GLOBS", _test_runner._CHROME_PROFILE_GLOBS), \
                     patch.object(sys, "argv", ["test.py", "-u", "dummy_mod", *argv]), \
                     patch.object(_test_runner.runner_slots, "broker_listening", return_value=broker), \
                     patch.object(_test_runner.runner_slots, "modules_mentioning_broker",
                                  return_value=["dummy_mod"]), \
                     patch.object(_test_runner, "get_addons_path", return_value="/dummy/addons"), \
                     patch.object(_test_runner, "load_ignore_file", return_value=[]), \
                     patch.object(_test_runner, "get_local_modules", return_value=["dummy_mod"]), \
                     patch.object(_test_runner, "FailureExtractor", side_effect=fake_extractor), \
                     patch.object(_test_runner, "check_linters"), \
                     patch.object(_test_runner, "rebuild_db",
                                  side_effect=lambda *a, **k: calls["rebuild"].append((a, k))), \
                     patch.object(_test_runner, "run_cmd", side_effect=fake_run_cmd), \
                     contextlib.redirect_stdout(io.StringIO()) as out:
                    with self.assertRaises(SystemExit) as ctx:
                        _test_runner.main()
                    chrome = (_test_runner._CHROME_KILL_PATTERN, _test_runner._CHROME_PROFILE_GLOBS)
            finally:
                os.chdir(orig_cwd)
        finally:
            for handle in held:
                handle.close()
            shutil.rmtree(fake_repo, ignore_errors=True)
            shutil.rmtree(lock_dir, ignore_errors=True)
        return ctx.exception.code, calls, out.getvalue(), chrome

    @staticmethod
    def _flag(cmd, name):
        return cmd[cmd.index(name) + 1]

    def test_one_slot_hands_over_exactly_what_it_always_did(self):
        code, calls, _, chrome = self._run_main(
            ["--slots", "1"], {}
        )
        self.assertEqual(code, 0)
        self.assertEqual(calls["rebuild"], [(("hams_test", None), {})])
        cmd = calls["cmd"][0]
        self.assertEqual(self._flag(cmd, "-d"), "hams_test")
        self.assertEqual(self._flag(cmd, "--http-port"), "8075")
        self.assertEqual(calls["env"][0]["ODOO_URL"], "http://127.0.0.1:8075")
        self.assertEqual(calls["env"][0]["REDIS_DB"], "1")
        self.assertEqual(calls["env"][0]["DB_NAME"], "hams_test")
        self.assertEqual(calls["extractor"][0]["slot"], 1)
        self.assertEqual(chrome, ("_chrome_odoo", ("/tmp/*_chrome_odoo", "/var/tmp/*_chrome_odoo")))

    def test_a_host_run_in_slot_two_has_its_own_database_port_redis_and_tmpdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, calls, out, chrome = self._run_main(
                ["--slots", "2", "--db", "hams_test_relay"], {"TMPDIR": tmp}, hold_slots=(1,)
            )
            self.assertEqual(code, 0, out)
            self.assertTrue(os.path.isdir(os.path.join(tmp, "slot-2")))
            self.assertEqual(calls["env"][0]["TMPDIR"], os.path.join(tmp, "slot-2"))
            self.assertEqual(chrome[1], (os.path.join(tmp, "slot-2") + "/*_chrome_odoo",))
        self.assertIn("Holding test slot 2 of 2", out)
        self.assertEqual(calls["rebuild"], [(("hams_test_relay_s2", 2), {})])
        cmd = calls["cmd"][0]
        self.assertEqual(self._flag(cmd, "-d"), "hams_test_relay_s2")
        self.assertEqual(self._flag(cmd, "--http-port"), "8076")
        self.assertEqual(calls["env"][0]["ODOO_URL"], "http://127.0.0.1:8076")
        self.assertEqual(calls["env"][0]["REDIS_DB"], "2")
        self.assertEqual(calls["env"][0]["DB_NAME"], "hams_test_relay_s2")
        self.assertEqual(calls["extractor"][0]["slot"], 2)

    def test_two_runs_on_one_host_share_no_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, first, _, _ = self._run_main(["--slots", "2"], {"TMPDIR": tmp})
            _, second, _, _ = self._run_main(["--slots", "2"], {"TMPDIR": tmp}, hold_slots=(1,))
        pairs = {
            "database": (first["rebuild"][0][0][0], second["rebuild"][0][0][0]),
            "port": (self._flag(first["cmd"][0], "--http-port"), self._flag(second["cmd"][0], "--http-port")),
            "redis": (first["env"][0]["REDIS_DB"], second["env"][0]["REDIS_DB"]),
            "tmpdir": (first["env"][0]["TMPDIR"], second["env"][0]["TMPDIR"]),
        }
        for name, (one, two) in pairs.items():
            with self.subTest(resource=name):
                self.assertNotEqual(one, two)

    def test_the_final_phase_of_the_full_pipeline_keeps_its_ports_and_takes_the_db_of_its_slot(self):
        # HAMS_TEST_LOCK_HELD=1 plus the slot words the root phase passes down: the namespace is private,
        # so only the database (and the log file names) depend on the slot.
        code, calls, _, chrome = self._run_main(
            ["--slots", "3", "--slot-held", "3"], {"HAMS_TEST_LOCK_HELD": "1"}
        )
        self.assertEqual(code, 0)
        self.assertEqual(calls["rebuild"], [(("hams_test_s3", None), {})])
        self.assertEqual(self._flag(calls["cmd"][0], "--http-port"), "8075")
        self.assertEqual(calls["env"][0]["REDIS_DB"], "1")
        self.assertEqual(calls["extractor"][0]["slot"], 3)
        self.assertEqual(chrome[0], "_chrome_odoo")

    def test_a_host_run_above_slot_one_refuses_a_shared_broker(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, calls, out, _ = self._run_main(
                ["--slots", "2"], {"TMPDIR": tmp}, hold_slots=(1,), broker=True
            )
            self.assertEqual(code, 1, out)
            self.assertIn("dummy_mod use RabbitMQ and one is listening on 127.0.0.1:5672", out)
            self.assertEqual((calls["rebuild"], calls["cmd"]), ([], []))
            # Slot 1 is never refused: it is what a run always was.
            code, _, out, _ = self._run_main(["--slots", "2"], {"TMPDIR": tmp}, broker=True)
            self.assertEqual(code, 0, out)

    def test_pause_on_fail_is_refused_above_slot_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, calls, out, _ = self._run_main(
                ["--slots", "2", "--pause-on-fail"], {"TMPDIR": tmp}, hold_slots=(1,)
            )
        self.assertEqual(code, 1)
        self.assertIn("--pause-on-fail pins Chrome's debugging port 9222", out)
        self.assertEqual((calls["rebuild"], calls["cmd"]), ([], []))

    def _invoke_and_capture_exec(self, argv, hold_slots=(), keep_slots_env=False):
        """main() as the invoking process of the full pipeline (no HAMS_ISOLATED_NS): everything up to the
        exec of the privileged phase runs for real; the exec itself is captured."""

        class Exec(Exception):
            pass

        captured = {}

        def fake_exec(program, command, environ):
            captured["command"] = list(command)
            raise Exec()

        with tempfile.TemporaryDirectory() as tmp:
            fake_repo = os.path.join(tmp, "repo")
            os.makedirs(os.path.join(fake_repo, ".git"))
            os.makedirs(os.path.join(fake_repo, "tools"))
            open(os.path.join(fake_repo, "tools", "test.py"), "w").close()
            lock_path = os.path.join(tmp, "test.lock")
            held = []
            for slot in hold_slots:
                handle = open(f"{lock_path}.slot-{slot}" if slot > 1 else lock_path, "a")
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held.append(handle)
            env = {"HAMS_TEST_LOCK_PATH": lock_path, "HAMS_SKIP_CI_LOAD_GATE": "1", "HOME": tmp}
            orig_cwd = os.getcwd()
            os.chdir(fake_repo)
            try:
                with patch.dict(os.environ, env), \
                     patch.object(sys, "argv", ["test.py", "-u", "dummy_mod", "--solo", "-l",
                                                os.path.join(tmp, "logs"), *argv]), \
                     patch.object(_test_runner, "check_host_apt_packages"), \
                     patch.object(_test_runner, "get_default_audio_sink_name", return_value=None), \
                     patch.object(_test_runner.os, "execvpe", side_effect=fake_exec), \
                     contextlib.redirect_stdout(io.StringIO()):
                    os.environ.pop("HAMS_ISOLATED_NS", None)
                    if not keep_slots_env:
                        os.environ.pop("HAMS_TEST_SLOTS", None)
                    with self.assertRaises(Exec):
                        _test_runner.main()
            finally:
                os.chdir(orig_cwd)
                for handle in held:
                    handle.close()
        return captured["command"]

    def test_the_invoking_process_hands_its_slot_to_the_root_phase(self):
        command = self._invoke_and_capture_exec(["--preflight-done", "--slots", "3"], hold_slots=(1,))
        self.assertEqual(
            command[-5:], ["--preflight-done", "--slots", "3", "--slot-held", "2"]
        )
        self.assertLess(command.index("unshare"), command.index("--internal-ns-init"))

    def test_the_slot_is_handed_down_even_when_only_the_environment_chose_the_count(self):
        # No --slots on the command line: the invoking process still writes the count and the slot
        # into the command line of the next phase, so that phase never has to read the environment.
        with patch.dict(os.environ, {"HAMS_TEST_SLOTS": "2"}):
            command = self._invoke_and_capture_exec(["--preflight-done"], keep_slots_env=True)
        self.assertEqual(command[-5:], ["--preflight-done", "--slots", "2", "--slot-held", "1"])

    def test_with_one_slot_the_root_phase_command_line_is_what_it_always_was(self):
        command = self._invoke_and_capture_exec(["--preflight-done", "--slots", "1"])
        self.assertEqual(command[-1], "--preflight-done")
        self.assertNotIn("--slot-held", command)
        self.assertEqual(command.count("--slots"), 1)  # only the user's own

    def test_broker_users_are_looked_up_only_for_a_host_run_above_slot_one_with_a_listening_broker(self):
        slots = _test_runner.runner_slots
        with tempfile.TemporaryDirectory() as addons:
            for name, text in (("uses_broker", "import pika\n"), ("plain", "x = 1\n")):
                os.makedirs(os.path.join(addons, name, "tests"))
                with open(os.path.join(addons, name, "tests", "test_a.py"), "w") as handle:
                    handle.write(text)
            host2 = slots.slot_resources(2, 2, False, "/var/lib/odoo/tmp")
            host1 = slots.slot_resources(1, 2, False, "/var/lib/odoo/tmp")
            private2 = slots.slot_resources(2, 2, True, "/var/lib/odoo/tmp")
            modules = ["uses_broker", "plain", "absent"]
            use = _test_runner.broker_users_sharing_the_host
            with patch.object(slots, "broker_listening", return_value=True):
                self.assertEqual(use(host2, modules, addons), ["uses_broker"])
                self.assertEqual(use(host1, modules, addons), [])
                self.assertEqual(use(private2, modules, addons), [])
            with patch.object(slots, "broker_listening", return_value=False):
                self.assertEqual(use(host2, modules, addons), [])

    def test_the_handoff_words_are_empty_for_one_slot_and_explicit_otherwise(self):
        self.assertEqual(_test_runner.slot_handoff_args(1, 1), [])
        self.assertEqual(_test_runner.slot_handoff_args(4, 3), ["--slots", "4", "--slot-held", "3"])

    def test_the_root_phase_gets_the_slot_words_in_its_command_line(self):
        source = io.open(_TEST_PY_PATH, encoding="utf-8").read()
        self.assertIn("exec_cmd += slot_handoff_args(slots, held_slot)", source)
        self.assertLess(
            source.index("] + sys.argv[1:] + [\"--preflight-done\"]"),
            source.index("exec_cmd += slot_handoff_args(slots, held_slot)"),
        )


class SlotFilesAndKillTests(unittest.TestCase):
    """What a slot writes to the log directory, which Redis database it flushes, and which Chrome it kills."""

    def test_slot_one_writes_the_original_report_and_progress_files(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"HOME": tmp}):
            os.environ.pop("HAMS_REAL_LOG_DIRECTORY", None)
            extractor = _test_runner.FailureExtractor(tmp, disable_atexit=True)
            self.assertEqual(extractor.display_path, os.path.join(tmp, "filtered_test.txt"))
            self.assertEqual(extractor.progress_path, os.path.join(tmp, "tmp", "test_progress.txt"))

    def test_a_higher_slot_writes_files_of_its_own(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"HOME": tmp}):
            os.environ.pop("HAMS_REAL_LOG_DIRECTORY", None)
            first = _test_runner.FailureExtractor(tmp, disable_atexit=True, slot=1)
            second = _test_runner.FailureExtractor(tmp, disable_atexit=True, slot=2)
            self.assertEqual(second.display_path, os.path.join(tmp, "filtered_test.slot-2.txt"))
            self.assertEqual(second.progress_path, os.path.join(tmp, "tmp", "test_progress.slot-2.txt"))
            self.assertNotEqual(first.display_path, second.display_path)
            self.assertNotEqual(first.progress_path, second.progress_path)
            self.assertTrue(os.path.exists(second.progress_path))

    def _flush_command(self, **kwargs):
        calls = []

        def fake_run(cmd, *args, **kw):
            calls.append(list(cmd))
            if "-At" in cmd:
                return MagicMock(returncode=0, stdout="postgres\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch.dict(os.environ, {"HAMS_ISOLATED_NS": "1"}), \
             patch.object(_test_runner.subprocess, "run", side_effect=fake_run), \
             patch.object(_test_runner.infrastructure, "get_pg_bin", return_value="psql"), \
             patch.object(_test_runner, "PRODUCTION_HOST_MARKER_FILES", ()), \
             patch.object(_test_runner, "remove_stale_filestore"), \
             contextlib.redirect_stdout(io.StringIO()):
            os.environ.pop("REDIS_DB", None)
            _test_runner.rebuild_db("hams_test", **kwargs)
        return [c for c in calls if c[:1] == ["redis-cli"] and "flushdb" in c][0]

    def test_rebuild_db_flushes_what_it_always_flushed_unless_told_a_slot_database(self):
        self.assertEqual(self._flush_command(), ["redis-cli", "-n", "0", "flushdb"])
        self.assertEqual(self._flush_command(redis_flush_db=3), ["redis-cli", "-n", "3", "flushdb"])

    def test_the_chrome_kill_uses_the_runs_own_pattern(self):
        calls = []
        with patch.object(_test_runner.subprocess, "run", side_effect=lambda cmd, **kw: calls.append(cmd)):
            _test_runner.kill_test_chrome("TERM")
            with patch.object(_test_runner, "_CHROME_KILL_PATTERN", "/var/lib/odoo/tmp/slot\\-2/[^/ ]*_chrome_odoo"):
                _test_runner.kill_test_chrome("KILL")
        self.assertEqual(calls[0], ["pkill", "-u", "odoo", "-TERM", "-f", "_chrome_odoo"])
        self.assertEqual(
            calls[1],
            ["pkill", "-u", "odoo", "-KILL", "-f", "/var/lib/odoo/tmp/slot\\-2/[^/ ]*_chrome_odoo"],
        )


class NoTestsRanTests(unittest.TestCase):
    """Tests [@ANCHOR: test:exit_status_for_run]

    An Odoo process that dies before the tests ("Port 8075 is in use", a database login failure) used to
    end with the banner "No test failures or system crashes detected", and one that exited 0 without a
    verdict counted as a pass. Real child processes, the real extractor and the real banner."""

    def _run(self, child_code):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"HOME": tmp}):
            os.environ.pop("HAMS_REAL_LOG_DIRECTORY", None)
            extractor = _test_runner.FailureExtractor(tmp, disable_atexit=True)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                returned = _test_runner.run_cmd([sys.executable, "-c", child_code], extractor)
                status = _test_runner.exit_status_for_run(returned, extractor)
                extractor.finish_and_write()
        return status, out.getvalue()

    def test_a_child_that_dies_before_the_tests_is_a_failure_with_a_failure_banner(self):
        status, text = self._run("import sys; print('Port 8075 is in use by another program.'); sys.exit(1)")
        self.assertEqual(status, 1)
        self.assertIn("TEST RUN FAILED: no tests ran", text)
        self.assertIn("exit status: 1", text)
        self.assertNotIn("No test failures or system crashes detected", text)
        self.assertNotIn("the suite passed", text)

    def test_a_child_that_exits_zero_without_a_verdict_is_a_failure(self):
        status, text = self._run("print('started and stopped')")
        self.assertEqual(status, 1)
        self.assertIn("Odoo exited 0 but printed no odoo.tests.result line", text)
        self.assertIn("TEST RUN FAILED: no tests ran", text)

    def test_a_child_with_a_passing_verdict_still_passes(self):
        verdict = "2026-11-08 07:54:03,979 1 INFO db odoo.tests.result: 0 failed, 0 error(s) of 7 tests when loading database 'db'"
        status, text = self._run(f"print({verdict!r})")
        self.assertEqual(status, 0)
        self.assertIn("the suite passed", text)
        self.assertNotIn("TEST RUN FAILED", text)

    def test_a_verdict_from_an_earlier_module_does_not_vouch_for_a_later_process(self):
        extractor = MagicMock()
        extractor.odoo_result_lines = [{"failed": 0, "errors": 0, "total": 3}]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(_test_runner.exit_status_for_run(0, extractor, results_before=1), 1)
            self.assertEqual(_test_runner.exit_status_for_run(0, extractor, results_before=0), 0)

    def test_a_non_zero_status_is_passed_through(self):
        self.assertEqual(_test_runner.exit_status_for_run(137, MagicMock()), 137)


class ProductionHostRefusalTests(unittest.TestCase):
    """Bruce's decision, 2026-10-02/03: test.py never runs on the production host (hams1). A
    HAMS_ISOLATED_NS=1 run there flushed production Redis db 0 and fed broker tests to production's
    RabbitMQ consumers. Either signal (deploy_to_production.py's marker files, or a hams_prod
    database on the target PostgreSQL) must stop the run before anything is touched."""

    def test_a_dev_box_with_no_marker_and_no_production_database_is_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            markers = (os.path.join(directory, "DEPLOY_LOG"),)
            self.assertEqual(
                _test_runner.production_host_refusal_reasons(markers, ["postgres", "hams_dev", "hams_test"]), []
            )
            _test_runner.refuse_to_run_on_production_host(markers, ["hams_test"])  # does not exit

    def test_a_deploy_marker_file_alone_refuses(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = os.path.join(directory, "DEPLOY_LOG")
            open(marker, "w").close()
            reasons = _test_runner.production_host_refusal_reasons((marker,), [])
            self.assertEqual(len(reasons), 1)
            self.assertIn(marker, reasons[0])

    def test_a_production_database_alone_refuses(self):
        reasons = _test_runner.production_host_refusal_reasons((), ["postgres", "hams_prod", "hams_test"])
        self.assertEqual(len(reasons), 1)
        self.assertIn("hams_prod", reasons[0])

    def test_refusal_exits_with_status_2_and_says_why(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            _test_runner.refuse_to_run_on_production_host((), ["hams_prod"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("PRODUCTION host", out.getvalue())
        self.assertIn("hams_prod", out.getvalue())

    def test_the_real_marker_paths_are_the_ones_deploy_to_production_writes(self):
        self.assertEqual(
            _test_runner.PRODUCTION_HOST_MARKER_FILES,
            ("/opt/hams/src/DEPLOY_LOG", "/opt/hams/src/DEPLOYED_COMMITS"),
        )

    def test_list_database_names_parses_psql_output(self):
        fake = MagicMock(returncode=0, stdout="postgres\nhams_prod\n\nhams_test\n", stderr="")
        with patch.object(_test_runner.subprocess, "run", return_value=fake) as run:
            self.assertEqual(
                _test_runner.list_database_names("psql", {}), ["postgres", "hams_prod", "hams_test"]
            )
        self.assertEqual(run.call_args.args[0][:2], ["psql", "postgres"])

    def test_list_database_names_fails_fast_when_postgres_is_unreachable(self):
        fake = MagicMock(returncode=2, stdout="", stderr="connection refused")
        with patch.object(_test_runner.subprocess, "run", return_value=fake), \
             contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            _test_runner.list_database_names("psql", {})
        self.assertEqual(ctx.exception.code, 1)

    def test_rebuild_db_refuses_before_the_redis_flush(self):
        """The flush is what wiped production Redis; the refusal has to come first."""
        calls = []

        def fake_run(cmd, *args, **kwargs):
            calls.append(list(cmd))
            if "-At" in cmd:
                return MagicMock(returncode=0, stdout="postgres\nhams_prod\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch.dict(os.environ, {"HAMS_ISOLATED_NS": "1"}), \
             patch.object(_test_runner.subprocess, "run", side_effect=fake_run), \
             patch.object(_test_runner.infrastructure, "get_pg_bin", return_value="psql"), \
             patch.object(_test_runner, "PRODUCTION_HOST_MARKER_FILES", ()), \
             contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            _test_runner.rebuild_db("hams_test")
        self.assertEqual(ctx.exception.code, 2)
        self.assertFalse([c for c in calls if c and c[0] == "redis-cli"], calls)
        self.assertFalse([c for c in calls if "DROP" in " ".join(c)], calls)

    def test_main_refuses_first_when_a_marker_file_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = os.path.join(directory, "DEPLOYED_COMMITS")
            open(marker, "w").close()
            with patch.object(_test_runner, "PRODUCTION_HOST_MARKER_FILES", (marker,)), \
                 patch.object(_test_runner, "rebuild_db") as mock_rebuild_db, \
                 patch.object(_test_runner, "run_cmd") as mock_run_cmd, \
                 patch.object(sys, "argv", ["test.py", "-u", "dummy_mod"]), \
                 contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as ctx:
                _test_runner.main()
        self.assertEqual(ctx.exception.code, 2)
        mock_rebuild_db.assert_not_called()
        mock_run_cmd.assert_not_called()


class ResourceMonitorHelperTests(unittest.TestCase):
    # Real bug found 2026-09-12: both of these helpers wrapped their real
    # work in `except Exception: pass`, silently returning None/0 (a
    # legitimate "monitoring is unavailable this cycle" fallback) with zero
    # trace of WHY. Deliberately a fresh, unstarted instance -- exercising
    # get_available_memory_mb()/get_chrome_count() directly needs no
    # background thread.
    def setUp(self):
        self.monitor = _test_runner.ResourceMonitorThread()

    def test_get_available_memory_mb_warns_and_returns_none_on_a_real_read_failure(self):
        def _raise_open(*a, **kw):
            raise OSError("meminfo unreadable")

        with patch.object(_test_runner, "open", side_effect=_raise_open, create=True):
            with self.assertLogs(_test_runner.__name__, level="WARNING") as cm:
                result = self.monitor.get_available_memory_mb()
        self.assertIsNone(result)
        self.assertTrue(any("meminfo" in msg for msg in cm.output))

    def test_get_chrome_count_warns_and_returns_zero_on_a_real_counting_failure(self):
        """Renamed and rewired 2026-09-16: this used to assert the failure of
        `subprocess.run(["pgrep", "-c", "-f", "chrom"])`, which was the whole
        implementation. That box-wide substring match counted the developer's
        own desktop browser, so the counting method changed (see
        `TestBrowserCountTests`) -- but the property this test was written to
        protect did not, and is kept rather than dropped with the old
        implementation: a counting failure must WARN and degrade to zero, never
        raise into the monitor thread and never fail silently."""
        with patch.object(
            _test_runner,
            "count_test_browser_processes",
            side_effect=OSError("/proc unreadable"),
        ):
            with self.assertLogs(_test_runner.__name__, level="WARNING") as cm:
                result = self.monitor.get_chrome_count()
        self.assertEqual(result, 0)
        self.assertTrue(any("test browser processes" in msg for msg in cm.output))


class ResolveRepoLayoutTests(unittest.TestCase):
    # Real bug found 2026-09-12: the `except (subprocess.CalledProcessError,
    # OSError): pass` here used to swallow a real `git rev-parse` failure
    # with zero trace, silently falling back to treating a linked worktree
    # as if it were a plain clone (a real behavior change -- the sibling-
    # checkout scan then looks in the wrong parent directory).
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        # A linked-worktree-shaped .git (a FILE, not a directory) is what
        # routes resolve_repo_layout() into the git-rev-parse branch at all.
        with open(os.path.join(self.tmp, ".git"), "w") as f:
            f.write("gitdir: /some/other/path/.git/worktrees/whatever\n")

    def test_a_failed_git_rev_parse_warns_and_falls_back_to_the_plain_clone_layout(self):
        with patch.object(
            _test_runner.subprocess, "run",
            side_effect=_test_runner.subprocess.CalledProcessError(128, ["git"]),
        ):
            with self.assertLogs(_test_runner.__name__, level="WARNING") as cm:
                parent_dir, repo_root = _test_runner.resolve_repo_layout(self.tmp)
        self.assertEqual(repo_root, self.tmp)
        self.assertEqual(parent_dir, os.path.abspath(os.path.join(self.tmp, "..")))
        self.assertTrue(any(self.tmp in msg for msg in cm.output))

    def test_the_git_rev_parse_call_scopes_safe_directory_to_the_worktree(self):
        # Real bug found live, 2026-09-22/23 (night_shift_todo/high/
        # worktree-test-py-cannot-resolve-zero-sudo-addon-b9f496a7.md): a linked worktree's
        # .git is a FILE, not a directory, and git's own ownership-safety check rejects it
        # for an account (odoo, here) with no matching safe.directory entry in its global
        # git config -- "detected dubious ownership", caught by the except below and
        # silently falling back to the plain-clone layout, which is wrong for a worktree.
        # Confirmed live against a real worktree that `-c safe.directory=<base_dir>` on this
        # one subprocess call fixes it with no standing config mutation. This test doesn't
        # reproduce the actual cross-account ownership mismatch (this process already owns
        # its own tmpdir), but does assert the real subprocess call always carries the flag,
        # so a future edit can't silently drop it and reintroduce the bug for every worktree
        # run as odoo again.
        captured_cmd = []
        real_run = _test_runner.subprocess.run

        def spy_run(cmd, **kwargs):
            captured_cmd.append(cmd)
            return real_run(cmd, **kwargs)

        with patch.object(_test_runner.subprocess, "run", side_effect=spy_run):
            _test_runner.resolve_repo_layout(self.tmp)
        self.assertEqual(len(captured_cmd), 1)
        self.assertIn(f"safe.directory={self.tmp}", captured_cmd[0])

    def test_a_real_linked_worktree_resolves_to_the_main_checkout(self):
        # Companion to the mock-based test above: a genuine `git worktree add`, not a fake
        # .git file, exercising resolve_repo_layout() end to end against real git behavior
        # (including the safe.directory-scoped rev-parse call actually succeeding).
        main_repo = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(main_repo, ignore_errors=True))
        run = _test_runner.subprocess.run
        run(["git", "init", "-q", main_repo], check=True)
        run(["git", "-C", main_repo, "config", "user.email", "test@example.com"], check=True)
        run(["git", "-C", main_repo, "config", "user.name", "Test"], check=True)
        with open(os.path.join(main_repo, "README.md"), "w") as f:
            f.write("placeholder\n")
        run(["git", "-C", main_repo, "add", "README.md"], check=True)
        run(["git", "-C", main_repo, "commit", "-q", "-m", "initial"], check=True)

        worktree_dir = os.path.join(self.tmp, "linked_worktree")
        run(["git", "-C", main_repo, "worktree", "add", "-q", worktree_dir], check=True)
        self.addCleanup(lambda: run(["git", "-C", main_repo, "worktree", "remove", "-f", worktree_dir], check=False))

        parent_dir, repo_root = _test_runner.resolve_repo_layout(worktree_dir)
        self.assertEqual(repo_root, main_repo)
        self.assertEqual(parent_dir, os.path.abspath(os.path.join(main_repo, "..")))


class FailureExtractorAttributeGuardTests(unittest.TestCase):
    """hams_shared/tools/ 326-finding discovery, 2026-09-12 (CRITICAL AI LAZINESS:
    Catch-all AttributeError): FailureExtractor.set_context()/finish_and_write() used to
    wrap self.current_test/self._written/self.aborted reads in `try/except AttributeError`,
    even though __init__ unconditionally sets all three before returning and the only
    attribute this instance exposes to outside code before __init__ finishes
    (atexit.register(self.finish_and_write)) never reads any of them -- confirmed by reading
    __init__ and finish_and_write's full bodies, not assumed. These tests exercise the real
    behavior with the guards removed, not just that they don't crash.

    Real instantiation (not a MagicMock, unlike every other FailureExtractor reference in
    this file) is needed to exercise set_context()/finish_and_write() for real;
    disable_atexit=True keeps that from registering a real atexit hook per test, and
    os.path.expanduser is patched so the hardcoded "~/tmp/test_progress.txt" progress file
    lands in this test's own tmpdir instead of the real developer's home directory.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        real_expanduser = os.path.expanduser

        def fake_expanduser(path):
            if path.startswith("~/"):
                return os.path.join(self.tmp, path[2:])
            if path == "~":
                return self.tmp
            return real_expanduser(path)

        patcher = patch.object(_test_runner.os.path, "expanduser", side_effect=fake_expanduser)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_set_context_tracks_the_current_test_across_calls(self):
        extractor = _test_runner.FailureExtractor(self.tmp, disable_atexit=True)
        extractor.set_context("Starting test_one")
        self.assertEqual(extractor.current_test, "test_one")
        extractor.set_context("Starting test_two")
        self.assertEqual(extractor.current_test, "test_two")

    def test_finish_and_write_is_idempotent_via_the_written_flag(self):
        extractor = _test_runner.FailureExtractor(self.tmp, disable_atexit=True)
        extractor.finish_and_write()
        self.assertTrue(os.path.exists(extractor.output_path))
        os.remove(extractor.output_path)
        # A second call must no-op (return early on self._written) rather than
        # re-creating the file -- proves the "w = self._written" read still works
        # correctly with the try/except AttributeError guard removed.
        extractor.finish_and_write()
        self.assertFalse(os.path.exists(extractor.output_path))

    def test_finish_and_write_reports_the_aborted_state(self):
        extractor = _test_runner.FailureExtractor(self.tmp, disable_atexit=True)
        extractor.aborted = True
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            extractor.finish_and_write()
        self.assertIn("TEST RUN ABORTED", buf.getvalue())

    def _written_text(self, extractor):
        extractor.finish_and_write()
        with open(extractor.output_path) as f:
            return f.read()

    def test_append_diagnostic_reaches_the_failure_log_when_not_capturing(self):
        # Regression: run_cmd's hard-timeout branches used to set capturing=True,
        # append to current_block, then capturing=False (block never cleared), so
        # finish_and_write's `capturing and current_block` flush never fired and
        # the diagnostic was silently absent from the report.
        extractor = _test_runner.FailureExtractor(self.tmp, disable_atexit=True)
        extractor.set_context("Starting test_hung")
        extractor.append_diagnostic("DIAGNOSTIC FOR AI (HARD TIMEOUT): tour hung\n")
        self.assertFalse(extractor.capturing)
        self.assertEqual(extractor.current_block, [])
        self.assertIn("DIAGNOSTIC FOR AI (HARD TIMEOUT): tour hung", self._written_text(extractor))

    def test_append_diagnostic_joins_a_block_already_being_captured(self):
        extractor = _test_runner.FailureExtractor(self.tmp, disable_atexit=True)
        extractor.capturing = True
        extractor.current_block.append("Traceback (most recent call last):\n")
        extractor.append_diagnostic("DIAGNOSTIC FOR AI: joined\n")
        self.assertTrue(extractor.capturing)
        text = self._written_text(extractor)
        self.assertIn("Traceback (most recent call last):", text)
        self.assertIn("DIAGNOSTIC FOR AI: joined", text)

    def test_run_cmd_does_not_poke_extractor_capture_state_directly(self):
        # The invariant lives inside FailureExtractor; outside code must go
        # through its methods.
        src = inspect.getsource(_test_runner.run_cmd)
        self.assertNotIn("extractor.capturing", src)
        self.assertNotIn("extractor.current_block", src)


class FailureExtractorOdooResultHeadlineTests(unittest.TestCase):
    """test-py-headline-contradicts-odoo-result-a33308da: the closing headline used to
    report `len(grouped_blocks)` -- a count of captured ERROR-level log-line blocks -- as
    "N issue(s) detected", with no relation to whether the suite actually passed. Any test
    that deliberately provokes an ERROR-level log line on a path it then asserts correctly
    raises (a normal thing for a test to do -- daemon_key_manager's own
    test_write_secure_env_file_* tests are a real example) produced a false positive on a
    run Odoo's own `odoo.tests.result:` line reported as fully green. These tests exercise
    process_line()'s real parsing of that line and finish_and_write()'s real headline
    output with it, using the same real-instantiation pattern as
    FailureExtractorAttributeGuardTests above.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        real_expanduser = os.path.expanduser

        def fake_expanduser(path):
            if path.startswith("~/"):
                return os.path.join(self.tmp, path[2:])
            if path == "~":
                return self.tmp
            return real_expanduser(path)

        patcher = patch.object(_test_runner.os.path, "expanduser", side_effect=fake_expanduser)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _make_extractor(self):
        return _test_runner.FailureExtractor(self.tmp, disable_atexit=True)

    def test_process_line_parses_a_real_odoo_result_line(self):
        extractor = self._make_extractor()
        extractor.process_line(
            "2026-09-16 00:01:11,636 168210 INFO hams_test odoo.tests.result: "
            "0 failed, 0 error(s) of 77 tests when loading database 'hams_test' \n"
        )
        self.assertEqual(
            extractor.odoo_result_lines,
            [{"failed": 0, "errors": 0, "total": 77, "raw": extractor.odoo_result_lines[0]["raw"]}],
        )

    def test_green_odoo_result_overrides_a_false_positive_error_block_count(self):
        # Real reproduction of the bug: a test deliberately logs an ERROR-level line (which
        # gets captured into a block, exactly like daemon_key_manager's own
        # test_write_secure_env_file_* tests do for the PermissionError/OSError they assert
        # on), but the suite as a whole still passes according to Odoo's own result line.
        extractor = self._make_extractor()
        extractor.process_line(
            "2026-09-16 00:00:01,000 1 ERROR hams_test some.module: a deliberately provoked error\n"
        )
        extractor.process_line(
            "2026-09-16 00:00:02,000 1 INFO hams_test odoo.tests.result: "
            "0 failed, 0 error(s) of 16 tests when loading database 'hams_test' \n"
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            extractor.finish_and_write()
        output = buf.getvalue()
        self.assertIn("Odoo reports 0 failed, 0 error(s) of 16 tests -- the suite passed", output)
        self.assertNotIn("issue(s) detected", output)

    def test_real_odoo_failure_is_reported_using_odoo_s_own_counts(self):
        extractor = self._make_extractor()
        extractor.process_line(
            "2026-09-16 00:00:01,000 1 INFO hams_test odoo.tests.result: "
            "2 failed, 1 error(s) of 50 tests when loading database 'hams_test' \n"
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            extractor.finish_and_write()
        self.assertIn("Odoo reports 2 failed, 1 error(s) of 50 tests", buf.getvalue())

    def test_no_odoo_result_line_falls_back_to_the_old_block_count_behavior(self):
        # A run that never reaches Odoo's own result line (e.g. a pre-flight crash) has
        # nothing to cross-check against -- the fallback must say so explicitly rather than
        # silently pretending it's still authoritative.
        extractor = self._make_extractor()
        extractor.process_line(
            "2026-09-16 00:00:01,000 1 ERROR hams_test some.module: a real crash\n"
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            extractor.finish_and_write()
        output = buf.getvalue()
        self.assertIn("issue(s) detected", output)
        self.assertIn("No odoo.tests.result line was found", output)


class RemoveStaleFilestoreTests(unittest.TestCase):
    """rebuild_db() used to DROP/CREATE the same-named database on every real test run without
    ever touching its on-disk Odoo filestore -- since a fresh, empty database has zero
    ir.attachment rows pointing at any of a prior run's own filestore blobs, those became
    permanently orphaned garbage with no other cleanup path (found live: hams_test's own
    filestore had grown to 2.3GB). remove_stale_filestore() is the fix, tested here in isolation
    from rebuild_db()'s own daemon-starting/DB-rebuilding side effects."""

    def test_no_op_when_filestore_directory_does_not_exist(self):
        with tempfile.TemporaryDirectory() as base:
            # Deliberately never create base/some_db -- the real, common case for a
            # never-before-run db_name.
            with patch.object(_test_runner.subprocess, "run") as mock_run:
                _test_runner.remove_stale_filestore("some_db", filestore_base=base)
            mock_run.assert_not_called()

    def test_removes_a_real_directory_it_has_permission_to_delete(self):
        with tempfile.TemporaryDirectory() as base:
            target = os.path.join(base, "hams_test")
            os.makedirs(os.path.join(target, "ab"))
            with open(os.path.join(target, "ab", "cdef0123"), "w") as f:
                f.write("stale attachment blob")

            _test_runner.remove_stale_filestore("hams_test", filestore_base=base)

            self.assertFalse(
                os.path.isdir(target),
                "a filestore directory the caller can already write to should be removed "
                "directly, with no need to shell out to sudo at all",
            )

    def test_permission_error_falls_back_to_sudo_and_succeeds(self):
        # Captured before patching: `patch.object(_test_runner.shutil, "rmtree", ...)` patches
        # the one shared `shutil` module object in sys.modules (test.py's own `import shutil`
        # and this test file's own are the same object) -- so `fake_run` below must not call
        # `shutil.rmtree` by name, or it would recurse straight back into its own mock instead of
        # performing the real removal it's meant to simulate `sudo -n rm -rf` doing.
        real_rmtree = shutil.rmtree

        with tempfile.TemporaryDirectory() as base:
            target = os.path.join(base, "hams_test")
            os.makedirs(target)

            def fake_rmtree(path, *a, **kw):
                raise PermissionError(f"[Errno 13] Permission denied: '{path}'")

            def fake_run(cmd, **kwargs):
                # Simulate `sudo -n rm -rf -- <target>` actually succeeding.
                self.assertEqual(cmd[:4], ["sudo", "-n", "rm", "-rf"])
                self.assertEqual(cmd[-1], target)
                real_rmtree(target)
                return MagicMock(returncode=0, stderr="")

            with patch.object(_test_runner.shutil, "rmtree", side_effect=fake_rmtree), \
                    patch.object(_test_runner.subprocess, "run", side_effect=fake_run):
                _test_runner.remove_stale_filestore("hams_test", filestore_base=base)

            self.assertFalse(
                os.path.isdir(target),
                "a PermissionError on the direct removal must fall back to a non-interactive "
                "`sudo -n rm -rf` rather than silently giving up",
            )

    def test_permission_error_with_failing_sudo_warns_but_never_raises(self):
        with tempfile.TemporaryDirectory() as base:
            target = os.path.join(base, "hams_test")
            os.makedirs(target)

            def fake_rmtree(path, *a, **kw):
                raise PermissionError(f"[Errno 13] Permission denied: '{path}'")

            def fake_run(cmd, **kwargs):
                # Simulate a real, non-NOPASSWD box: sudo -n fails immediately rather than
                # blocking on an interactive prompt (the whole point of the -n flag).
                return MagicMock(
                    returncode=1, stderr="sudo: a password is required\n"
                )

            with patch.object(_test_runner.shutil, "rmtree", side_effect=fake_rmtree), \
                    patch.object(_test_runner.subprocess, "run", side_effect=fake_run):
                # Must not raise -- this is a best-effort cleanup step inside a real test run,
                # and a cleanup failure must never abort the run it's cleaning up after.
                _test_runner.remove_stale_filestore("hams_test", filestore_base=base)

            self.assertTrue(
                os.path.isdir(target),
                "the directory should still be there -- this test only asserts the function "
                "degrades gracefully, not that it magically succeeds anyway",
            )


class CoreDaemonsToStartTests(unittest.TestCase):
    """rebuild_db() starts host daemons only outside the isolated namespace, and PowerDNS is
    never assumed to exist inside it (night_shift_todo 6d19d5cb)."""

    def test_outside_isolation_all_four_host_daemons_are_started(self):
        self.assertEqual(
            _test_runner.core_daemons_to_start(False),
            ["postgresql", "redis-server", "rabbitmq-server", "pdns"],
        )

    def test_inside_isolation_nothing_is_started_on_the_host(self):
        self.assertEqual(_test_runner.core_daemons_to_start(True), [])

    def test_the_docstring_says_pdns_is_not_provided_under_isolation(self):
        doc = _test_runner.core_daemons_to_start.__doc__
        self.assertIn("PowerDNS is deliberately NOT", doc)
        self.assertIn("pdns_server", doc)

    def test_rebuild_db_uses_the_helper_rather_than_its_own_list(self):
        source = inspect.getsource(_test_runner.rebuild_db)
        self.assertIn("core_daemons_to_start(", source)
        self.assertNotIn('"pdns"', source)


class CiLoadGateDecisionTests(unittest.TestCase):
    """`evaluate_ci_load_gate()` is deliberately pure -- it takes the two
    measurements rather than making them -- so every boundary below is
    exercised without a loaded machine and without mocking /proc.

    The numbers come from the real incident recorded in
    `night_shift_todo/medium/odoo-tours-vs-relay-ci-load-contention-4f9f03c6.md`:
    a sixteen-processor box, healthy near a load average of 8 to 11 even with a
    continuous-integration job running, and near 23 when browser tours and
    url_open tests began failing on their own ten-second timeouts.
    """

    def test_a_running_runner_worker_is_busy_whatever_the_load_says(self):
        busy, reason = _test_runner.evaluate_ci_load_gate(
            [{"pid": 2486284, "comm": "Runner.Worker", "uid": 1002}],
            0.1,
            16,
            1.0,
        )
        self.assertTrue(busy)
        # The message must name what was busy, not merely that something was:
        # the operator's next action differs completely between "continuous
        # integration is building" and "a peer is compiling by hand".
        self.assertIn("2486284", reason)
        self.assertIn("Runner.Worker", reason)

    def test_several_runner_workers_are_all_named_and_pluralised(self):
        busy, reason = _test_runner.evaluate_ci_load_gate(
            [
                {"pid": 222, "comm": "Runner.Worker", "uid": 1002},
                {"pid": 111, "comm": "Runner.Worker", "uid": 1002},
            ],
            0.1,
            16,
            1.0,
        )
        self.assertTrue(busy)
        self.assertIn("processes", reason)
        # Sorted, so the message is stable across /proc listing order.
        self.assertIn("111, 222", reason)

    def test_high_load_with_no_runner_is_busy(self):
        """The case the runner check cannot see at all. On 2026-09-15 two peer
        sessions running cargo by hand put the load near 23 with no
        continuous-integration job involved, and the same tours failed."""
        busy, reason = _test_runner.evaluate_ci_load_gate([], 23.0, 16, 1.0)
        self.assertTrue(busy)
        self.assertIn("23.0", reason)
        self.assertIn("16", reason)

    def test_a_quiet_box_is_not_busy(self):
        busy, reason = _test_runner.evaluate_ci_load_gate([], 8.77, 16, 1.0)
        self.assertFalse(busy)
        self.assertEqual(reason, "")

    def test_the_threshold_is_exclusive_at_exactly_the_processor_count(self):
        """A load average equal to the processor count means fully committed
        but not oversubscribed, and is allowed. One tick above is not."""
        self.assertFalse(_test_runner.evaluate_ci_load_gate([], 16.0, 16, 1.0)[0])
        self.assertTrue(_test_runner.evaluate_ci_load_gate([], 16.01, 16, 1.0)[0])

    def test_the_ratio_scales_the_threshold(self):
        # Ratio 2.0 on sixteen processors tolerates a load of 32.
        self.assertFalse(_test_runner.evaluate_ci_load_gate([], 23.0, 16, 2.0)[0])
        # Ratio 0.5 refuses at 9.
        self.assertTrue(_test_runner.evaluate_ci_load_gate([], 9.0, 16, 0.5)[0])

    def test_the_threshold_never_falls_below_one(self):
        """os.cpu_count() can return None, in which case the caller passes 1;
        a zero or negative ratio must not produce a gate that refuses a
        completely idle box."""
        self.assertFalse(_test_runner.evaluate_ci_load_gate([], 0.5, 1, 0.0)[0])
        self.assertTrue(_test_runner.evaluate_ci_load_gate([], 1.5, 1, 0.0)[0])

    def test_an_unavailable_load_average_is_not_treated_as_busy(self):
        """No /proc/loadavg is a missing diagnostic, not evidence of load.
        Refusing to test because a measurement is unavailable would be a worse
        failure than the false timeouts this gate exists to prevent."""
        self.assertFalse(_test_runner.evaluate_ci_load_gate([], None, 16, 1.0)[0])


class CiLoadGateProcessScanTests(unittest.TestCase):
    """`find_runner_worker_processes()` reads /proc directly rather than
    shelling out to `pgrep -f`. That is not a style preference: a
    `pgrep -f Runner.Worker` run from a script whose own command line contains
    the string matches itself, which is the same trap that left a waiting loop
    in this repository spinning for over an hour."""

    def _fake_proc(self, base, entries):
        for pid, comm in entries:
            d = os.path.join(base, str(pid))
            os.makedirs(d)
            with open(os.path.join(d, "comm"), "w", encoding="utf-8") as fh:
                fh.write(comm + "\n")
        # Non-numeric entries are what a real /proc is mostly made of.
        for name in ("self", "cpuinfo", "loadavg"):
            os.makedirs(os.path.join(base, name), exist_ok=True)

    def test_finds_workers_and_ignores_the_permanent_listener(self):
        """`Runner.Listener` runs for as long as the runner service is
        enabled and says nothing about whether a job is executing. Only
        `Runner.Worker` does, which is what makes this an exact signal rather
        than a heuristic."""
        with tempfile.TemporaryDirectory() as base:
            self._fake_proc(
                base,
                [
                    (1974088, "Runner.Listener"),
                    (2486284, "Runner.Worker"),
                    (999, "python3"),
                ],
            )
            found = _test_runner.find_runner_worker_processes(proc_root=base)
            self.assertEqual([p["pid"] for p in found], [2486284])

    def test_an_unreadable_proc_reports_nothing_rather_than_raising(self):
        found = _test_runner.find_runner_worker_processes(
            proc_root="/nonexistent-proc-for-this-test"
        )
        self.assertEqual(found, [])

    def test_a_process_that_exits_mid_scan_is_skipped_not_fatal(self):
        """The normal case on a busy box: the directory is listed, then the
        process is gone before its comm can be read."""
        with tempfile.TemporaryDirectory() as base:
            self._fake_proc(base, [(2486284, "Runner.Worker")])
            os.makedirs(os.path.join(base, "4242"))  # a pid with no comm file
            found = _test_runner.find_runner_worker_processes(proc_root=base)
            self.assertEqual([p["pid"] for p in found], [2486284])


class CiLoadGateWaitTests(unittest.TestCase):
    """`wait_for_ci_load_to_subside()` returns rather than exiting, so the
    wait loop itself is testable. Time is injected, so none of these sleep."""

    def setUp(self):
        for var in (
            "HAMS_SKIP_CI_LOAD_GATE",
            "HAMS_CI_LOAD_GATE_RATIO",
            "HAMS_CI_LOAD_GATE_TIMEOUT",
        ):
            os.environ.pop(var, None)

    def _run(self, busy_sequence, timeout="60"):
        """Drive the gate through a scripted sequence of busy/not-busy
        measurements, with a fake clock that advances one poll interval per
        sleep so the deadline is reached deterministically."""
        os.environ["HAMS_CI_LOAD_GATE_TIMEOUT"] = timeout
        clock = {"t": 0.0}
        calls = {"sleeps": 0}
        seq = iter(busy_sequence)

        def fake_sleep(seconds):
            calls["sleeps"] += 1
            clock["t"] += seconds

        def fake_now():
            return clock["t"]

        def fake_evaluate(workers, load, cpus, ratio):
            try:
                busy = next(seq)
            except StopIteration:
                busy = False
            return (busy, "a fabricated reason") if busy else (False, "")

        buf = io.StringIO()
        with patch.object(_test_runner, "evaluate_ci_load_gate", fake_evaluate), \
                patch.object(_test_runner, "find_runner_worker_processes", lambda **kw: []), \
                contextlib.redirect_stdout(buf):
            ok = _test_runner.wait_for_ci_load_to_subside(
                sleep_func=fake_sleep, now_func=fake_now
            )
        return ok, calls["sleeps"], buf.getvalue()

    def test_a_quiet_box_proceeds_immediately_without_sleeping(self):
        ok, sleeps, out = self._run([False])
        self.assertTrue(ok)
        self.assertEqual(sleeps, 0)
        self.assertEqual(out, "")

    def test_it_waits_and_then_proceeds_once_the_box_quietens(self):
        ok, sleeps, out = self._run([True, True, False])
        self.assertTrue(ok)
        self.assertEqual(sleeps, 2)
        self.assertIn("Waiting for the box to quieten", out)
        self.assertIn("quiet again", out)

    def test_it_gives_up_at_the_timeout_and_names_what_was_busy(self):
        """Bounded on purpose, matching the single-instance lock's own
        fail-fast reasoning: a session told it cannot start can go and do
        something that does not need the box."""
        ok, sleeps, out = self._run([True] * 100, timeout="30")
        self.assertFalse(ok)
        self.assertIn("still too busy", out)
        self.assertIn("a fabricated reason", out)
        # 30-second budget, 15-second poll: two sleeps, then the deadline.
        self.assertEqual(sleeps, 2)

    def test_the_skip_flag_bypasses_the_gate_entirely(self):
        """Every other pre-flight check in this file has a HAMS_SKIP_ escape
        except the init-imports linter, whose absence of one is a documented
        problem rather than a precedent."""
        os.environ["HAMS_SKIP_CI_LOAD_GATE"] = "1"
        buf = io.StringIO()
        called = {"n": 0}

        def must_not_be_called(*a, **kw):
            called["n"] += 1
            return []

        with patch.object(
            _test_runner, "find_runner_worker_processes", must_not_be_called
        ), contextlib.redirect_stdout(buf):
            ok = _test_runner.wait_for_ci_load_to_subside()
        self.assertTrue(ok)
        self.assertEqual(called["n"], 0)
        self.assertIn("HAMS_SKIP_CI_LOAD_GATE=1", buf.getvalue())

    def test_help_never_waits_because_it_starts_no_odoo(self):
        """Reading the usage text loads nothing and tests nothing, so making
        somebody wait out a build matrix for it would be a pure regression."""
        buf = io.StringIO()
        called = {"n": 0}

        def must_not_be_called(*a, **kw):
            called["n"] += 1
            return []

        with patch.object(_test_runner.sys, "argv", ["test.py", "--help"]), \
                patch.object(
                    _test_runner, "find_runner_worker_processes", must_not_be_called
                ), contextlib.redirect_stdout(buf):
            self.assertTrue(_test_runner.wait_for_ci_load_to_subside())
        self.assertEqual(called["n"], 0)

    def test_a_malformed_override_falls_back_to_the_default_and_says_so(self):
        os.environ["HAMS_CI_LOAD_GATE_RATIO"] = "not-a-number"
        os.environ["HAMS_CI_LOAD_GATE_TIMEOUT"] = "also-not"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ratio, timeout = _test_runner._ci_load_gate_settings()
        self.assertEqual(ratio, _test_runner.CI_LOAD_GATE_DEFAULT_LOAD_RATIO)
        self.assertEqual(timeout, _test_runner.CI_LOAD_GATE_DEFAULT_TIMEOUT_SECONDS)
        self.assertIn("HAMS_CI_LOAD_GATE_RATIO", buf.getvalue())
        self.assertIn("HAMS_CI_LOAD_GATE_TIMEOUT", buf.getvalue())

    def test_a_non_finite_override_falls_back_to_the_default_and_says_so(self):
        """float("nan")/float("inf")/float("-infinity") all parse without raising ValueError --
        a plain `float(raw)` guard alone lets them through silently. Real bug found 2026-09-28 by
        hams_shared/tools/check_float_isfinite.py; confirmed it would otherwise have set a NaN
        threshold (every comparison against it is False, so the load gate would never trip) or an
        infinite timeout (the gate would wait forever)."""
        for bad_ratio, bad_timeout in (("nan", "inf"), ("inf", "-infinity"), ("-infinity", "nan")):
            with self.subTest(bad_ratio=bad_ratio, bad_timeout=bad_timeout):
                os.environ["HAMS_CI_LOAD_GATE_RATIO"] = bad_ratio
                os.environ["HAMS_CI_LOAD_GATE_TIMEOUT"] = bad_timeout
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    ratio, timeout = _test_runner._ci_load_gate_settings()
                self.assertEqual(ratio, _test_runner.CI_LOAD_GATE_DEFAULT_LOAD_RATIO)
                self.assertEqual(timeout, _test_runner.CI_LOAD_GATE_DEFAULT_TIMEOUT_SECONDS)
                self.assertIn("HAMS_CI_LOAD_GATE_RATIO", buf.getvalue())
                self.assertIn("HAMS_CI_LOAD_GATE_TIMEOUT", buf.getvalue())


class TestBrowserCountTests(unittest.TestCase):
    """`count_test_browser_processes()` replaced a box-wide
    `pgrep -c -f chrom`, which on a developer box with a desktop session was
    wrong in both directions. Measured 2026-09-16 with no test running: 27
    matching processes, 17 of them Bruce's own interactive Google Chrome and 8
    the Claude desktop application's Electron shell. The monitor duly printed
    "POSSIBLE BROWSER LEAK: 42 active Chromium processes" about a browser
    nobody had leaked -- and a real leak of twenty headless test browsers would
    equally have hidden inside that number on a quieter desktop day.

    Odoo's own ChromeBrowser._chrome_start() always passes
    `--remote-debugging-port`, and adds `--headless` unless watch mode is on.
    """

    def _proc(self, base, entries, collapsed=False):
        """`collapsed=True` writes the command line the way Chromium actually
        writes it -- see test_chromiums_real_collapsed_argv_is_counted."""
        for pid, cmdline in entries:
            d = os.path.join(base, str(pid))
            os.makedirs(d)
            with open(os.path.join(d, "cmdline"), "w", encoding="utf-8") as fh:
                if collapsed:
                    fh.write(" ".join(cmdline) + "\0")
                else:
                    fh.write("\0".join(cmdline) + "\0")
        os.makedirs(os.path.join(base, "self"), exist_ok=True)

    def test_chromiums_real_collapsed_argv_is_counted(self):
        """Chromium rewrites its own argv in place to set the process title,
        collapsing the whole command line into a single NUL-separated element.
        Measured 2026-09-16 against a real headless chromium on this box: every
        one of its processes had exactly ONE element, several hundred
        characters long.

        The first version of this counter split on NUL and took element zero as
        the executable, which for those processes is the tail of a
        `--user-data-dir` path -- so it counted a live headless browser as
        ZERO, while the box-wide method it replaced said 41. Its unit tests all
        passed, because they built their fixtures the way /proc is documented
        to work rather than the way Chromium actually writes it. Only running a
        real browser found it, which is why this fixture is a verbatim excerpt
        of one.
        """
        real = (
            "/usr/lib/chromium/chromium --type=renderer --top-chrome-webui "
            "--crashpad-handler-pid=2638436 --noerrdialogs "
            "--user-data-dir=/tmp/tmp.q3btHLah2S --no-sandbox "
            "--remote-debugging-port=9333 --ozone-platform=headless "
            "--lang=en-US --renderer-client-id=5"
        )
        with tempfile.TemporaryDirectory() as base:
            self._proc(base, [(601, [real])], collapsed=True)
            self.assertEqual(
                _test_runner.count_test_browser_processes(proc_root=base), 1
            )

    def test_a_collapsed_argv_that_is_not_a_browser_is_still_rejected(self):
        """The bash wrapper that launched the measurement above had the browser
        command in its own command line and was correctly rejected, because its
        executable is /bin/bash."""
        with tempfile.TemporaryDirectory() as base:
            self._proc(
                base,
                [(602, ["/bin/bash -c /usr/bin/chromium --headless "
                        "--remote-debugging-port=9333"])],
                collapsed=True,
            )
            self.assertEqual(
                _test_runner.count_test_browser_processes(proc_root=base), 0
            )

    def test_a_desktop_browser_is_not_counted(self):
        with tempfile.TemporaryDirectory() as base:
            self._proc(
                base,
                [
                    (101, ["/opt/google/chrome/chrome"]),
                    (102, ["/opt/google/chrome/chrome", "--type=renderer",
                           "--crashpad-handler-pid=101"]),
                    (103, ["/usr/lib/claude-desktop/chrome-sandbox",
                           "--type=utility"]),
                ],
            )
            self.assertEqual(
                _test_runner.count_test_browser_processes(proc_root=base), 0
            )

    def test_a_headless_test_browser_is_counted(self):
        with tempfile.TemporaryDirectory() as base:
            self._proc(
                base,
                [
                    (201, ["/usr/bin/chromium", "--headless",
                           "--remote-debugging-port=9222",
                           "--user-data-dir=/tmp/odoo_chrome_x"]),
                    (202, ["/usr/bin/chromium", "--type=renderer",
                           "--headless"]),
                    (203, ["/opt/google/chrome/chrome"]),  # the human's
                ],
            )
            self.assertEqual(
                _test_runner.count_test_browser_processes(proc_root=base), 2
            )

    def test_watch_mode_is_counted_even_though_it_is_not_headless(self):
        """`--pause-on-fail` runs the browser with a visible window, but
        ChromeBrowser still drives it over the DevTools protocol, so the
        debugging port is present in both modes and is the signal that covers
        watch mode."""
        with tempfile.TemporaryDirectory() as base:
            self._proc(
                base,
                [(301, ["/usr/bin/chromium", "--remote-debugging-port=9222"])],
            )
            self.assertEqual(
                _test_runner.count_test_browser_processes(proc_root=base), 1
            )

    def test_a_non_browser_holding_a_debugging_port_is_not_counted(self):
        """The switch alone is not enough -- the binary must be a browser."""
        with tempfile.TemporaryDirectory() as base:
            self._proc(
                base, [(401, ["node", "inspect", "--remote-debugging-port=9222"])]
            )
            self.assertEqual(
                _test_runner.count_test_browser_processes(proc_root=base), 0
            )

    def test_a_script_whose_own_text_mentions_a_browser_does_not_count_itself(self):
        """Matching the whole command line rather than the binary would count
        this very kind of process -- a script checking for browsers, whose
        `-c` argument necessarily contains both the word and the switch. That
        is the `pgrep -f` self-matching trap wearing a different hat, and it
        has sprung on this repository three times."""
        with tempfile.TemporaryDirectory() as base:
            self._proc(
                base,
                [
                    (402, ["python3", "-c",
                           "count chromium procs with --headless set"]),
                    (403, ["/bin/sh", "-c",
                           "pgrep -f 'chrom.*--remote-debugging-port'"]),
                ],
            )
            self.assertEqual(
                _test_runner.count_test_browser_processes(proc_root=base), 0
            )

    def test_the_browser_binary_alone_without_a_switch_is_not_counted(self):
        """A bare `chrome` with no DevTools switch is a human's browser."""
        with tempfile.TemporaryDirectory() as base:
            self._proc(base, [(404, ["/usr/bin/chromium"])])
            self.assertEqual(
                _test_runner.count_test_browser_processes(proc_root=base), 0
            )

    def test_an_unreadable_proc_counts_zero_rather_than_raising(self):
        self.assertEqual(
            _test_runner.count_test_browser_processes(
                proc_root="/nonexistent-proc-for-this-test"
            ),
            0,
        )

    def test_a_process_that_exits_mid_scan_is_skipped(self):
        with tempfile.TemporaryDirectory() as base:
            self._proc(
                base,
                [(501, ["/usr/bin/chromium", "--headless",
                        "--remote-debugging-port=9222"])],
            )
            os.makedirs(os.path.join(base, "502"))  # no cmdline file
            self.assertEqual(
                _test_runner.count_test_browser_processes(proc_root=base), 1
            )


class CiLoadGateOrderingTests(unittest.TestCase):
    """The gate must run BEFORE the box-wide single-instance lock is acquired.
    Waiting out somebody else's build matrix while holding that lock would stop
    every other session on the box from testing for the whole wait, converting
    one session's delay into everybody's.

    Asserted against the source text because the ordering is a property of
    main(), which cannot be called in a unit test without starting a real Odoo
    run. A source assertion is a weak test in general; here it is the only one
    that can fail if somebody moves the call, which is the regression worth
    catching."""

    def test_the_gate_call_precedes_the_flock_acquisition(self):
        source = io.open(_TEST_PY_PATH, encoding="utf-8").read()
        # The fail-fast path gates inline; the --wait path gates through lock_queue.acquire's on_head
        # callback, which runs only once this invocation is first in line and strictly before
        # try_lock() (asserted in test_lock_queue.py's gate tests).
        gate_at = source.index("if not use_queue and not wait_for_ci_load_to_subside():")
        lock_at = source.index("held_slot = _try_slot_locks(lock_file_path, slots, early_args.slot_held)")
        self.assertIn("on_head=None if adopted else wait_for_ci_load_to_subside", source)
        self.assertLess(
            gate_at,
            lock_at,
            "the continuous-integration load gate must run before the systemwide "
            "test lock is taken, or a wait blocks every other session on the box",
        )

class CheckLintersBurnListScopeTests(unittest.TestCase):
    """Regression tests for the burn-list scan-scope decision in check_linters(),
    per Bruce's own answer (night_shift_questions/answered/test-py-multi-target-burn-
    list-scope-4e892722.md, 2026-09-23): a run naming one or more specific modules
    scans only those modules' own directories, not the whole repository -- a
    multi-module run now behaves the same way as a single-module run, rather than
    getting the whole-repo treatment only a zero-module ("test everything") run still
    gets.

    subprocess.run is mocked for the whole function (check_linters() runs several
    other linters too -- manifest, init-imports, anchor scan, JS -- each via its own
    subprocess.run call); the mock inspects each call's own command list to tell the
    burn-list invocations apart from the others and returns a generic success result
    for everything, so the function runs to completion without exercising any real
    linter or touching the filesystem beyond what's asserted on directly.
    """

    def _mock_subprocess_run(self, burn_calls):
        def side_effect(cmd, *args, **kwargs):
            if any("check_burn_list.py" in str(part) for part in cmd):
                burn_calls.append(cmd)
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        return side_effect

    def test_a_single_named_module_scans_only_that_module(self):
        burn_calls = []
        with patch.object(
            _test_runner.subprocess, "run", side_effect=self._mock_subprocess_run(burn_calls)
        ):
            _test_runner.check_linters(
                "python3", "/repo", "/repo/.ignore", target_modules=["ham_events"]
            )
        self.assertEqual(len(burn_calls), 1, burn_calls)
        self.assertIn(os.path.join("/repo", "ham_events"), burn_calls[0])
        self.assertNotIn("/repo", [arg for arg in burn_calls[0] if arg == "/repo"])

    def test_two_named_modules_scan_only_those_two_modules_not_the_whole_repo(self):
        burn_calls = []
        with patch.object(
            _test_runner.subprocess, "run", side_effect=self._mock_subprocess_run(burn_calls)
        ):
            _test_runner.check_linters(
                "python3",
                "/repo",
                "/repo/.ignore",
                target_modules=["ham_events", "ham_repeater_dir"],
            )
        self.assertEqual(len(burn_calls), 2, burn_calls)
        scanned_targets = [call[2] for call in burn_calls]
        self.assertIn(os.path.join("/repo", "ham_events"), scanned_targets)
        self.assertIn(os.path.join("/repo", "ham_repeater_dir"), scanned_targets)
        # The whole-repo path itself must never be scanned for a named multi-module
        # run -- that's the exact regression this test exists to catch.
        self.assertNotIn("/repo", scanned_targets)

    def test_zero_named_modules_still_scans_the_whole_repo(self):
        burn_calls = []
        with patch.object(
            _test_runner.subprocess, "run", side_effect=self._mock_subprocess_run(burn_calls)
        ):
            _test_runner.check_linters("python3", "/repo", "/repo/.ignore", target_modules=None)
        self.assertEqual(len(burn_calls), 1, burn_calls)
        self.assertEqual(burn_calls[0][2], "/repo")

    def test_a_failure_in_either_named_module_halts_the_whole_run(self):
        # A multi-module run must still catch a real burn-list violation in EITHER
        # named module, not just the first one checked.
        def side_effect(cmd, *args, **kwargs):
            result = MagicMock()
            if any("check_burn_list.py" in str(part) for part in cmd) and cmd[2].endswith(
                "ham_repeater_dir"
            ):
                result.returncode = 1
                result.stdout = "some real violation"
                result.stderr = ""
            else:
                result.returncode = 0
                result.stdout = ""
                result.stderr = ""
            return result

        with patch.object(_test_runner.subprocess, "run", side_effect=side_effect):
            with self.assertRaises(SystemExit):
                _test_runner.check_linters(
                    "python3",
                    "/repo",
                    "/repo/.ignore",
                    target_modules=["ham_events", "ham_repeater_dir"],
                )


class SiblingCheckoutSelectionTests(unittest.TestCase):
    """A stale sibling hams_open silently supplied the harness; HAMS_OPEN_TREE and a warning fix that."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_hams_open_tree_overrides_the_sibling_scan(self):
        base = os.path.join(self.tmp, "hams_com")
        stale = os.path.join(self.tmp, "hams_open")
        fresh = os.path.join(self.tmp, "elsewhere", "fresh_open")
        for d in (base, stale, fresh):
            os.makedirs(d)
        with patch.dict(os.environ, {"HAMS_OPEN_TREE": fresh}):
            paths = _test_runner.get_addons_path(base).split(",")
        self.assertIn(fresh, paths)
        self.assertNotIn(stale, paths)

    def test_a_missing_hams_open_tree_is_fatal(self):
        base = os.path.join(self.tmp, "hams_com")
        os.makedirs(base)
        with patch.dict(os.environ, {"HAMS_OPEN_TREE": os.path.join(self.tmp, "nope")}):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                _test_runner.get_addons_path(base)

    def test_a_checkout_behind_origin_main_is_warned_about(self):
        result = MagicMock(returncode=0, stdout="7\n")
        err = io.StringIO()
        with patch.object(_test_runner.subprocess, "run", return_value=result), contextlib.redirect_stderr(err):
            _test_runner.warn_if_sibling_checkout_is_behind("/some/hams_open")
        self.assertIn("7 commit(s) behind", err.getvalue())

    def test_a_current_or_unreadable_checkout_is_silent(self):
        for result in (MagicMock(returncode=0, stdout="0\n"), MagicMock(returncode=128, stdout="")):
            err = io.StringIO()
            with patch.object(_test_runner.subprocess, "run", return_value=result), contextlib.redirect_stderr(err):
                _test_runner.warn_if_sibling_checkout_is_behind("/some/hams_open")
            self.assertEqual(err.getvalue(), "")


class OdooLauncherTests(unittest.TestCase):
    """Odoo is started through the launcher that installs the third-party warning policy (pyasn1/ldap3)."""

    def test_the_odoo_entry_point_is_the_launcher_beside_test_py(self):
        launcher = _test_runner.ODOO_LAUNCHER
        self.assertEqual(os.path.dirname(launcher), os.path.dirname(_TEST_PY_PATH))
        self.assertTrue(os.path.isfile(launcher))
        with open(launcher, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("odoo_warning_policy.install(odoo.netsvc)", source)
        self.assertIn("odoo.cli.main()", source)

    def test_test_py_launches_odoo_through_it_not_usr_bin_odoo(self):
        with open(_TEST_PY_PATH, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("odoo_bin = ODOO_LAUNCHER", source)
        self.assertNotIn('odoo_bin = "/usr/bin/odoo"', source)


class RunnerCommandLineTests(unittest.TestCase):
    """The command line is checked in the first second, and a refusal ends with the cause and the fix."""

    def _parse_error(self, argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            _test_runner.parse_runner_args(argv)
        self.assertEqual(caught.exception.code, 2)
        return err.getvalue()

    def test_an_unknown_option_is_refused_with_the_next_action_as_the_last_line(self):
        text = self._parse_error(["-u", "pager_duty", "--bogus"])
        self.assertIn("unrecognized arguments: --bogus", text)
        self.assertIn("NEXT:", text.strip().splitlines()[-1])

    def test_test_tags_pass_through_and_name_the_module_without_dash_u(self):
        args = _test_runner.parse_runner_args(["--test-tags", "/pager_duty:TestPagerIncidentStandard.test_01"])
        self.assertEqual(_test_runner.module_arg_for(args), "pager_duty")

    def test_test_tags_with_dash_u_keep_the_named_module(self):
        args = _test_runner.parse_runner_args(["-u", "a,b", "--test-tags", ":TestX"])
        self.assertEqual(_test_runner.module_arg_for(args), "a,b")

    def test_a_tag_selector_naming_no_module_needs_dash_u(self):
        self.assertIn("names no module", self._parse_error(["--test-tags", ":TestX.test_y"]))

    def test_test_tags_are_refused_outside_standard_mode_and_with_offline_isolation(self):
        self.assertIn("--mode standard", self._parse_error(["-m", "individual", "-u", "a", "--test-tags", "/a"]))
        self.assertIn("offline-isolation", self._parse_error(["-u", "a", "--offline-isolation", "--test-tags", "/a"]))

    def test_the_default_run_is_unchanged(self):
        args = _test_runner.parse_runner_args(["-u", "pager_duty"])
        self.assertIsNone(args.test_tags)
        self.assertFalse(args.build_daemons)

    def test_tag_spec_modules_skips_exclusions_and_deduplicates(self):
        self.assertEqual(
            _test_runner.tag_spec_modules("/a:T.t,-slow/b,/a:U,standard"), ["a"]
        )


class RustDaemonBuildNeededTests(unittest.TestCase):
    """A run of named modules that never mention a daemon crate skips the cargo build."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        for name, body in (("quiet", "x = 1\n"), ("loud", "BIN = 'hams_data_relay'\n")):
            os.makedirs(os.path.join(self.tmp, name, "tests"))
            with open(os.path.join(self.tmp, name, "tests", "test_a.py"), "w", encoding="utf-8") as f:
                f.write(body)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _needed(self, argv, modules):
        args = _test_runner.parse_runner_args(argv)
        return _test_runner.rust_daemon_build_needed(args, self.tmp, modules)

    def test_a_module_that_never_mentions_a_daemon_skips_the_build(self):
        self.assertFalse(self._needed(["-u", "quiet"], ["quiet"]))

    def test_a_module_that_mentions_a_daemon_builds(self):
        self.assertTrue(self._needed(["-u", "loud"], ["loud"]))

    def test_a_full_run_and_the_force_flag_build(self):
        self.assertTrue(self._needed([], ["quiet"]))
        self.assertTrue(self._needed(["-u", "quiet", "--build-daemons"], ["quiet"]))

    def test_an_unknown_module_directory_builds_to_be_safe(self):
        self.assertTrue(self._needed(["-u", "elsewhere"], ["elsewhere"]))


class HaltPreflightTests(unittest.TestCase):
    def test_the_cause_and_next_action_are_the_last_lines_and_the_extractor_is_marked(self):
        extractor = MagicMock(aborted=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as caught:
            _test_runner.halt_preflight(extractor, "lint findings.", "fix them.")
        self.assertEqual(caught.exception.code, 1)
        self.assertTrue(extractor.aborted)
        last_two = out.getvalue().strip().splitlines()[-2:]
        self.assertEqual(last_two, ["❌ PRE-FLIGHT FAILED: lint findings.", "   NEXT: fix them."])


class AddTestCompanionsTests(unittest.TestCase):
    """Tests [@ANCHOR: test:add_test_companions]

    `test.py -u ham_events` installed only ham_events' own dependency closure, so 12 to 17 tests that look up
    ham_communications_consent's service account errored locally (to-do 3e7a9c15); the remote runner added the module."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.tree = os.path.join(self.tmp, "ws", "hams_com")
        os.makedirs(os.path.join(self.tree, "devbox_tools"))
        os.makedirs(os.path.join(self.tmp, "ws", "hams_open"))

    def _gate(self, body):
        with open(os.path.join(self.tree, "devbox_tools", "asset_gate.py"), "w") as handle:
            handle.write(body)

    def test_the_companions_of_a_tested_module_are_installed_after_its_own_install_list(self):
        self._gate("def test_companions(module, trees):\n    return ['consent', 'base_mod'] if module == 'events' else []\n")
        modules, note = _test_runner.add_test_companions(["events", "base_mod"], ["events"], self.tree, environ={})
        self.assertEqual(modules, ["events", "base_mod", "consent"])
        self.assertIn("consent", note)
        self.assertNotIn("base_mod", note)

    def test_a_module_with_a_colon_test_selector_is_looked_up_by_its_module_name(self):
        self._gate("def test_companions(module, trees):\n    return ['consent'] if module == 'events' else []\n")
        modules, _note = _test_runner.add_test_companions(["events"], ["events:TestX.test_y"], self.tree, environ={})
        self.assertEqual(modules, ["events", "consent"])

    def test_without_an_asset_gate_nothing_is_added(self):
        modules, note = _test_runner.add_test_companions(["events"], ["events"], self.tree, environ={})
        self.assertEqual((modules, note), (["events"], ""))


class AnchorScanSiblingDirTests(unittest.TestCase):
    """Tests [@ANCHOR: test:anchor_scan_sibling_dir]

    A combined batch tree (~/workspace/tmp/batch_test_*/hams_com) has no hams_open beside it, so the pre-flight
    anchor scan ran on hams_com alone and reported ~200 false findings (found 2026-11-06 in a cloud session)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def _git(self, *args, cwd):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"] + list(args),
                       cwd=cwd, check=True, capture_output=True)

    def _main_checkout_with_sibling(self):
        main = os.path.join(self.tmp, "ws", "hams_com")
        os.makedirs(main)
        os.makedirs(os.path.join(self.tmp, "ws", "hams_open"))
        self._git("init", "-q", cwd=main)
        self._git("commit", "-q", "--allow-empty", "-m", "x", cwd=main)
        return main

    def test_the_sibling_beside_the_checkout_is_used(self):
        main = self._main_checkout_with_sibling()
        self.assertEqual(_test_runner.anchor_scan_sibling_dir(main, "hams_open", environ={}),
                         os.path.join(self.tmp, "ws", "hams_open"))

    def test_a_worktree_placed_anywhere_uses_the_main_checkouts_sibling(self):
        main = self._main_checkout_with_sibling()
        tree = os.path.join(self.tmp, "batch", "batch_test_hams_com_1", "hams_com")
        self._git("worktree", "add", "-q", "--detach", tree, cwd=main)
        self.assertEqual(_test_runner.anchor_scan_sibling_dir(tree, "hams_open", environ={}),
                         os.path.join(self.tmp, "ws", "hams_open"))

    def test_a_sibling_beside_a_worktree_wins_over_the_main_checkouts(self):
        main = self._main_checkout_with_sibling()
        tree = os.path.join(self.tmp, "pair", "hams_com")
        self._git("worktree", "add", "-q", "--detach", tree, cwd=main)
        paired = os.path.join(self.tmp, "pair", "hams_open")
        os.makedirs(paired)
        self.assertEqual(_test_runner.anchor_scan_sibling_dir(tree, "hams_open", environ={}), paired)

    def test_the_tree_variable_wins_and_a_missing_one_is_ignored(self):
        main = self._main_checkout_with_sibling()
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere)
        self.assertEqual(_test_runner.anchor_scan_sibling_dir(main, "hams_open", {"HAMS_OPEN_TREE": elsewhere}),
                         elsewhere)
        self.assertEqual(
            _test_runner.anchor_scan_sibling_dir(main, "hams_open", {"HAMS_OPEN_TREE": os.path.join(self.tmp, "no")}),
            os.path.join(self.tmp, "ws", "hams_open"))

    def test_no_sibling_anywhere_is_none(self):
        lone = os.path.join(self.tmp, "lone", "hams_com")
        os.makedirs(lone)
        self.assertIsNone(_test_runner.anchor_scan_sibling_dir(lone, "hams_open", environ={}))


class MissingHostAptPackagesTests(unittest.TestCase):
    """Tests [@ANCHOR: test:missing_host_apt_packages]
    Tests [@ANCHOR: test:check_host_apt_packages]
    Tests [@ANCHOR: infrastructure:pip_fallback_apt_packages]

    Ubuntu 24.04 has no awscli package; provisioning installs it with pip, and the pre-flight used to refuse the
    host anyway ("Missing required host APT packages: awscli")."""

    SPECS = [{"name": "awscli", "debian_name": "awscli"}, {"name": "cargo", "debian_name": "cargo-web"},
             {"name": "jing"}]

    def test_every_package_installed_is_nothing_missing(self):
        self.assertEqual(_test_runner.missing_host_apt_packages(self.SPECS, "ubuntu", lambda n: True, lambda n: False),
                         [])

    def test_awscli_from_pip_counts_as_installed(self):
        installed = {"cargo", "jing"}
        self.assertEqual(
            _test_runner.missing_host_apt_packages(self.SPECS, "ubuntu", installed.__contains__, {"awscli"}.__contains__),
            [])

    def test_awscli_absent_from_both_is_missing(self):
        installed = {"cargo", "jing"}
        self.assertEqual(
            _test_runner.missing_host_apt_packages(self.SPECS, "ubuntu", installed.__contains__, lambda n: False),
            ["awscli"])

    def test_a_package_without_a_pip_fallback_is_never_taken_from_pip(self):
        self.assertEqual(
            _test_runner.missing_host_apt_packages(self.SPECS, "ubuntu", lambda n: n == "awscli", lambda n: True),
            ["cargo", "jing"])

    def test_debian_names_are_checked_on_debian(self):
        seen = []
        _test_runner.missing_host_apt_packages(self.SPECS, "debian", lambda n: seen.append(n) or True, lambda n: False)
        self.assertEqual(seen, ["awscli", "cargo-web", "jing"])

    def test_cargo_on_path_satisfies_the_rust_toolchain_packages(self):
        # Provisioning skips the apt cargo when rustup's cargo is on PATH (infrastructure._filter_apt_install), and
        # the cloud container's pre-flight then stopped at "Missing required host APT packages: cargo" (2026-11-07).
        installed = {"awscli", "jing"}
        self.assertEqual(
            _test_runner.missing_host_apt_packages(self.SPECS, "ubuntu", installed.__contains__, lambda n: False,
                                                   have_cargo=True),
            [])
        self.assertEqual(
            _test_runner.missing_host_apt_packages(self.SPECS, "debian", installed.__contains__, lambda n: False,
                                                   have_cargo=True),
            [])
        self.assertEqual(
            _test_runner.missing_host_apt_packages(self.SPECS, "ubuntu", installed.__contains__, lambda n: False),
            ["cargo"])

    def test_the_fallback_map_names_awscli(self):
        self.assertEqual(_test_runner.infrastructure.PIP_FALLBACK_APT_PACKAGES.get("awscli"), "awscli")

    def test_check_exits_naming_only_what_is_really_missing(self):
        out = io.StringIO()
        with patch.object(_test_runner, "_dpkg_installed", lambda n: n != "awscli" and n != "jing"), \
                patch.object(_test_runner, "_pip_distribution_installed", lambda n: n == "awscli"), \
                patch.object(_test_runner.infrastructure, "MANIFEST", {"apt_packages": self.SPECS}), \
                patch.object(_test_runner.infrastructure, "get_os_identifier", lambda: "ubuntu"), \
                contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            _test_runner.check_host_apt_packages()
        self.assertIn("Missing required host APT packages: jing", out.getvalue())
        self.assertNotIn("awscli", out.getvalue())


if __name__ == "__main__":
    unittest.main()
