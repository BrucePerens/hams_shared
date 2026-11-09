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


# Upper bound (a safety net, not a delay) on how long to keep killing and waiting for the Chromes of one profile. The reaper
# returns as soon as no process uses the profile; with several lint slots and a load of 50 to 100 a SIGKILLed Chrome with many
# children took longer than the 1 s and 5 s this once was, and the tests below failed in most full lint runs (2026-11-08).
REAP_WAIT_SECONDS = 60.0
REAP_POLL_SECONDS = 0.05


def reap_headless_chromes(user_data_dir, wait_seconds=None):
    """Kill the Chrome processes of this UID that use user_data_dir as profile, until none is left.

    Chromes using any other profile (other sessions' browsers) are left alone. Each pass kills every matching process and polls
    for them to leave the process table; a new pass then rescans, because a Chrome that was starting can have spawned a child
    (zygote, renderer) after the previous scan. It returns when a scan finds nothing, with no fixed sleep, so it is as fast as the
    machine allows and as patient as `wait_seconds` (default REAP_WAIT_SECONDS) when the machine is loaded. Returns the set of
    pids it signalled. A process that outlives the deadline is reported on stderr and left to the caller.
    """
    real_profile = os.path.realpath(os.path.expanduser(user_data_dir))
    limit = REAP_WAIT_SECONDS if wait_seconds is None else wait_seconds
    deadline = time.monotonic() + limit
    killed = set()
    while True:
        batch = list(_matching_chromes(real_profile))
        if not batch:
            return killed
        for p in batch:
            try:
                if p.pid not in killed:
                    print(f"[*] Killing headless chrome process {p.pid} (profile {real_profile})")
                p.kill()
                killed.add(p.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess) as e:
                print(f"[*] Skipping a process that vanished mid-scan: {e}", file=sys.stderr)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print(f"[!] Chrome processes using {real_profile} are still present after {limit:.0f} s", file=sys.stderr)
            return killed
        # Poll the profile, not the pids: a killed Chrome whose parent has not reaped it is a zombie with an empty command line,
        # which no longer uses the profile, while psutil's own wait would treat it as running until the parent collects it.
        time.sleep(REAP_POLL_SECONDS)  # audit-ignore-sleep


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
