#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Run one google-chrome invocation (typically --headless --screenshot=...) and
make sure every Chrome process it started is gone when it finishes.

The cleanup is scoped to Chromes using this run's own profile directory
(--user-data-dir). Many sessions share this box as the same user, so killing
every headless Chrome the user owns would also kill other sessions' browsers
(a screenshot Chrome, or the tour Chrome of a test.py run).

When the caller gives no --user-data-dir, the wrapper creates a fresh one with
the prefix PROFILE_DIR_PREFIX, passes it to Chrome, and removes it on exit.
"""
import os
import sys
import fcntl
import psutil
import shutil
import tempfile
import time
import signal
import subprocess

LOCK_FILE = "/tmp/run_headless_chrome.lock"
PROFILE_DIR_PREFIX = "run_headless_chrome-"
USER_DATA_DIR_FLAG = "--user-data-dir="
CHROME_PROCESS_NAMES = ("chrome", "chromium", "chromium-browser", "google-chrome")


def user_data_dir_from_args(args):
    """Return the value of the last --user-data-dir=... in args, or None.

    Chrome only accepts the '=' form of this switch, and the last one wins.
    """
    found = None
    for arg in args:
        if str(arg).startswith(USER_DATA_DIR_FLAG):
            found = str(arg)[len(USER_DATA_DIR_FLAG):]
    return found


def build_chrome_command(args):
    """Return (cmd, user_data_dir, wrapper_owns_dir) for one wrapper run.

    [@ANCHOR: run_headless_chrome_own_profile_dir]
    Without a caller-supplied --user-data-dir, a new profile directory is made
    here, added to the Chrome command line, and owned (removed) by the wrapper.
    """
    user_data_dir = user_data_dir_from_args(args)
    if user_data_dir:
        return ["google-chrome"] + list(args), user_data_dir, False
    user_data_dir = tempfile.mkdtemp(prefix=PROFILE_DIR_PREFIX)
    cmd = ["google-chrome"] + list(args) + [USER_DATA_DIR_FLAG + user_data_dir]
    return cmd, user_data_dir, True


def _uses_profile(cmdline, real_profile):
    """True when a process command line uses exactly the given profile dir."""
    value = user_data_dir_from_args(cmdline)
    return bool(value) and os.path.realpath(os.path.expanduser(value)) == real_profile


def _matching_chromes(real_profile):
    # [@ANCHOR: run_headless_chrome_scoped_reap]
    # Only Chrome processes of this UID whose --user-data-dir is this run's
    # profile. Every Chrome child (zygote, renderer, GPU, utility) carries the
    # --user-data-dir switch, but not all of them carry --headless, so the
    # profile is the match key and --headless is not required.
    my_uid = os.getuid()
    for p in psutil.process_iter(["pid", "name", "uids", "cmdline"]):
        try:
            if (
                p.info.get("name") in CHROME_PROCESS_NAMES
                and p.info.get("uids")
                and p.info["uids"].real == my_uid
                and _uses_profile(p.info.get("cmdline") or [], real_profile)
            ):
                yield p
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, KeyError) as e:
            # Expected and harmless: the process can exit between
            # process_iter()'s snapshot and this loop body inspecting it.
            print(f"[*] Skipping a process that vanished mid-scan: {e}", file=sys.stderr)


def reap_headless_chromes(user_data_dir):
    """Kill the Chrome processes of this UID that use user_data_dir as profile.

    Chromes using any other profile (other sessions' browsers) are left alone.
    """
    real_profile = os.path.realpath(os.path.expanduser(user_data_dir))
    for p in _matching_chromes(real_profile):
        try:
            print(f"[*] Killing headless chrome process {p.pid} (profile {real_profile})")
            p.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess) as e:
            print(f"[*] Skipping a process that vanished mid-scan: {e}", file=sys.stderr)

    time.sleep(0.5)  # audit-ignore-sleep
    for p in _matching_chromes(real_profile):
        try:
            p.kill()
            p.wait(timeout=1.0)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, psutil.TimeoutExpired) as e:
            # Same benign race as above -- the process can exit or fail to
            # reap within the timeout between the scan and this inspection.
            print(f"[*] Skipping a process that vanished or didn't reap in time: {e}", file=sys.stderr)


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 run_headless_chrome.py [google-chrome args...]")
        sys.exit(1)

    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Sequential execution only is required. Another headless browser is currently running.", file=sys.stderr)
        sys.exit(1)

    cmd, user_data_dir, wrapper_owns_dir = build_chrome_command(sys.argv[1:])

    # 1. Kill any Chrome still using this profile before starting (only
    #    possible for a caller-supplied --user-data-dir; a new one is empty).
    reap_headless_chromes(user_data_dir)

    # 2. Launch google-chrome in a new process group
    print(f"[*] Launching headless chrome: {' '.join(cmd)}")

    process = subprocess.Popen(cmd, preexec_fn=os.setsid)

    def remove_own_profile_dir():
        if wrapper_owns_dir:
            shutil.rmtree(user_data_dir, ignore_errors=True)

    def terminate_process(signum, frame):
        print(f"\n[!] Received signal {signum}. Forcefully terminating headless chrome...")
        try:
            pgid = os.getpgid(process.pid)
            os.killpg(pgid, signal.SIGTERM)
        except OSError as e:
            print(f"[!] Could not signal process group for PID {process.pid}: {e}", file=sys.stderr)
        reap_headless_chromes(user_data_dir)
        remove_own_profile_dir()
        sys.exit(1)

    signal.signal(signal.SIGTERM, terminate_process)
    signal.signal(signal.SIGINT, terminate_process)

    try:
        process.wait()
    finally:
        print(f"[*] Ensuring process group for PID {process.pid} is terminated...")
        pgid = None
        try:
            pgid = os.getpgid(process.pid)
            os.killpg(pgid, signal.SIGTERM)
        except OSError as e:
            print(f"[!] Could not signal process group for PID {process.pid}: {e}", file=sys.stderr)

        # Fallback: kill any Chrome still using this run's profile
        reap_headless_chromes(user_data_dir)

        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if pgid is not None:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except OSError as e:
                    print(f"[!] Could not SIGKILL process group {pgid}: {e}", file=sys.stderr)
        remove_own_profile_dir()


if __name__ == "__main__":
    main()
