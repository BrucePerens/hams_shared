#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for run_headless_chrome.py. This script is dev/CI tooling, not
shipped product code -- but every other file in hams_shared/tools/ already
carries its own test_*.py (72/72), and this is the one script under
scripts/ that had none. reap_headless_chromes() matters for real: this
whole codebase's test.py runs headless Chrome repeatedly, and a stray
zombie chrome process left behind by a killed/crashed run is exactly the
kind of thing that silently accumulates and eats memory/disk across a long
session (matching this environment's own history of resource exhaustion
from unbounded test-run byproducts).

reap_headless_chromes(user_data_dir) is scoped: it kills only the Chrome
processes of the current UID whose --user-data-dir is the given profile, so
this file is safe to run while other sessions on this box use headless
Chrome (an Odoo test.py tour run, another screenshot). Each test also stops
the Chromes it started itself, by their own profile.
"""

import os
import shutil
import subprocess
import tempfile
import time
import unittest

import psutil

import run_headless_chrome as rhc


def _spawn_real_headless_chrome(tmp_dir):
    """
    Spawns a real, minimal google-chrome --headless process (not chromium
    -- the script's own name-match list checks 'google-chrome' first, and
    that's the binary this dev box actually has). Returns the raw
    subprocess.Popen object -- not just a psutil.Process wrapper around
    its pid, and this matters: once reap_headless_chromes() kills it, the
    process becomes a zombie until *this* process (its real parent) calls
    wait()/poll() on it, and psutil.Process.is_running() reports True for
    a zombie it doesn't own the reaping of -- confirmed directly, not
    assumed, after this test's first version failed with the process
    genuinely killed (system-wide, no matching processes left) but
    is_running() still true. Popen.poll() is the correct check: it's a
    real wait() call, so it actually reaps the zombie and returns the
    real exit status.

    --user-data-dir is a fresh temp dir so this never collides with a
    profile lock any other real chrome instance running in this same
    test session might be holding.

    Waits for the process to settle into its final identity before
    returning -- confirmed directly, not assumed: /usr/bin/google-chrome
    is a bash wrapper (`exec -a "$0" "$HERE/chrome" "$@"`, itself
    forking `cat` subshells for stdout/stderr redirection first), so
    immediately after Popen() returns, psutil still sees a /bin/bash
    process, not yet the real chrome binary.
    """
    proc = subprocess.Popen(
        [
            "google-chrome",
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            f"--user-data-dir={tmp_dir}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        # Chrome also writes its own runtime dir (com.google.Chrome.XXXX with the singleton
        # socket) under TMPDIR, and is SIGKILLed by this test, so point TMPDIR inside the
        # profile dir this test removes afterwards instead of leaking into the shared /tmp.
        env={**os.environ, "TMPDIR": tmp_dir},
    )
    p = psutil.Process(proc.pid)
    deadline = time.time() + 10.0
    while time.time() < deadline and p.name() != "chrome":
        time.sleep(0.1)  # audit-ignore-sleep
    return proc


class ReapHeadlessChromesTests(unittest.TestCase):
    # Tests [@ANCHOR: run_headless_chrome_scoped_reap]
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # Cleanups run LIFO after tearDown: chromes are stopped before rmtree.
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _profile(self, name):
        path = os.path.join(self.tmp, name)
        os.mkdir(path)
        return path

    def _spawn(self, profile):
        proc = _spawn_real_headless_chrome(profile)
        self.addCleanup(self._stop, proc, profile)
        self.assertIsNone(
            proc.poll(),
            "test setup assumption: the spawned chrome process must actually be alive",
        )
        return proc

    @staticmethod
    def _stop(proc, profile):
        # Scoped to this test's own profile, so other sessions' Chromes survive.
        rhc.reap_headless_chromes(profile)
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5.0)

    def _assert_exits(self, proc):
        try:
            exit_code = proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            exit_code = None
        self.assertIsNotNone(
            exit_code,
            "reap_headless_chromes() must terminate a real headless chrome "
            "process that uses the given profile directory",
        )

    def test_a_chrome_using_the_given_profile_is_killed(self):
        profile = self._profile("own")
        proc = self._spawn(profile)

        rhc.reap_headless_chromes(profile)

        self._assert_exits(proc)

    def test_a_chrome_using_another_profile_is_spared(self):
        # The to-do this fixes: one wrapper run must not kill a headless
        # Chrome it did not start (another session's, or a test.py tour's).
        other = self._spawn(self._profile("other_session"))
        mine = self._profile("own")

        rhc.reap_headless_chromes(mine)
        time.sleep(1.0)  # audit-ignore-sleep

        self.assertIsNone(
            other.poll(),
            "reap_headless_chromes() killed a headless chrome using a "
            "different --user-data-dir",
        )
        self.assertEqual(
            psutil.Process(other.pid).name(),
            "chrome",
            "the spared process must still be the real chrome binary",
        )

    def test_profile_paths_are_compared_after_normalisation(self):
        # A trailing slash or a symlinked path names the same profile.
        profile = self._profile("own")
        proc = self._spawn(profile)
        link = os.path.join(self.tmp, "link_to_own")
        os.symlink(profile, link)

        rhc.reap_headless_chromes(link + "/")

        self._assert_exits(proc)

    def test_no_running_chromes_does_not_raise(self):
        # The common case (nothing to reap) must be a silent no-op, not an
        # exception -- reap_headless_chromes() is called unconditionally at
        # startup, on every termination signal and on exit.
        rhc.reap_headless_chromes(self._profile("empty"))


class BuildChromeCommandTests(unittest.TestCase):
    # Tests [@ANCHOR: run_headless_chrome_own_profile_dir]
    def test_user_data_dir_from_args(self):
        self.assertIsNone(rhc.user_data_dir_from_args(["--headless=new", "about:blank"]))
        self.assertEqual(
            rhc.user_data_dir_from_args(["--user-data-dir=/a", "--user-data-dir=/b"]),
            "/b",
            "Chrome uses the last --user-data-dir switch",
        )

    def test_caller_profile_is_used_and_not_owned(self):
        args = ["--headless=new", "--user-data-dir=/some/profile", "about:blank"]
        cmd, profile, owned = rhc.build_chrome_command(args)
        self.assertEqual(cmd, ["google-chrome"] + args)
        self.assertEqual(profile, "/some/profile")
        self.assertFalse(owned)

    def test_wrapper_creates_its_own_profile_when_none_given(self):
        args = ["--headless=new", "about:blank"]
        cmd, profile, owned = rhc.build_chrome_command(args)
        self.addCleanup(shutil.rmtree, profile, ignore_errors=True)
        self.assertTrue(owned)
        self.assertTrue(os.path.isdir(profile))
        self.assertTrue(os.path.basename(profile).startswith(rhc.PROFILE_DIR_PREFIX))
        self.assertEqual(cmd, ["google-chrome"] + args + ["--user-data-dir=" + profile])


if __name__ == "__main__":
    unittest.main()
