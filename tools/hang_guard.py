#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Wall-clock limits for lint steps and test runs, and the hang report written BEFORE a timed-out process is stopped
(Bruce, 2026-11-09, after the merge queue sat 48 minutes in a test that waited on a socket for ever).

* `step_limit` / `run_cap`: the limit of one step (its median duration of the last runs x HAMS_LINT_STEP_FACTOR, kept between a floor and
  a hard cap; a step never seen gets HAMS_LINT_STEP_DEFAULT; `HAMS_LINT_STEP_TIMEOUT_<NAME>` sets one step exactly) and of the whole run.
* `write_hang_report`: the process tree under a pid with, per process, the state, wait channel, kernel stack (when readable), open
  files and sockets, the socket table lines and a `py-spy dump` (when py-spy is installed). Written to a file; its path is returned.
* `run_limited`: `subprocess.run` with a limit. On timeout the report is written first, then ONLY the child's own process group is
  stopped (verified: it leads its own session, was started by this call, still has the start time recorded when it started).

Configuration (environment): HAMS_LINT_STEP_FACTOR (4), HAMS_LINT_STEP_FLOOR (300 s), HAMS_LINT_STEP_CAP (3600 s), HAMS_LINT_STEP_DEFAULT
(1800 s), HAMS_LINT_RUN_CAP (7200 s), HAMS_LINT_STEP_TIMEOUT_<NAME> (NAME = step name upper-cased, every non-alphanumeric run -> `_`),
HAMS_HANG_REPORT_DIR (default ~/.cache/hams/hang_reports).
"""

import math
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_procs  # noqa: E402

FACTOR, FLOOR, CAP, NO_HISTORY, RUN_CAP = 4.0, 300.0, 3600.0, 1800.0, 7200.0
RECENT_RUNS = 7
STOP_GRACE = 10.0
TIMED_OUT_CODE = 124  # the exit status `timeout(1)` uses


def number(environ, key, default):
    try:
        value = float(environ.get(key, ""))
    except ValueError:
        return default
    return value if math.isfinite(value) and value > 0 else default


# [@ANCHOR: hang_guard:step_env_name]
def step_env_name(step_name):
    """`HAMS_LINT_STEP_TIMEOUT_<NAME>` for a step: the name upper-cased, each run of other characters one `_`."""
    return "HAMS_LINT_STEP_TIMEOUT_" + re.sub(r"[^A-Z0-9]+", "_", step_name.upper()).strip("_")


# [@ANCHOR: hang_guard:step_limit]
def step_limit(step_name, recent_seconds, environ=None, scale=1.0):
    """Seconds one step may run. A `HAMS_LINT_STEP_TIMEOUT_<NAME>` is used as given. Otherwise: the median of `recent_seconds` times the
    factor, at least the floor and at most the cap; no history -> the default (also at most the cap). The derived value is stretched by
    `scale` (the load factor of lint_exec.timeout_scale), the cap still applies after it."""
    environ = os.environ if environ is None else environ
    exact = number(environ, step_env_name(step_name), 0)
    if exact:
        return exact
    cap = number(environ, "HAMS_LINT_STEP_CAP", CAP)
    runs = [s for s in recent_seconds or () if s and s > 0]
    if runs:
        base = max(number(environ, "HAMS_LINT_STEP_FLOOR", FLOOR), statistics.median(runs) * number(environ, "HAMS_LINT_STEP_FACTOR", FACTOR))
    else:
        base = number(environ, "HAMS_LINT_STEP_DEFAULT", NO_HISTORY)
    return min(cap, base * max(1.0, scale))


# [@ANCHOR: hang_guard:run_cap]
def run_cap(environ=None):
    """Seconds a whole lint run may take (HAMS_LINT_RUN_CAP)."""
    return number(os.environ if environ is None else environ, "HAMS_LINT_RUN_CAP", RUN_CAP)


# [@ANCHOR: hang_guard:remember]
def remember(history, name, seconds, keep=RECENT_RUNS):
    """Append `seconds` to `history[name]` (a list kept to the last `keep` runs); returns the list."""
    runs = history.setdefault(name, [])
    runs.append(round(seconds, 2))
    del runs[:-keep]
    return runs


def report_dir(environ=None):
    environ = os.environ if environ is None else environ
    return environ.get("HAMS_HANG_REPORT_DIR") or os.path.join(os.path.expanduser("~"), ".cache", "hams", "hang_reports")


def _text(path, limit=4000):
    try:
        with open(path, "r", errors="replace") as handle:
            return handle.read(limit).strip()
    except OSError as error:
        return f"(unreadable: {error.strerror or error})"


def _fds(pid, limit=60):
    lines = []
    try:
        names = sorted(os.listdir(f"/proc/{pid}/fd"), key=int)
    except OSError as error:
        return [f"(no fd list: {error.strerror or error})"]
    for name in names[:limit]:
        try:
            lines.append(f"{name} -> {os.readlink(f'/proc/{pid}/fd/{name}')}")
        except OSError:
            continue
    if len(names) > limit:
        lines.append(f"... {len(names) - limit} more")
    return lines


def _run_tool(argv, timeout=20):
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, errors="replace")
    except (OSError, subprocess.SubprocessError) as error:
        return f"({argv[0]} failed: {error})"
    return (done.stdout + done.stderr).strip()[:12000]


# [@ANCHOR: hang_guard:process_section]
def process_section(info, run_tool=_run_tool, which=shutil.which):
    """The report text of one process: state, wait channel, syscall, kernel stack, open files, its sockets and, for a Python process,
    a py-spy dump (when py-spy is on the PATH; otherwise a note, so the reader knows the Python stack was not captured)."""
    pid = info.pid
    out = [f"=== pid {pid} ppid {info.ppid} pgid {info.pgid} state {info.state}: {' '.join(info.argv)[:300]}",
           f"wchan: {_text(f'/proc/{pid}/wchan')}", f"syscall: {_text(f'/proc/{pid}/syscall')}",
           f"kernel stack: {_text(f'/proc/{pid}/stack') or '(empty)'}", "open files and sockets:"]
    out += ["  " + line for line in _fds(pid)]
    if which("ss"):
        sockets = [line for line in run_tool(["ss", "-tunapH"]).splitlines() + run_tool(["ss", "-xapH"]).splitlines() if f"pid={pid}," in line]
        out += ["sockets (ss):"] + ["  " + line[:200] for line in sockets[:40]]
    if info.comm.startswith(("python", "pytest")):
        spy = which("py-spy")
        out.append("py-spy dump:\n" + run_tool([spy, "dump", "--pid", str(pid)]) if spy else
                   "py-spy dump: not installed (set PYTEST_ADDOPTS faulthandler_timeout, or install py-spy)")
    return "\n".join(out)


# [@ANCHOR: hang_guard:write_hang_report]
def write_hang_report(root_pid, label, reason, directory=None, snapshot=agent_procs.snapshot, section=process_section, clock=time.time):
    """Write the hang report of `root_pid` and its descendants to `<directory>/<time>_<label>.txt`; returns the path, or None when the
    directory cannot be written (the report is then printed to stderr so it is not lost)."""
    table = snapshot()
    pids = sorted(({root_pid} | agent_procs.descendants(table, root_pid)) & set(table))
    now = clock()
    if not math.isfinite(now):  # an injected clock is not trusted: localtime() raises on nan and inf
        now = time.time()
    body = [f"hang report: {label}", f"reason: {reason}", f"time: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))}",
            f"process tree of pid {root_pid}: {len(pids)} process(es)"]
    body += [agent_procs.ps_line(table[p]) for p in pids]
    body += ["", *(section(table[p]) for p in pids)]
    text = "\n".join(body) + "\n"
    directory = directory or report_dir()
    path = os.path.join(directory, time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + "_" + re.sub(r"[^A-Za-z0-9._-]+", "_", label)[:80] + ".txt")
    try:
        os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
    except OSError as error:
        print(f"(hang report not written: {error})\n{text}", file=sys.stderr)
        return None
    return path


# [@ANCHOR: hang_guard:stop_own_group]
def stop_own_group(pid, start_ticks, grace=STOP_GRACE, snapshot=agent_procs.snapshot, sleep=time.sleep, clock=time.monotonic):
    """SIGTERM, then after `grace` seconds SIGKILL, to the process group that child `pid` leads. Refuses (returns False without signalling)
    unless `pid` still has `start_ticks` (not a reused pid), leads its own group and session (it was started with `start_new_session`) and
    that group is not this process's own. Returns True when the group has no live member afterwards."""
    info = snapshot().get(pid)
    if info is None:
        return True
    if info.start_ticks != start_ticks or info.pgid != pid or info.sid != pid or info.pgid == os.getpgrp():
        return False
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 5.0)):
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            return True
        deadline = clock() + wait
        while agent_procs.group_members(snapshot(), pid, pid) and clock() < deadline:
            sleep(0.1)
        if not agent_procs.group_members(snapshot(), pid, pid):
            return True
    return False


# [@ANCHOR: hang_guard:run_limited]
def run_limited(argv, limit, label, directory=None, **popen_kwargs):
    """`subprocess.run(argv, capture_output, text)` with a wall-clock `limit` in seconds. Returns (completed process, report path or None,
    timed_out). On timeout: the hang report is written first, then the child's own group is stopped (`stop_own_group`); the output read so
    far is returned with returncode 124."""
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace", start_new_session=True, **popen_kwargs)
    info = agent_procs.read_proc(proc.pid)
    start_ticks = info.start_ticks if info else 0
    try:
        out, err = proc.communicate(timeout=limit)
        return subprocess.CompletedProcess(argv, proc.returncode, out, err), None, False
    except subprocess.TimeoutExpired:
        pass
    path = write_hang_report(proc.pid, label, f"still running after {limit:.0f} s (wall-clock limit)", directory)
    stop_own_group(proc.pid, start_ticks)
    try:
        out, err = proc.communicate(timeout=STOP_GRACE)
    except subprocess.TimeoutExpired:  # something outside the group still holds the pipes: what was read is lost, the step still ends
        out, err = "", ""
    return subprocess.CompletedProcess(argv, TIMED_OUT_CODE, out, err), path, True
