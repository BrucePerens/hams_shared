#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Shared parts of the unattended overnight tools (`mutation_run.py`, `fuzz_run.py`, and the hams_com `devbox_tools/overnight/` scripts):
the load and capacity verdict, a named-job runner on `agent_procs.py`, the append-only log, atomic JSON files and the targets-file reader.

Rules these tools keep (Bruce, 2026-11-09): mechanical work runs without agent turns and an agent is started only for a finding that needs
judgment; every process wait and stop goes through `agent_procs.py` (no pgrep, pkill or kill); nothing is committed or pushed; every script
is idempotent and safe to re-run. Reads only; the verdict never starts or probes anything. Full entry: tools/README.md "Overnight automation".
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import tomllib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import agent_procs  # noqa: E402
import dispatch_ok  # noqa: E402
import atomic_io

DEFAULT_REPORTS = os.path.join(os.path.expanduser("~"), "workspace", "tmp", "reports")
MIN_MEM_AVAILABLE_GB = 6.0  # below this the idle-fill work stops (the load display's own HOLD limit is 10 GB; this is the hard floor)
EXIT_NOT_IDLE = 4


# [@ANCHOR: overnight_common:reports_dir]
def reports_dir(environ=None):
    """`$HAMS_REPORTS_DIR`, else `~/workspace/tmp/reports`. Holds `overnight.log`, `mutation/`, `fuzz/`, `findings/`. Pure."""
    environ = os.environ if environ is None else environ
    return environ.get("HAMS_REPORTS_DIR") or DEFAULT_REPORTS


# [@ANCHOR: overnight_common:log_line]
def log_line(path, text, now=time.time):
    """Appends `YYYY-MM-DD HH:MM:SS text` (one line, newlines folded) to `path`, creating the directory. Never raises on a write error."""
    line = "%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now())), " ".join(str(text).split()))
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        pass
    return line


# [@ANCHOR: overnight_common:write_json]
def write_json(path, data):
    """Writes `data` as JSON to `path` through a temporary file and `os.replace`, so a reader never sees half a file."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    atomic_io.atomic_write(path, json.dumps(data, indent=1, sort_keys=True) + "\n", mode=0o644)


def read_json(path, default=None):
    """The JSON in `path`, or `default` when it is missing or unreadable."""
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


# [@ANCHOR: overnight_common:slug]
def slug(text, limit=48):
    """A file- and job-name-safe form of `text` (letters, digits, `.`, `_`, `-`), never empty, at most `limit` characters."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(text)).strip("-._") or "x"
    return cleaned[:limit]


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# [@ANCHOR: overnight_common:load_targets]
def load_targets(path, required=("name",), home=None):
    """The `[[target]]` tables of a TOML targets file, each a dict with `~` expanded in string values that start with it. Raises
    `ValueError` (with the file name and the target's number) for a file that is not TOML, has no `[[target]]`, lacks a required key,
    or repeats a name. Targets whose `enabled` is false are dropped."""
    try:
        with open(path, "rb") as handle:
            document = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ValueError("%s: cannot read the targets file: %s" % (path, error)) from error
    targets = document.get("target")
    if not isinstance(targets, list) or not targets:
        raise ValueError("%s: no [[target]] table" % path)
    home = home or os.path.expanduser("~")
    seen, result = set(), []
    for number, target in enumerate(targets, 1):
        for key in required:
            if key not in target:
                raise ValueError("%s: target %d has no `%s`" % (path, number, key))
        if target["name"] in seen:
            raise ValueError("%s: target name %r is used twice" % (path, target["name"]))
        seen.add(target["name"])
        if target.get("enabled", True) is False:
            continue
        result.append({k: (home + v[1:] if isinstance(v, str) and v.startswith("~") else v) for k, v in target.items()})
    return result


# [@ANCHOR: overnight_common:Load]
class Load:
    """What the load display says right now. `state` is OK or HOLD (dispatch_ok), `reasons` its text, `test_running`/`test_slots`/`lint_running`
    the queue figures, `mem_available_gb` from /proc/meminfo (None when unreadable)."""

    def __init__(self, state, reasons, test_running=0, test_slots=1, lint_running=0, lint_slots=1, mem_available_gb=None):
        self.state, self.reasons = state, list(reasons)
        self.test_running, self.test_slots = test_running, test_slots
        self.lint_running, self.lint_slots = lint_running, lint_slots
        self.mem_available_gb = mem_available_gb

    def test_free(self):
        return max(0, self.test_slots - self.test_running)

    def lint_free(self):
        return max(0, self.lint_slots - self.lint_running)

    def critical(self):
        """Why the idle-fill work must stop, or None: the load display says HOLD, or memory is under the floor."""
        if self.state != "OK":
            return "load HOLD: " + "; ".join(self.reasons)
        if self.mem_available_gb is not None and self.mem_available_gb < MIN_MEM_AVAILABLE_GB:
            return "memory low: %.1f GB available, floor %.1f GB" % (self.mem_available_gb, MIN_MEM_AVAILABLE_GB)
        return None


def mem_available_gb(meminfo="/proc/meminfo"):
    try:
        with open(meminfo, encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1048576.0
    except (OSError, ValueError, IndexError):
        pass
    return None


# [@ANCHOR: overnight_common:read_load]
def read_load(stats_path=dispatch_ok.DEFAULT_STATS, meminfo="/proc/meminfo", now=time.time, lint_slots=None):
    """The `Load` now: `dispatch_ok.evaluate` for the verdict (a missing or stale load display is HOLD), the stats file's `queues` and
    `test_slots` for the slot use, `lint_lock.slot_count` for the lint slots (`lint_slots` overrides, for tests)."""
    state, reasons = dispatch_ok.evaluate(stats_path, now=now)
    data = read_json(stats_path, {})
    data = data if isinstance(data, dict) else {}
    queues = data.get("queues") if isinstance(data.get("queues"), dict) else {}
    if lint_slots is None:
        try:
            import lint_lock
            lint_slots = lint_lock.slot_count()
        except Exception:  # a broken slot setting must not stop the verdict: one slot is the safe answer
            lint_slots = 1

    def number(value, default):
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else default

    return Load(state, reasons, number(queues.get("test_running"), 0), max(1, number(data.get("test_slots"), 1)),
                number(queues.get("lint_running"), 0), lint_slots, mem_available_gb(meminfo))


# [@ANCHOR: overnight_common:overnight_common_job_result_class]
class JobResult:
    """`status` is `ok` (exit 0), `failed` (non-zero), `timeout` (stopped at the time limit), `error` (could not start or no exit record)."""

    def __init__(self, status, exit_code, log, text=""):
        self.status, self.exit_code, self.log, self.text = status, exit_code, log, text

    def __repr__(self):
        return "JobResult(%s, %s, %s)" % (self.status, self.exit_code, self.log)


def _agent_procs(*args, env=None):
    return subprocess.run([sys.executable, os.path.join(HERE, "agent_procs.py"), *args], capture_output=True, text=True, env=env)


# [@ANCHOR: overnight_common:run_job]
def run_job(name, argv, cwd=None, timeout=3600.0, env=None, grace=30.0):
    """Runs `argv` as the named job `name` through `agent_procs.py run`, blocks in ONE `agent_procs.py wait --job NAME --timeout T`, and
    when the time limit passes stops the job with `agent_procs.py stop NAME` (SIGTERM to its own group, never SIGKILL). Returns a
    `JobResult` (the job's log is `~/.cache/hams-jobs/NAME.log` or under `$HAMS_JOBS_DIR`). A job of that name that is still running is an
    `error` result (nothing started). No pgrep, kill or pid from ps is involved."""
    name = slug(name, 64)
    environ = dict(os.environ if env is None else env)
    started = _agent_procs("run", name, *(["--cwd", cwd] if cwd else []), "--", *argv, env=environ)
    log = os.path.join(agent_procs.jobs_dir(environ), name + ".log")
    if started.returncode != 0:
        return JobResult("error", None, log, (started.stderr or started.stdout).strip()[-400:])
    waited = _agent_procs("wait", "--job", name, "--timeout", "%g" % timeout, env=environ)
    if waited.returncode == agent_procs.EX_TIMEOUT:
        _agent_procs("stop", name, "--grace", "%g" % grace, env=environ)
        return JobResult("timeout", None, log, "stopped after %g s" % timeout)
    code = None
    old = os.environ.get("HAMS_JOBS_DIR")
    if environ.get("HAMS_JOBS_DIR"):
        os.environ["HAMS_JOBS_DIR"] = environ["HAMS_JOBS_DIR"]
    try:
        for record in agent_procs.list_records():
            if record.name == name:
                code = record.exit_code
    finally:
        if old is None:
            os.environ.pop("HAMS_JOBS_DIR", None)
        else:
            os.environ["HAMS_JOBS_DIR"] = old
    if code is None:
        return JobResult("error", None, log, (waited.stdout or waited.stderr).strip()[-400:])
    return JobResult("ok" if code == 0 else "failed", code, log)


def tail(path, lines=40):
    """The last `lines` lines of a text file, or "" when it cannot be read."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return "".join(handle.readlines()[-lines:])
    except OSError:
        return ""
