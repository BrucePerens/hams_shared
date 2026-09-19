#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
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
import importlib.util
import io
import inspect
import itertools
import os
import shutil
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
        gate_at = source.index("if not wait_for_ci_load_to_subside():")
        lock_at = source.index("fcntl.flock(_single_instance_lock")
        self.assertLess(
            gate_at,
            lock_at,
            "the continuous-integration load gate must run before the systemwide "
            "test lock is taken, or a wait blocks every other session on the box",
        )

if __name__ == "__main__":
    unittest.main()
