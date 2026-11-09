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
from unittest.mock import patch

import psutil

import run_headless_chrome as rhc


# Safety nets only: every wait below ends as soon as its condition holds.
SETTLE_SECONDS = 180.0
EXIT_SECONDS = 180.0


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
    # Wait on the condition, not for ten seconds: under a load of 100 the wrapper took longer than that to exec the real binary, the
    # old loop gave up while the process was still `bash`, and the reaper (which matches Chrome by name and profile) then rightly
    # did not kill it, so the test failed with "must terminate a real headless chrome" about 30 s later. The bound is only a safety
    # net, and an unsettled process is a setup failure that says so, never a half-started test.
    deadline = time.time() + SETTLE_SECONDS
    while time.time() < deadline and proc.poll() is None and p.name() != "chrome":
        time.sleep(0.1)  # audit-ignore-sleep
    if proc.poll() is not None or p.name() != "chrome":
        raise AssertionError(
            "test setup: the spawned process did not become the chrome binary within %.0f s (exit status %s, name %s)"
            % (SETTLE_SECONDS, proc.poll(), None if proc.poll() is not None else p.name())
        )
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
            proc.wait(timeout=EXIT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=EXIT_SECONDS)

    def _assert_exits(self, proc):
        try:
            exit_code = proc.wait(timeout=EXIT_SECONDS)
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

        mine_killed = rhc.reap_headless_chromes(mine)
        # Deterministic, no waiting for a wrong kill to show up: the reaper reports every pid it signalled, and by the time it
        # returns it has seen no process of its profile left, so another profile's chrome is either in that set or untouched.
        self.assertEqual(mine_killed, set(), "nothing used the profile, so nothing may be signalled")
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

    def test_the_other_profile_stays_alive_while_this_profile_is_reaped(self):
        # Both profiles in use: reaping one signals only its processes and leaves the other's browser running.
        other = self._spawn(self._profile("other_session"))
        mine = self._profile("own")
        mine_proc = self._spawn(mine)

        killed = rhc.reap_headless_chromes(mine)

        self._assert_exits(mine_proc)
        self.assertIn(mine_proc.pid, killed)
        self.assertNotIn(other.pid, killed)
        self.assertIsNone(other.poll(), "the other profile's chrome must keep running")

    def test_profile_paths_are_compared_after_normalisation(self):
        # A trailing slash or a symlinked path names the same profile.
        profile = self._profile("own")
        proc = self._spawn(profile)
        link = os.path.join(self.tmp, "link_to_own")
        os.symlink(profile, link)

        rhc.reap_headless_chromes(link + "/")

        self._assert_exits(proc)

    def test_the_reaper_rescans_until_a_scan_finds_nothing(self):
        # A Chrome that was starting can spawn a child after the first scan; the reaper must kill that one too instead of
        # sleeping a fixed time and scanning once more.
        class Fake:
            def __init__(self, pid):
                self.pid = pid
                self.kills = 0

            def kill(self):
                self.kills += 1

        first, late = Fake(10), Fake(11)
        scans = iter([[first], [first, late], [late], []])
        with patch.object(rhc, "_matching_chromes", lambda profile: iter(next(scans))), \
             patch.object(rhc.time, "sleep") as slept:
            killed = rhc.reap_headless_chromes(self.tmp)
        self.assertEqual(killed, {10, 11})
        self.assertGreaterEqual(first.kills, 1)
        self.assertGreaterEqual(late.kills, 1)
        self.assertEqual(slept.call_count, 3, "one short poll between scans, none once a scan is empty")

    def test_a_chrome_that_will_not_die_is_given_up_on_at_the_deadline(self):
        class Fake:
            pid = 12

            def kill(self):
                pass

        with patch.object(rhc, "_matching_chromes", lambda profile: iter([Fake()])), \
             patch.object(rhc.time, "sleep"), \
             patch.object(rhc.time, "monotonic", side_effect=[0.0, 1.0, 2.0, 3.0]), \
             patch("sys.stderr") as err:
            killed = rhc.reap_headless_chromes(self.tmp, wait_seconds=2.0)
        self.assertEqual(killed, {12})
        self.assertIn("still present", "".join(c.args[0] for c in err.write.call_args_list))

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
