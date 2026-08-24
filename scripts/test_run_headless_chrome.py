#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
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

CAUTION for whoever runs this file: reap_headless_chromes() is
deliberately broad by design (matches ANY chrome/chromium-named process
with --headless in its cmdline, owned by the current UID -- not scoped to
processes this script itself spawned). That's the tool's own real,
intended behavior (its own module docstring: "Sequential execution only
is required"), not a test artifact -- but it means these tests must not
run concurrently with anything else on this box that's using headless
Chrome (e.g. an Odoo test.py tour run), or it will kill that too. Run
this file in isolation.
"""

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
    )
    p = psutil.Process(proc.pid)
    deadline = time.time() + 10.0
    while time.time() < deadline and p.name() != "chrome":
        time.sleep(0.1)  # audit-ignore-sleep
    return proc


class ReapHeadlessChromesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        # Best-effort: if a test somehow left a real chrome process
        # running, don't let it survive past this test file.
        rhc.reap_headless_chromes()

    def test_a_real_running_headless_chrome_process_is_killed(self):
        proc = _spawn_real_headless_chrome(self.tmp)
        self.assertIsNone(
            proc.poll(),
            "test setup assumption: the spawned chrome process must actually be alive",
        )

        rhc.reap_headless_chromes()

        try:
            exit_code = proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            exit_code = None

        self.assertIsNotNone(
            exit_code,
            "reap_headless_chromes() must actually terminate a real headless "
            "chrome process owned by the current user",
        )

    def test_no_running_chromes_does_not_raise(self):
        # The common case (nothing to reap) must be a silent no-op, not an
        # exception -- reap_headless_chromes() is called unconditionally at
        # both startup and on every termination signal.
        rhc.reap_headless_chromes()


if __name__ == "__main__":
    unittest.main()
