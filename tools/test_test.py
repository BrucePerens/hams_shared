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
        # IN_JULES_VM/JULES_SESSION_ID are explicitly cleared so this test
        # doesn't depend on whatever the real ambient environment happens
        # to have set.
        env_overrides = {
            "HAMS_ISOLATED_NS": "1",
            "HAMS_TEST_LOCK_HELD": "1",
            "IN_JULES_VM": "",
            "JULES_SESSION_ID": "",
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

    def test_get_chrome_count_warns_and_returns_zero_on_a_real_pgrep_failure(self):
        with patch.object(_test_runner.subprocess, "run", side_effect=OSError("pgrep missing")):
            with self.assertLogs(_test_runner.__name__, level="WARNING") as cm:
                result = self.monitor.get_chrome_count()
        self.assertEqual(result, 0)
        self.assertTrue(any("pgrep" in msg for msg in cm.output))


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


class StartJulesDaemonsInjectionTests(unittest.TestCase):
    """start_jules_daemons() builds a Python script as an f-string and runs it
    via `sudo -E python3 -c <script>` -- base_dir and $USER used to be embedded
    as raw f-string text inside single quotes, so a value containing a quote
    could break out of the string literal and inject arbitrary code into a
    script that runs as root. Fixed via `!r` (repr) escaping; these tests never
    let a real `sudo`/`mkdir`/`chmod`/script-execution happen -- subprocess.run
    is mocked throughout, and the assertions are against the generated script
    text itself."""

    def test_a_base_dir_and_user_containing_a_quote_do_not_break_out_of_the_generated_script(self):
        malicious = "/tmp/evil'; os.system('touch /tmp/pwned'); x = '"
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return MagicMock(returncode=0)

        with patch.object(_test_runner.subprocess, "run", side_effect=fake_run), \
                patch.dict(os.environ, {"USER": malicious}):
            _test_runner.start_jules_daemons(malicious)

        sudo_calls = [c for c in calls if c and c[0] == "sudo"]
        self.assertEqual(
            len(sudo_calls), 1,
            "expected exactly one `sudo -E <python> -c <script>` invocation",
        )
        script = sudo_calls[0][-1]

        # Pre-fix, the unescaped quote in `malicious` breaks the string literal
        # boundary, and the payload becomes real top-level statements -- this
        # compile() call raises SyntaxError against the pre-fix code (the broken-
        # out `x = '...'` never finds its matching close-quote) or, if it happens
        # to parse, the injected os.system(...) call would appear as bare,
        # executable source rather than inert string data.
        compile(script, "<generated-jules-script>", "exec")
        self.assertNotIn("os.system('touch /tmp/pwned')\n", script)


if __name__ == "__main__":
    unittest.main()
