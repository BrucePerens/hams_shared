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

import importlib.util
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


if __name__ == "__main__":
    unittest.main()
