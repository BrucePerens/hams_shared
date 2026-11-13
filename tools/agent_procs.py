#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Find, wait for, run, list and stop processes without the mistakes agents keep making (Bruce, 2026-11-08).

The mistakes, all seen on one night:
  * `kill -9 $(pgrep -f "[x]yz")`: the pattern also matched a PostgreSQL backend whose process title held the database
    name; SIGKILL of one backend crashed the whole shared cluster.
  * `until ! pgrep -f "tools/test.py"; do sleep 3; done` inside `bash -c '...'`: the waiter's own command line holds the
    pattern, so it matches itself (and its parents) and never ends, or the kill takes the agent's own shell down (exit 144).
  * a pid copied from an earlier command named a different process by the time it was used (pids are reused).
  * waiting on a background job by polling its output file instead of waiting on the exact process or exit marker.

What this tool does instead: a process is named by its pid AND its start time (field 22 of /proc/PID/stat, so a reused pid is
detected), a long job is started as a NAMED JOB in its own session with a record of its pid, process group and exit code, and a
pattern search never lists the searcher, its ancestors or its descendants. Pure /proc, standard library only.

    agent_procs.py find   [--cwd-under PATH] [--cmd-contains TEXT | --cmd-regex RE] [--user U] [--any-user] [--pid-file F]
    agent_procs.py wait   (--pid N [--start-time T] | --job NAME | --file PATH [--contains RE] [--fail-contains RE]
                          | SELECTOR [--all] [--allow-missing])     SELECTOR: --cwd-under/--cmd-contains/--cmd-regex; one match
                          required (several: error listing them, or --all; none: error, or --allow-missing)
                          [--while-pid N] [--timeout S]       exit 0 done, 1 failed, 75 timeout, 2 usage, 3 refused
    agent_procs.py run    NAME [--tag AGENT] [--cwd DIR] [--timeout S] [--mem-max SIZE] [--nice N] -- COMMAND...
                          a named job; log in ~/.cache/hams-jobs/NAME.log; ends after 1800 s (`--timeout 0`: never) and is
                          capped at 6G (`--mem-max none`: no cap; systemd-run --user scope, else ulimit -v); a timeout stops
                          the whole process group (exit 124, reason `timeout` in the record); the command dies with its wrapper
    agent_procs.py jobs   [--json]                         shows each job's timeout and memory cap
    agent_procs.py reap   [--min-age S] [--apply] [--force]   your orphans: cwd a deleted path under /storage/agent_work, or
                          parent the systemd user manager and cwd under it, older than 900 s; dry run unless --apply
    agent_procs.py stop   NAME [--grace S] [--force]          SIGTERM the job's process group; SIGKILL only with --force
    agent_procs.py kill-pid PID (--start-time T | --cwd-under PATH) [--grace S] [--force]    one exact, verified pid

Refused always (exit 3, no flag overrides it except `--i-am-the-owner-and-checked` for the caller's own recorded job): a process of
another user, root or postgres; any command containing postgres, postmaster, redis-server, rabbitmq, beam.smp, epmd, systemd, sshd,
gdm, gnome or claude-desktop; the claude application; a test.py run outside the caller's own worktree; a chrome of a test run
outside the caller's own TMPDIR; pid 1; the caller's own ancestry. Full documentation of every function: tools/README.md
"Watching and stopping processes (`agent_procs.py`)".
"""

import argparse
import dataclasses
import datetime
import fcntl
import functools
import json
import math
import os
import pwd
import re
import shutil
import signal
import subprocess
import sys
import time
import atomic_io

CLK_TCK = os.sysconf("SC_CLK_TCK")
EX_OK = 0
EX_FAILED = 1
EX_USAGE = 2
EX_REFUSED = 3
EX_TIMEOUT = 75
DEFAULT_WAIT_TIMEOUT = 1800.0
DEFAULT_GRACE = 10.0
DEFAULT_TIMEOUT = 1800
DEFAULT_MEM_MAX = "6G"
KILL_GRACE = 10
AGENT_WORK_ROOT = "/storage/agent_work"
DEFAULT_MIN_AGE = 900
EX_TIMEOUT_JOB = 124
POLL_INTERVAL = 0.5
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
PROTECTED_WORDS = (
    "postgres", "postmaster", "redis-server", "rabbitmq", "beam.smp", "epmd", "systemd", "sshd", "gdm", "gnome",
    "claude-desktop",
)
PROTECTED_USERS = ("postgres", "root")
ALLOWED_SIGNALS = {
    "TERM": signal.SIGTERM, "INT": signal.SIGINT, "HUP": signal.SIGHUP, "QUIT": signal.SIGQUIT,
    "USR1": signal.SIGUSR1, "USR2": signal.SIGUSR2,
}
OWNER_OVERRIDE_FLAG = "--i-am-the-owner-and-checked"


# [@ANCHOR: agent_procs:agent_procs_proc_error_class]
class ProcError(Exception):
    """A refusal or usage error with the exit code the CLI should use (`code`)."""

    def __init__(self, message, code=EX_USAGE):
        super().__init__(message)
        self.code = code


# [@ANCHOR: agent_procs:ProcInfo]
@dataclasses.dataclass(frozen=True)
class ProcInfo:
    """One process as read from /proc at one instant. Never refreshed: read again to see a change."""

    pid: int
    ppid: int
    pgid: int
    sid: int
    uid: int
    user: str
    state: str
    start_ticks: int
    comm: str
    argv: tuple
    cwd: object


# [@ANCHOR: agent_procs:JobRecord]
@dataclasses.dataclass
class JobRecord:
    """The file `<jobs dir>/<name>.json` of a named job, written by `start_job`, completed by the wrapper at the end."""

    name: str
    tag: str
    pid: int
    pgid: int
    sid: int
    start_ticks: int
    cwd: str
    argv: list
    log: str
    started_at: float
    user: str
    exit_code: object = None
    ended_at: object = None
    stopped: object = None
    timeout: int = 0
    mem_max: object = None
    exit_reason: object = None


# [@ANCHOR: agent_procs:WaitResult]
@dataclasses.dataclass(frozen=True)
class WaitResult:
    """The outcome of a `wait_*` function: `code` is the process exit code to use, `message` one line for the human."""

    code: int
    message: str


# [@ANCHOR: agent_procs:user_name]
def user_name(uid):
    """The login name of `uid`, or its number as text when the account is unknown. Never raises."""
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


# [@ANCHOR: agent_procs:boot_time]
def boot_time():
    """Seconds since the epoch at which the machine booted (`btime` of /proc/stat); 0.0 when unreadable."""
    try:
        with open("/proc/stat") as handle:
            for line in handle:
                if line.startswith("btime "):
                    value = float(line.split()[1])
                    return value if math.isfinite(value) else 0.0
    except (OSError, ValueError):
        pass
    return 0.0


# [@ANCHOR: agent_procs:format_start]
def format_start(start_ticks):
    """The local wall-clock start time (`YYYY-MM-DDTHH:MM:SS`) of a process whose /proc start time is `start_ticks`."""
    when = boot_time() + start_ticks / CLK_TCK
    try:
        return datetime.datetime.fromtimestamp(when).strftime("%Y-%m-%dT%H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return "unknown"  # a record file with a corrupt start time must not stop `agent_procs find`/`wait`


START_TIME_FORMAT = ("the `ticks=` number that `agent_procs.py find` prints for the process (field 22 of /proc/PID/stat, clock "
                     "ticks since boot, e.g. 6209871), written `6209871` or `ticks=6209871`; the `start=` timestamp of the same "
                     "line (e.g. 2026-11-09T09:37:10) is accepted too")


# [@ANCHOR: agent_procs:parse_start_time]
def parse_start_time(value):
    """A process start time as given by a person, as a half-open range `(low, high)` of /proc clock ticks.

    Accepts an int or digits (ticks), `ticks=N`, a `(low, high)` tuple (returned as is), and the `start=YYYY-MM-DDTHH:MM:SS`
    timestamp `find` prints (a whole second: `CLK_TCK` ticks wide). Anything else raises `ProcError` code 2 naming the
    expected format: an unreadable start time must never be taken for "the process is gone"."""
    if isinstance(value, tuple):
        return value
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return (value, value + 1)
    text = str(value).strip()
    if text.startswith("ticks="):
        text = text[len("ticks="):]
    elif text.startswith("start="):
        text = text[len("start="):]
    if re.fullmatch(r"[0-9]{1,15}", text):
        return (int(text), int(text) + 1)
    try:
        when = datetime.datetime.strptime(text.replace(" ", "T"), "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        raise ProcError(f"cannot read the start time {value!r}; expected {START_TIME_FORMAT}") from None
    low = round((when.timestamp() - boot_time()) * CLK_TCK)
    if low < 0:
        raise ProcError(f"the start time {value!r} is before this machine booted; expected {START_TIME_FORMAT}")
    return (low, low + CLK_TCK)


# [@ANCHOR: agent_procs:check_start_time]
def check_start_time(info, spec):
    """Compare the start time a person named (`parse_start_time`) with the live process `info` (`None` when the pid is gone).

    Returns `"same"` when it is that process, `"reused"` when `info` started AFTER the named time (the pid was handed to a
    new process, so the named one is gone), `"gone"` when `info` is `None` and the time is plausible. Raises `ProcError`
    code 2 for a start time that cannot belong to this pid: in the future (milliseconds or epoch seconds, not ticks), later
    than the process now holding the pid (a pid cannot be older than its owner's start: wrong pid or wrong number), or that
    process's start in seconds instead of ticks. A mismatch that cannot be explained is never reported as "gone"."""
    low, high = spec
    uptime_ticks = (time.time() - boot_time()) * CLK_TCK
    if low > uptime_ticks + 2 * CLK_TCK:
        raise ProcError(f"start time {low} is in the future (this machine has been up {uptime_ticks:.0f} ticks): "
                        f"that is not clock ticks since boot (epoch seconds or milliseconds?). Expected {START_TIME_FORMAT}")
    if info is None:
        return "gone"
    if low <= info.start_ticks < high:
        return "same"
    if info.start_ticks < low:
        raise ProcError(f"start time {low} does not fit pid {info.pid}: that process started at ticks={info.start_ticks}, "
                        f"before the time you named, so the pid cannot be the one you meant. Wrong pid or a wrong number "
                        f"(a wrapper's pid, or a start time from another process)? Expected {START_TIME_FORMAT}")
    if high == low + 1 and low * CLK_TCK <= info.start_ticks < (low + 1) * CLK_TCK:
        raise ProcError(f"start time {low} looks like seconds since boot, but ticks are {CLK_TCK} per second: pid {info.pid} "
                        f"started at ticks={info.start_ticks}. Expected {START_TIME_FORMAT}")
    return "reused"


# [@ANCHOR: agent_procs:read_proc]
def read_proc(pid):
    """Read one process from /proc. Returns a `ProcInfo`, or `None` when it does not exist or vanished while being read.

    The start time is field 22 of /proc/PID/stat, parsed after the LAST `)` because the command name may hold one."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            raw = handle.read().decode("utf-8", "replace")
        comm = raw[raw.index("(") + 1:raw.rindex(")")]
        fields = raw[raw.rindex(")") + 2:].split()
        uid = os.stat(f"/proc/{pid}").st_uid
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            parts = handle.read().decode("utf-8", "replace").split("\0")
    except (OSError, ValueError, IndexError):
        return None
    argv = tuple(part for part in parts if part != "") or (f"[{comm}]",)
    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        cwd = None
    try:
        return ProcInfo(pid, int(fields[1]), int(fields[2]), int(fields[3]), uid, user_name(uid), fields[0],
                        int(fields[19]), comm, argv, cwd)
    except (IndexError, ValueError):
        return None


# [@ANCHOR: agent_procs:snapshot]
def snapshot():
    """Every readable process, as `{pid: ProcInfo}`. One pass over /proc; processes that exit during it are omitted."""
    table = {}
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            info = read_proc(int(entry))
            if info is not None:
                table[info.pid] = info
    return table


# [@ANCHOR: agent_procs:ancestry]
def ancestry(table, pid):
    """The set of `pid` and all of its ancestors (parent, grandparent, ... up to pid 1) according to `table`."""
    seen = set()
    while pid and pid not in seen:
        seen.add(pid)
        info = table.get(pid)
        if info is None:
            break
        pid = info.ppid
    return seen


# [@ANCHOR: agent_procs:descendants]
def descendants(table, pid):
    """The set of all children, grandchildren, ... of `pid` according to `table` (not `pid` itself)."""
    children = {}
    for info in table.values():
        children.setdefault(info.ppid, []).append(info.pid)
    found = set()
    todo = [pid]
    while todo:
        for child in children.get(todo.pop(), ()):
            if child not in found:
                found.add(child)
                todo.append(child)
    return found


# [@ANCHOR: agent_procs:self_family]
def self_family(table, me=None):
    """The pids no search may list and no signal may reach: this process, its ancestors and its descendants.

    This is what stops a waiter from matching itself: the `bash -c '...'` that holds the pattern in its command line is an
    ancestor of the process that searches."""
    me = os.getpid() if me is None else me
    return ancestry(table, me) | descendants(table, me)


# [@ANCHOR: agent_procs:is_under]
def is_under(path, root):
    """True when `path` is `root` or inside it, after `realpath` of both. False when either is empty or `None`."""
    if not path or not root:
        return False
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root.rstrip("/") + "/")


# [@ANCHOR: agent_procs:caller_worktree]
def caller_worktree(environ=None, cwd=None):
    """The git worktree root of the caller: `HAMS_AGENT_WORKTREE` if set, else the nearest directory holding `.git` at or
    above `cwd` (default the current directory), else `None` (then every test.py run counts as someone else's)."""
    environ = os.environ if environ is None else environ
    if environ.get("HAMS_AGENT_WORKTREE"):
        return environ["HAMS_AGENT_WORKTREE"]
    here = os.path.realpath(cwd or os.getcwd())
    while True:
        if os.path.exists(os.path.join(here, ".git")):
            return here
        parent = os.path.dirname(here)
        if parent == here:
            return None
        here = parent


# [@ANCHOR: agent_procs:chrome_data_dir]
def chrome_data_dir(argv):
    """The `--user-data-dir=` value of a chrome command line, or `None`."""
    for arg in argv:
        if arg.startswith("--user-data-dir="):
            return arg.split("=", 1)[1]
    return None


# [@ANCHOR: agent_procs:protected_reason]
def protected_reason(info, caller_uid=None, worktree=None, tmpdir=None, family=()):
    """Why `info` must never be signalled by an agent, or `None` when it is the caller's own ordinary process.

    Arguments: `info` a `ProcInfo`; `caller_uid` the signalling user (default the real uid); `worktree` the caller's own
    worktree root (see `caller_worktree`); `tmpdir` the caller's `TMPDIR` (`None`: every chrome is protected); `family` pids of
    the caller's ancestry. Returns a sentence naming the rule. The rules are in the module docstring; they are checked in the
    order: pid 1 and ancestry, other users and system accounts, protected program names, the claude application, test.py
    outside the worktree, chrome outside the TMPDIR."""
    caller_uid = os.getuid() if caller_uid is None else caller_uid
    haystack = (info.comm + " " + " ".join(info.argv)).lower()
    base = os.path.basename(info.argv[0]).lower()
    if info.pid == 1:
        return "pid 1 is the init system"
    if info.pid in family:
        return "it is the caller's own process or one of its ancestors (the shell running this command)"
    if info.uid != caller_uid or info.uid == 0 or info.user in PROTECTED_USERS:
        return f"it is owned by {info.user}, not by the caller (system and other users' processes are never signalled)"
    for word in PROTECTED_WORDS:
        if word in haystack:
            return f"its command contains '{word}' (a shared service; a signal can crash everyone's work)"
    if info.comm in ("claude", "Claude") or base in ("claude", "claude-desktop"):
        return "it is the claude application"
    if "test.py" in haystack and not is_under(info.cwd, worktree):
        return "it is a test.py run that is not under the caller's own worktree"
    if "chrome" in haystack or "chromium" in haystack:
        if not tmpdir or not (is_under(chrome_data_dir(info.argv), tmpdir) or is_under(info.cwd, tmpdir)):
            return "it is a chrome/chromium that is not under the caller's own TMPDIR"
    return None


# [@ANCHOR: agent_procs:ps_line]
def ps_line(info):
    """One line describing `info`, printed before any signal: pid, owner, start time and ticks, group, cwd, full arguments."""
    return (f"pid={info.pid} user={info.user} start={format_start(info.start_ticks)} ticks={info.start_ticks} "
            f"pgid={info.pgid} cwd={info.cwd or '?'} args={' '.join(info.argv)}")


# [@ANCHOR: agent_procs:shell_clones]
def shell_clones(table, ancestors):
    """Pids of the forked copies of the caller's own shells: a child of one of `ancestors` whose command line is identical to
    an ancestor's. A shell forks a subshell for each element of a pipeline (`wait --cmd-contains X | tail`) and the copy
    carries the whole command text, so a text search for X would find "the waiter's own shell". Never a process that merely
    shares a parent with different arguments."""
    texts = {info.argv for pid, info in table.items() if pid in ancestors and len(info.argv) >= 3}
    return {pid for pid, info in table.items() if info.ppid in ancestors and info.argv in texts}


# [@ANCHOR: agent_procs:find_procs]
def find_procs(table, me=None, user=None, any_user=False, cwd_under=None, contains=None, regex=None, pids=None,
               caller_uid=None, worktree=None, tmpdir=None):
    """Search `table` and return `[(ProcInfo, protected_reason_or_None), ...]` sorted by pid.

    Never returns the caller (`me`, default this process), its ancestors or its descendants. `user` (default the caller's
    own login) restricts to one owner; any other user than the caller needs `any_user=True`, which lists every owner.
    `cwd_under` keeps processes whose working directory is under it; `contains` is a LITERAL substring of the full argument
    text; `regex` is a regular expression searched in that text; `pids` is a mapping `{pid: start_ticks or None}` that restricts
    the result to those pids (a given start time must match). All filters must hold. Raises `ProcError` for a bad regex or for
    another user without `any_user`."""
    caller_uid = os.getuid() if caller_uid is None else caller_uid
    family = self_family(table, me)
    family |= shell_clones(table, ancestry(table, me or os.getpid()))
    own_name = user_name(caller_uid)
    if user is not None and user != own_name and not any_user:
        raise ProcError(f"--user {user} would list another user's processes: add --any-user (they are shown as protected)")
    try:
        pattern = re.compile(regex) if regex else None
    except re.error as error:
        raise ProcError(f"bad --cmd-regex: {error}")
    found = []
    for pid, info in sorted(table.items()):
        text = " ".join(info.argv)
        if pid in family:
            continue
        if not any_user and info.uid != caller_uid:
            continue
        if user is not None and info.user != user:
            continue
        if cwd_under and not is_under(info.cwd, cwd_under):
            continue
        if contains and contains not in text:
            continue
        if pattern and not pattern.search(text):
            continue
        if pids is not None and (pid not in pids or (pids[pid] is not None and pids[pid] != info.start_ticks)):
            continue
        found.append((info, protected_reason(info, caller_uid, worktree, tmpdir, ancestry(table, me or os.getpid()))))
    return found


# [@ANCHOR: agent_procs:read_pid_file]
def read_pid_file(path):
    """Parse a pid file into `{pid: start_ticks or None}`. Each line is `PID` or `PID:START_TICKS`; blank lines and `#`
    comments are ignored. Raises `ProcError` on an unreadable file or a malformed line."""
    result = {}
    try:
        with open(path) as handle:
            lines = handle.read().splitlines()
    except OSError as error:
        raise ProcError(f"cannot read --pid-file: {error}")
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        match = re.fullmatch(r"(\d+)(?::(\d+))?", line)
        if not match:
            raise ProcError(f"bad line in --pid-file: {line!r} (want PID or PID:START_TICKS)")
        result[int(match.group(1))] = int(match.group(2)) if match.group(2) else None
    return result


# [@ANCHOR: agent_procs:jobs_dir]
def jobs_dir(environ=None):
    """The directory of job records: `HAMS_JOBS_DIR`, else `~/.cache/hams-jobs`. Created (mode 0700) when missing."""
    environ = os.environ if environ is None else environ
    path = environ.get("HAMS_JOBS_DIR") or os.path.join(os.path.expanduser("~"), ".cache", "hams-jobs")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


# [@ANCHOR: agent_procs:check_job_name]
def check_job_name(name):
    """Return `name` when it is a safe job name (letters, digits, `.`, `_`, `-`; 1-64 chars; starts alphanumeric), else
    raise `ProcError`. The name becomes a file name, so no `/` and no leading dot."""
    if not _NAME_RE.match(name or ""):
        raise ProcError(f"bad job name {name!r}: use letters, digits, '.', '_' or '-' (1-64 characters, alphanumeric first)")
    if name == "guard":  # list_records skips guard.json (the hook's own name), so such a job would never be listed or counted
        raise ProcError("bad job name 'guard': that name is reserved for the signal guard hook")
    return name


# [@ANCHOR: agent_procs:record_path]
def record_path(name, directory=None):
    """The path of the record file of job `name` (validated by `check_job_name`)."""
    return os.path.join(directory or jobs_dir(), check_job_name(name) + ".json")


# [@ANCHOR: agent_procs:write_record]
def write_record(rec, directory=None):
    """Write `rec` (a `JobRecord`) atomically: a temporary file in the same directory, then `os.replace`."""
    path = record_path(rec.name, directory)
    atomic_io.atomic_write(path, json.dumps(dataclasses.asdict(rec), indent=1) + "\n", mode=0o644)


# [@ANCHOR: agent_procs:read_record]
def read_record(name, directory=None):
    """Load the `JobRecord` of job `name`, or `None` when there is no record or it is unreadable."""
    try:
        with open(record_path(name, directory)) as handle:
            return JobRecord(**json.load(handle))
    except (OSError, ValueError, TypeError):
        return None


# [@ANCHOR: agent_procs:list_records]
def list_records(directory=None):
    """Every readable `JobRecord` in the jobs directory, sorted by start time. A file whose name is not a valid job name (a
    stray `.hidden.json`) is not a record and is skipped."""
    directory = directory or jobs_dir()
    records = []
    for entry in os.listdir(directory):
        if entry.endswith(".json") and entry != "guard.json" and _NAME_RE.match(entry[:-5]):
            rec = read_record(entry[:-5], directory)
            if rec is not None:
                records.append(rec)
    return sorted(records, key=lambda rec: rec.started_at)


# [@ANCHOR: agent_procs:pid_alive]
def pid_alive(pid, start_ticks):
    """True when process `pid` exists, is not a zombie, and its start time is exactly `start_ticks` (so a reused pid is
    NOT alive for this question)."""
    info = read_proc(pid)
    return info is not None and info.start_ticks == start_ticks and info.state not in ("Z", "X")


# [@ANCHOR: agent_procs:signal_same_process]
def signal_same_process(pid, start_ticks, signum):
    """Send `signum` to `pid` only if it is still the process that started at `start_ticks`, with no window for pid reuse.

    Opens a pidfd (a handle that keeps naming that one process even if the number is reused), re-checks the start time
    through it, then signals the handle. Where pidfds are unavailable (old kernel or Python) it checks and calls
    `os.kill`. Raises `ProcessLookupError` when the process is gone or is a different one."""
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        raise
    except (AttributeError, OSError):
        if not pid_alive(pid, start_ticks):
            raise ProcessLookupError(pid)
        os.kill(pid, signum)
        return
    try:
        if not pid_alive(pid, start_ticks):
            raise ProcessLookupError(pid)
        signal.pidfd_send_signal(fd, signum)
    finally:
        os.close(fd)


# [@ANCHOR: agent_procs:job_state]
def job_state(rec):
    """`"exited"` when the wrapper recorded an exit code, `"running"` when the recorded pid is alive with the recorded start
    time, else `"lost"` (the job is gone without an exit code: it was killed with SIGKILL, or the machine restarted)."""
    if rec.exit_code is not None:
        return "exited"
    return "running" if pid_alive(rec.pid, rec.start_ticks) else "lost"


# [@ANCHOR: agent_procs:forward_signal]
def forward_signal(holder, signum, _frame):
    """Wrapper signal handler: pass `signum` on to the job's command (`holder[0]`, a `Popen`) so that a signal sent to the
    wrapper alone still reaches the command; the wrapper itself keeps running until the command ends, so the exit code is
    always recorded."""
    if holder and holder[0] is not None and holder[0].poll() is None:
        try:
            holder[0].send_signal(signum)
        except OSError:
            pass


# [@ANCHOR: agent_procs:parse_size]
def parse_size(text):
    """Bytes in a size such as `6G`, `512M`, `100K` or `123456` (binary units, case-insensitive); `ProcError` otherwise."""
    match = re.fullmatch(r"(\d+)([kmgt]?)b?", str(text).strip().lower())
    if not match:
        raise ProcError(f"bad size {text!r}: use a number with an optional K, M, G or T (for example 6G), or `none`")
    unit = match.group(2)
    return int(match.group(1)) * (1024 ** ("kmgt".index(unit) + 1) if unit else 1)


@functools.lru_cache(maxsize=None)
def user_scope_works():
    """True when `systemd-run --user --scope` can start a command here (it needs the user manager and a cgroup parent).
    AGENT_PROCS_NO_SYSTEMD_SCOPE=1 forces False (the `ulimit -v` fallback): tests that deliberately exceed a cap use it so
    the kernel OOM killer never fires in a user scope (GNOME shows a desktop toast for each)."""
    if os.environ.get("AGENT_PROCS_NO_SYSTEMD_SCOPE"):
        return False
    try:
        return subprocess.run(["systemd-run", "--user", "--scope", "-q", "true"], capture_output=True,
                              timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# [@ANCHOR: agent_procs:limit_argv]
def limit_argv(argv, mem_max, nice, have_systemd=None):
    """The command line the wrapper runs for `argv`: `nice -n NICE` first when `nice` is non-zero; the memory cap `mem_max`
    (text such as `6G`, or `None` for no cap) as `systemd-run --user --scope -p MemoryMax=... -p MemorySwapMax=0`, or, when
    that is unavailable (`have_systemd` False; default: `user_scope_works()`), `ulimit -v` in a `sh -c` (address space, a
    cruder cap)."""
    argv = list(argv)
    if mem_max:
        if have_systemd is None:
            have_systemd = user_scope_works()
        if have_systemd:
            argv = ["systemd-run", "--user", "--scope", "-q", "-p", f"MemoryMax={mem_max}", "-p", "MemorySwapMax=0"] + argv
        else:
            argv = ["sh", "-c", f'ulimit -v {parse_size(mem_max) // 1024} && exec "$@"', "sh"] + argv
    if nice:
        argv = ["nice", "-n", str(nice)] + argv
    return argv


# [@ANCHOR: agent_procs:stop_group]
def stop_group(pgid, sid, grace, sleep=time.sleep, clock=time.monotonic):
    """Wrapper-side stop of its own job: SIGTERM to the process group `pgid` (the wrapper survives it: its handler forwards
    it), wait up to `grace` seconds for every other member to end, then SIGKILL each remaining member one by one (never the
    group, which holds the wrapper; each signal checks the start time)."""
    me = os.getpid()
    others = lambda: [i for i in group_members(snapshot(), pgid, sid) if i.pid != me]  # noqa: E731
    os.killpg(pgid, signal.SIGTERM)
    deadline = clock() + grace
    while others() and clock() < deadline:
        sleep(0.1)
    for info in others():
        try:
            signal_same_process(info.pid, info.start_ticks, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _die_with_parent():
    """`preexec_fn` of the job's command: SIGTERM when the wrapper (its parent) dies, so a lost wrapper never leaves it."""
    import ctypes
    ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, int(signal.SIGTERM))  # PR_SET_PDEATHSIG
    for signum in ALLOWED_SIGNALS.values():  # the wrapper's forwarding handlers must not run in the command (holder is empty)
        signal.signal(signum, signal.SIG_DFL)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, set(ALLOWED_SIGNALS.values()))  # held by the wrapper until Popen returned


# [@ANCHOR: agent_procs:wrapper_main]
def wrapper_main(name, argv, directory=None, timeout=0, kill_grace=KILL_GRACE, mem_max=None, nice=0):
    """The tiny wrapper that is the leader of a named job's session (`agent_procs.py _wrap NAME [--timeout S] -- COMMAND...`).

    Waits for the line `go` on standard input (so the record exists before the command runs), runs `argv` with standard input
    from /dev/null and output inherited (the log), waits for it, and writes `exit_code` (128+N for death by signal N, 127 when
    the program was not found) and `ended_at` into the job's record when the record still names this process. Returns that
    exit code. SIGTERM, SIGINT, SIGHUP, SIGQUIT, SIGUSR1 and SIGUSR2 (`ALLOWED_SIGNALS`) are forwarded to the command and do not
    stop the wrapper. After `timeout` seconds (0: never) the whole process group is stopped with `stop_group` (SIGTERM, then
    SIGKILL after `kill_grace`), the exit code is 124 and the record gets `exit_reason` `timeout`. The command asks the kernel
    for SIGTERM when the wrapper dies (PR_SET_PDEATHSIG). `mem_max` and `nice` are applied with `limit_argv` here, so the
    wrapper's own command line never names systemd-run."""
    if sys.stdin.readline().strip() != "go":
        return 126
    holder = [None]
    for signum in ALLOWED_SIGNALS.values():
        signal.signal(signum, functools.partial(forward_signal, holder))
    reason = None
    try:
        held = set(ALLOWED_SIGNALS.values())
        signal.pthread_sigmask(signal.SIG_BLOCK, held)  # a signal during the start is delivered after it, not lost
        try:  # (the block covers `limit_argv` too: its probe can be slow under load, and a handler run before the command exists has nothing to forward to)
            full = limit_argv(argv, mem_max, nice) if shutil.which(argv[0]) else list(argv)  # a missing program stays 127
            holder[0] = subprocess.Popen(full, stdin=subprocess.DEVNULL, preexec_fn=_die_with_parent)
        finally:
            signal.pthread_sigmask(signal.SIG_UNBLOCK, held)
        try:
            code = holder[0].wait(timeout=timeout or None)
        except subprocess.TimeoutExpired:
            reason = "timeout"
            print(f"agent_procs: job {name} reached its timeout of {timeout:g}s: stopping its process group",
                  file=sys.stderr, flush=True)
            stop_group(os.getpgrp(), os.getsid(0), kill_grace)
            holder[0].wait()
            code = EX_TIMEOUT_JOB
        else:
            code = 128 - code if code < 0 else code
    except OSError as error:
        print(f"agent_procs: cannot run {argv[0]}: {error}", file=sys.stderr, flush=True)
        code = 127
    rec = read_record(name, directory)
    if rec is not None and rec.pid == os.getpid():
        rec.exit_code = code
        rec.exit_reason = reason
        rec.ended_at = time.time()
        write_record(rec, directory)
    return code


# [@ANCHOR: agent_procs:start_job]
def start_job(name, argv, tag="", cwd=None, directory=None, timeout=DEFAULT_TIMEOUT, mem_max=DEFAULT_MEM_MAX, nice=0,
              kill_grace=KILL_GRACE):
    """Start `argv` as the named job `name` in its own session and process group; returns its `JobRecord`.

    Output (standard output and error) goes to `<jobs dir>/<name>.log`, truncated. The record names the wrapper's pid,
    process group, session and start time. Holds an exclusive lock on `<name>.lock` while checking and starting, so two
    starts of one name cannot both succeed. Raises `ProcError` for a bad name, an empty command, a wrapper whose /proc start time cannot be read, a `cwd` that is not a directory, or a job of that name that
    is still running (stop it, or wait for it, first). `timeout` seconds (0: none) and `mem_max` (`6G`; `None`: none) are
    recorded and enforced (see `wrapper_main`, `limit_argv`); `nice` is the niceness."""
    directory = directory or jobs_dir()
    check_job_name(name)
    if not argv:
        raise ProcError("run needs a command after --")
    if cwd is not None and not os.path.isdir(cwd):
        raise ProcError(f"--cwd {cwd} is not a directory")
    with open(os.path.join(directory, name + ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        old = read_record(name, directory)
        if old is not None and job_state(old) == "running":
            raise ProcError(f"job {name} is already running (pid {old.pid}); wait for it or `stop {name}`", EX_FAILED)
        log = os.path.join(directory, name + ".log")
        with open(log, "w") as log_handle:
            env = dict(os.environ, AGENT_PROCS_JOB=name)
            child = subprocess.Popen(
                [sys.executable, os.path.abspath(__file__), "_wrap", name, "--timeout", str(timeout), "--kill-grace",
                 str(kill_grace), "--mem-max", mem_max or "none", "--nice", str(nice), "--"] + list(argv), stdin=subprocess.PIPE,
                stdout=log_handle, stderr=subprocess.STDOUT, cwd=cwd, start_new_session=True, env=env,
                close_fds=True)
        info = read_proc(child.pid)
        if info is None:  # a record with start_ticks 0 would read as "lost" while the job runs
            child.stdin.close()  # the wrapper then ends without running the command
            child.wait()
            raise ProcError(f"cannot read the start time of the wrapper for job {name} (pid {child.pid}); not started", EX_FAILED)
        rec = JobRecord(name, tag, child.pid, child.pid, child.pid, info.start_ticks,
                        os.path.realpath(cwd or os.getcwd()), list(argv), log, time.time(), user_name(os.getuid()),
                        timeout=timeout, mem_max=mem_max)
        write_record(rec, directory)
        child.stdin.write(b"go\n")
        child.stdin.close()
    return rec


# [@ANCHOR: agent_procs:wait_pid]
def wait_pid(pid, start_ticks=None, timeout=DEFAULT_WAIT_TIMEOUT, interval=POLL_INTERVAL, sleep=time.sleep,
             clock=time.monotonic, table=None):
    """Wait until process `pid` ends. Returns a `WaitResult`: code 0 when it ended (or was already gone, or the pid was reused:
    it now belongs to a process that started LATER than the one named), 75 on timeout, 3 when `pid` is the caller's own
    ancestry (watching the watcher: it could never end before this command). Raises `ProcError` code 2 when `start_ticks`
    cannot be read or cannot belong to this pid (`parse_start_time`, `check_start_time`): a mismatch is never "done".

    `start_ticks` is the start time printed by `find` (`ticks=N` or `start=...`, see `parse_start_time`); when `None` it is read at the first look. Never matches on a
    command line. The exit status of a process that is not our child is not knowable, so a plain pid is "done", not "ok";
    wait for a named job (`wait_job`) or a marker file (`wait_file`) when the outcome matters."""
    spec = None if start_ticks is None else parse_start_time(start_ticks)  # a bad format is an error, never "gone"
    if pid in ancestry(snapshot() if table is None else table, os.getpid()):
        return WaitResult(EX_REFUSED, f"refusing: pid {pid} is this command's own process or an ancestor (watching the watcher)")
    info = read_proc(pid)
    if spec is not None and check_start_time(info, spec) == "reused":
        return WaitResult(EX_OK, f"pid {pid} was reused: it now belongs to a process that started later (ticks={info.start_ticks}) "
                                 f"than the one you named: the original is gone")
    if info is None:
        return WaitResult(EX_OK, f"pid {pid} is already gone")
    start_ticks = info.start_ticks
    deadline = clock() + timeout
    while pid_alive(pid, start_ticks):
        if clock() >= deadline:
            return WaitResult(EX_TIMEOUT, f"timeout after {timeout:g}s: pid {pid} is still running")
        sleep(interval)
    return WaitResult(EX_OK, f"pid {pid} ended")


def short_command(info, width=100):
    """The command line of `info`, cut to `width` characters with `...` when longer."""
    text = " ".join(info.argv)
    return text if len(text) <= width else text[:width - 3] + "..."


# [@ANCHOR: agent_procs:wait_matching]
def wait_matching(found, wait_all=False, allow_missing=False, timeout=DEFAULT_WAIT_TIMEOUT, interval=POLL_INTERVAL,
                  sleep=time.sleep, clock=time.monotonic):
    """Wait for the processes a selector matched (`found` is `find_procs`' result, taken once, at the start).

    Exactly one match: wait until it ends (judged by pid AND start time). More than one match is an ERROR (`ProcError` code
    2 listing every match with pid, start time, user and a shortened command, and the exact `--pid P --start-time T` way to
    name one) unless `wait_all`, which waits until every one of them has ended. No match is an ERROR (code 2: did it
    already finish, or is the selector wrong?) unless `allow_missing`, which is "done". A match that ends later is "done";
    a pattern is never turned into a signal. Returns a `WaitResult` (0 done, 75 timeout)."""
    if not found:
        if allow_missing:
            return WaitResult(EX_OK, "nothing matches (--allow-missing): nothing to wait for")
        raise ProcError("nothing matches: did it already finish, or is the selector wrong? (--allow-missing treats that as "
                        "done; `find` with the same selector shows what it sees)")
    if len(found) > 1 and not wait_all:
        lines = [f"  pid={info.pid} ticks={info.start_ticks} user={info.user} cmd={short_command(info)}" for info, _ in found]
        raise ProcError(f"ambiguous: {len(found)} matches\n" + "\n".join(lines) +
                        "\nname one with `wait --pid P --start-time T` (pid and ticks as above), or add --all to wait until "
                        "every one of them has ended")
    deadline = clock() + timeout
    pending = [(info.pid, info.start_ticks) for info, _ in found]
    while True:
        pending = [(pid, ticks) for pid, ticks in pending if pid_alive(pid, ticks)]
        if not pending:
            return WaitResult(EX_OK, f"{len(found)} matching process(es) ended")
        if clock() >= deadline:
            return WaitResult(EX_TIMEOUT, f"timeout after {timeout:g}s: still running: " + ", ".join(str(p) for p, _ in pending))
        sleep(interval)


# [@ANCHOR: agent_procs:wait_job]
def wait_job(name, timeout=DEFAULT_WAIT_TIMEOUT, interval=POLL_INTERVAL, sleep=time.sleep, clock=time.monotonic,
             directory=None):
    """Wait for the named job to end. Code 0 when its exit code is 0, 1 for any other exit code or for a job that vanished
    without recording one (`lost`), 75 on timeout. Raises `ProcError` (usage) when there is no such job."""
    if read_record(name, directory) is None:
        raise ProcError(f"no job named {name}")
    deadline = clock() + timeout
    while True:
        rec = read_record(name, directory)
        if rec is None:
            return WaitResult(EX_FAILED, f"the record of job {name} disappeared while waiting (removed, or unreadable)")
        state = job_state(rec)
        if state == "lost":  # the wrapper may have written its exit code and ended between our read and the liveness check
            rec = read_record(name, directory) or rec
            state = job_state(rec)
        if state == "exited":
            return WaitResult(EX_OK if rec.exit_code == 0 else EX_FAILED, f"job {name} exited with code {rec.exit_code}")
        if state == "lost":
            return WaitResult(EX_FAILED, f"job {name} is gone without an exit code (killed?); log: {rec.log}")
        if clock() >= deadline:
            return WaitResult(EX_TIMEOUT, f"timeout after {timeout:g}s: job {name} is still running")
        sleep(interval)


# [@ANCHOR: agent_procs:wait_file]
def wait_file(path, contains=None, fail_contains=None, while_pid=None, timeout=DEFAULT_WAIT_TIMEOUT,
              interval=POLL_INTERVAL, sleep=time.sleep, clock=time.monotonic):
    """Wait for `path` to exist and, when `contains` (a regular expression, `^` and `$` match at each line) is given, to contain a match.

    Code 0 when the condition holds; 1 when `fail_contains` matches first, or when `while_pid` (a pid, or `(pid, ticks)`)
    ends before the condition holds (the producer died without writing its marker); 75 on timeout. The file is read whole
    each poll, so keep it a log or marker, not a large data file."""
    try:
        want = re.compile(contains, re.MULTILINE) if contains else None
        bad = re.compile(fail_contains, re.MULTILINE) if fail_contains else None
    except re.error as error:
        raise ProcError(f"bad --contains or --fail-contains regular expression: {error}")
    watch = None
    if while_pid is not None:
        wpid, wticks = while_pid if isinstance(while_pid, tuple) else (while_pid, None)
        info = read_proc(wpid)
        watch = (wpid, wticks if wticks is not None else (info.start_ticks if info else None))
    deadline = clock() + timeout
    while True:
        text = None
        if os.path.exists(path):
            try:
                with open(path, errors="replace") as handle:
                    text = handle.read()
            except OSError:
                text = None
        if text is not None:
            if bad and bad.search(text):
                return WaitResult(EX_FAILED, f"{path} contains the failure pattern {fail_contains!r}")
            if want is None or want.search(text):
                return WaitResult(EX_OK, f"{path} is ready")
        if watch and (watch[1] is None or not pid_alive(*watch)):
            return WaitResult(EX_FAILED, f"pid {watch[0]} ended before {path} was ready")
        if clock() >= deadline:
            return WaitResult(EX_TIMEOUT, f"timeout after {timeout:g}s waiting for {path}")
        sleep(interval)


# [@ANCHOR: agent_procs:group_members]
def group_members(table, pgid, sid):
    """The live (non-zombie) processes of process group `pgid` in session `sid`, from `table`."""
    return [info for info in table.values() if info.pgid == pgid and info.sid == sid and info.state not in ("Z", "X")]


# [@ANCHOR: agent_procs:stop_job]
def stop_job(name, grace=DEFAULT_GRACE, force=False, owner_checked=False, out=sys.stdout, directory=None,
             sleep=time.sleep, clock=time.monotonic):
    """Stop the NAMED job `name`: SIGTERM to its process group, then (only with `force`) SIGKILL to that group.

    Verifies before every signal: the job's record exists and is the caller's; the group members found in /proc have the
    recorded process group AND session; the process that now has the recorded pid, if any, has the recorded start time (a
    reused pid that leads a session of its own has the same group and session numbers, but not the start time, and its group
    is then left alone); the group is not the caller's own. Prints the ps
    line of every member first. Refuses (raises `ProcError` code 3) when a member is protected (`protected_reason`) unless
    `owner_checked` (the `--i-am-the-owner-and-checked` flag). Waits up to `grace` seconds after SIGTERM. Returns 0 when
    the job is gone, 1 when it is still running (SIGTERM was not enough and `force` was not given, or even SIGKILL failed).
    Raises `ProcError` (usage) for an unknown job."""
    rec = read_record(name, directory)
    if rec is None:
        raise ProcError(f"no job named {name}")
    table = snapshot()
    members = group_members(table, rec.pgid, rec.sid)
    if not members:
        print(f"job {name} is not running (pid {rec.pid} gone); nothing to stop", file=out)
        return EX_OK
    me = os.getpid()
    family = ancestry(table, me)
    if rec.pgid in {table[p].pgid for p in family if p in table} | {os.getpgrp()}:
        raise ProcError(f"refusing: job {name}'s process group contains the caller's own shell", EX_REFUSED)
    leader = table.get(rec.pid)
    if leader is not None and leader.start_ticks != rec.start_ticks:
        print(f"job {name} is not running (pid {rec.pid} now belongs to a different process); nothing to stop", file=out)
        return EX_OK
    worktree, tmpdir = caller_worktree(), os.environ.get("TMPDIR")
    for info in members:
        print(ps_line(info), file=out)
        if info.uid != os.getuid():
            raise ProcError(f"refusing: pid {info.pid} is owned by {info.user}", EX_REFUSED)
        reason = None if owner_checked else protected_reason(info, os.getuid(), worktree, tmpdir, family)
        if reason:
            raise ProcError(f"REFUSED: pid {info.pid} in job {name}: {reason}", EX_REFUSED)
    os.killpg(rec.pgid, signal.SIGTERM)
    print(f"sent SIGTERM to process group {rec.pgid} of job {name}", file=out, flush=True)
    stopped = "term"
    deadline = clock() + grace
    while group_members(snapshot(), rec.pgid, rec.sid) and clock() < deadline:
        sleep(0.1)
    left = group_members(snapshot(), rec.pgid, rec.sid)
    if left and not force:
        print(f"job {name} is still running after {grace:g}s: {len(left)} process(es); `stop {name} --force` sends SIGKILL "
              f"to this verified group", file=out)
        return EX_FAILED
    if left:
        left = group_members(snapshot(), rec.pgid, rec.sid)
        if left:
            os.killpg(rec.pgid, signal.SIGKILL)
            stopped = "kill"
            print(f"sent SIGKILL to process group {rec.pgid} of job {name}", file=out, flush=True)
            deadline = clock() + 5
            while group_members(snapshot(), rec.pgid, rec.sid) and clock() < deadline:
                sleep(0.1)
    current = read_record(name, directory)
    if current is not None and current.pid == rec.pid:
        current.stopped = stopped
        current.ended_at = current.ended_at or time.time()
        write_record(current, directory)
    if group_members(snapshot(), rec.pgid, rec.sid):
        print(f"job {name} did not end", file=out)
        return EX_FAILED
    print(f"job {name} stopped", file=out)
    return EX_OK


# [@ANCHOR: agent_procs:authorize_kill]
def authorize_kill(info, start_ticks, cwd_under, directory=None):
    """How a single pid is allowed to be signalled, or raises `ProcError` (code 3). Returns the reason as text:
    `"recorded job"` when a job record names this pid and start time; `"start time confirmed"` when `start_ticks` equals the
    process's; `"cwd under <path>"` when `cwd_under` holds. A `start_ticks` that differs means the pid was reused: refused."""
    if start_ticks is not None:
        if check_start_time(info, parse_start_time(start_ticks)) == "reused":
            raise ProcError(f"refusing: pid {info.pid} was reused (start time {info.start_ticks}, you named {start_ticks})",
                            EX_REFUSED)
    for rec in list_records(directory):
        if rec.pid == info.pid and rec.start_ticks == info.start_ticks:
            return "recorded job"
    if start_ticks is not None:
        return "start time confirmed"
    if cwd_under and is_under(info.cwd, cwd_under):
        return f"cwd under {cwd_under}"
    raise ProcError(f"refusing: pid {info.pid} is neither a recorded job, nor confirmed by --start-time {info.start_ticks} "
                    f"(as printed by `find`), nor under --cwd-under", EX_REFUSED)


# [@ANCHOR: agent_procs:kill_pid]
def kill_pid(pid, start_ticks=None, cwd_under=None, grace=DEFAULT_GRACE, force=False, owner_checked=False,
             sig="TERM", out=sys.stdout, directory=None, sleep=time.sleep, clock=time.monotonic):
    """Signal ONE exact pid, after verification. Never accepts a list and never a pattern.

    Order: refuse pid <= 1 and the caller's own ancestry; refuse a process of another user; `authorize_kill`; refuse a
    protected process (`protected_reason`; `owner_checked` lifts this for a recorded job only); print the ps line; send
    `sig` (one of TERM INT HUP QUIT USR1 USR2; default TERM) to that pid only; wait up to `grace`; with `force` and the same
    process (same start time) still alive, SIGKILL it. Returns 0 when the process is gone (or already was), 1 when it is
    still alive. Raises `ProcError` code 3 for every refusal, 2 for a bad signal name."""
    if sig not in ALLOWED_SIGNALS:
        raise ProcError(f"signal must be one of {', '.join(ALLOWED_SIGNALS)} (SIGKILL only with --force)")
    table = snapshot()
    family = ancestry(table, os.getpid())
    if pid <= 1 or pid in family:
        raise ProcError(f"refusing: pid {pid} is pid 1 or the caller's own ancestry", EX_REFUSED)
    info = table.get(pid) or read_proc(pid)
    if info is None:
        print(f"pid {pid} does not exist (already gone)", file=out)
        return EX_OK
    if info.uid != os.getuid():
        raise ProcError(f"REFUSED: pid {pid} is owned by {info.user}", EX_REFUSED)
    how = authorize_kill(info, start_ticks, cwd_under, directory)
    reason = protected_reason(info, os.getuid(), caller_worktree(), os.environ.get("TMPDIR"), family)
    if reason and not (owner_checked and how == "recorded job"):
        raise ProcError(f"REFUSED: pid {pid}: {reason}", EX_REFUSED)
    print(ps_line(info), file=out)
    try:
        signal_same_process(pid, info.start_ticks, ALLOWED_SIGNALS[sig])
    except ProcessLookupError:
        print(f"pid {pid} is gone or was reused before the signal (nothing sent)", file=out)
        return EX_OK
    print(f"sent SIG{sig} to pid {pid} ({how})", file=out, flush=True)
    deadline = clock() + grace
    while pid_alive(pid, info.start_ticks) and clock() < deadline:
        sleep(0.1)
    if pid_alive(pid, info.start_ticks):
        if not force:
            print(f"pid {pid} is still running after {grace:g}s; add --force to send SIGKILL to this pid", file=out)
            return EX_FAILED
        try:
            signal_same_process(pid, info.start_ticks, signal.SIGKILL)
        except ProcessLookupError:
            return EX_OK
        print(f"sent SIGKILL to pid {pid} (same start time)", file=out, flush=True)
        deadline = clock() + 5
        while pid_alive(pid, info.start_ticks) and clock() < deadline:
            sleep(0.1)
    return EX_FAILED if pid_alive(pid, info.start_ticks) else EX_OK


# [@ANCHOR: agent_procs:cmd_find]
def cmd_find(args, out=sys.stdout):
    """`find` subcommand. Prints one `ps_line` per match (`PROTECTED:` appended when a signal would be refused) or the
    JSON list with `--json`. Returns 0 when something matched, 1 when nothing did (like pgrep). It lists no pid list for
    `kill`: pass pids one at a time to `kill-pid`."""
    pids = read_pid_file(args.pid_file) if args.pid_file else None
    found = find_procs(snapshot(), user=args.user, any_user=args.any_user, cwd_under=args.cwd_under,
                       contains=args.cmd_contains, regex=args.cmd_regex, pids=pids, worktree=caller_worktree(),
                       tmpdir=os.environ.get("TMPDIR"))
    if args.json:
        json.dump([dict(dataclasses.asdict(info), protected=why) for info, why in found], out, indent=1)
        out.write("\n")
    elif not args.quiet:
        for info, why in found:
            print(ps_line(info) + (f"  PROTECTED: {why}" if why else ""), file=out)
    return EX_OK if found else EX_FAILED


# [@ANCHOR: agent_procs:cmd_wait]
def cmd_wait(args, out=sys.stdout):
    """`wait` subcommand: exactly one of `--pid`, `--job`, `--file`. Prints the result message; returns its code."""
    selector = bool(args.cwd_under or args.cmd_contains or args.cmd_regex)
    chosen = [bool(args.pid is not None), bool(args.job), bool(args.file), selector]
    if sum(chosen) != 1:
        raise ProcError("wait needs exactly one of --pid, --job, --file, or a selector (--cwd-under, --cmd-contains, --cmd-regex)")
    if (args.all or args.allow_missing) and not selector:
        raise ProcError("--all and --allow-missing belong to a selector wait (--cwd-under, --cmd-contains, --cmd-regex)")
    if selector:
        found = find_procs(snapshot(), user=args.user, any_user=args.any_user, cwd_under=args.cwd_under,
                           contains=args.cmd_contains, regex=args.cmd_regex, worktree=caller_worktree(),
                           tmpdir=os.environ.get("TMPDIR"))
        result = wait_matching(found, args.all, args.allow_missing, args.timeout, args.interval)
    elif args.pid is not None:
        result = wait_pid(args.pid, args.start_time, args.timeout, args.interval)
    elif args.job:
        result = wait_job(args.job, args.timeout, args.interval)
    else:
        result = wait_file(args.file, args.contains, args.fail_contains, args.while_pid, args.timeout, args.interval)
    print(result.message, file=out)
    return result.code


# [@ANCHOR: agent_procs:cmd_run]
def cmd_run(args, out=sys.stdout):
    """`run` subcommand: starts the named job and prints its name, pid, process group, log path and how to wait for it."""
    timeout = DEFAULT_TIMEOUT if args.timeout is None else int(args.timeout)
    mem = DEFAULT_MEM_MAX if args.mem_max is None else args.mem_max
    mem = None if mem.lower() == "none" else mem
    if mem:
        parse_size(mem)
    rec = start_job(args.name, args.command, args.tag or "", args.cwd, timeout=timeout, mem_max=mem, nice=args.nice or 0)
    print(f"job {rec.name} started: pid={rec.pid} pgid={rec.pgid} log={rec.log}", file=out)
    if not timeout:
        print("  WARNING: NO TIMEOUT (--timeout 0): this job runs until it ends or you stop it", file=out)
    if not mem:
        print("  WARNING: NO MEMORY CAP (--mem-max none): this job can use all the memory", file=out)
    elif not user_scope_works():
        print(f"  note: systemd-run --user is unavailable, memory cap {mem} is `ulimit -v` (address space) instead", file=out)
    print(f"  wait: agent_procs.py wait --job {rec.name}    stop: agent_procs.py stop {rec.name}", file=out)
    return EX_OK


# [@ANCHOR: agent_procs:cmd_jobs]
def cmd_jobs(args, out=sys.stdout):
    """`jobs` subcommand: lists every record with its state (`running`, `exited`, `lost`), or the JSON with `--json`."""
    rows = [(rec, job_state(rec)) for rec in list_records()]
    if args.json:
        json.dump([dict(dataclasses.asdict(rec), state=state) for rec, state in rows], out, indent=1)
        out.write("\n")
        return EX_OK
    for rec, state in rows:
        code = f" code={rec.exit_code}" if rec.exit_code is not None else ""
        print(f"{rec.name} {state}{code} pid={rec.pid} started={format_start(rec.start_ticks) if state == 'running' else '-'} "
              f"timeout={f'{rec.timeout}s' if rec.timeout else 'none'} mem={rec.mem_max or 'none'}"
              f"{' reason=' + rec.exit_reason if rec.exit_reason else ''} tag={rec.tag or '-'} cwd={rec.cwd} cmd={' '.join(rec.argv)}", file=out)
    return EX_OK


# [@ANCHOR: agent_procs:orphan_reason]
def orphan_reason(info, table, min_age, now, boot):
    """Why `info` is an orphan of an agent worktree, or `None`. It must be older than `min_age` seconds, have its cwd under
    `AGENT_WORK_ROOT`, and either that cwd is deleted (`/proc/PID/cwd` reads `<path> (deleted)`), or its parent (in `table`)
    is a systemd user manager (comm `systemd`, not pid 1, same user): the agent that started it is gone."""
    cwd = info.cwd or ""
    deleted = cwd.endswith(" (deleted)")
    if not is_under(cwd[:-len(" (deleted)")] if deleted else cwd, AGENT_WORK_ROOT):
        return None
    if now - (boot + info.start_ticks / CLK_TCK) < min_age:
        return None
    if deleted:
        return "its working directory was deleted"
    parent = table.get(info.ppid)
    if parent is not None and parent.pid != 1 and parent.uid == info.uid and parent.comm == "systemd":
        return "its parent is the systemd user manager (the agent that started it is gone)"
    return None


# [@ANCHOR: agent_procs:find_orphans]
def find_orphans(table, min_age=DEFAULT_MIN_AGE, directory=None):
    """`[(ProcInfo, reason)]` of the caller's own orphaned agent processes (`orphan_reason`). Never listed: protected
    processes (`protected_reason`), the caller's family, and any process in the session of a still-running recorded job
    (`run` jobs are children of the user manager too)."""
    now, boot, me = time.time(), boot_time(), os.getuid()
    family = ancestry(table, os.getpid())
    job_sids = {rec.sid for rec in list_records(directory) if job_state(rec) == "running"}
    worktree, tmpdir = caller_worktree(), os.environ.get("TMPDIR")
    found = []
    for info in sorted(table.values(), key=lambda i: i.pid):
        if info.uid != me or info.sid in job_sids or info.state in ("Z", "X"):
            continue
        why = orphan_reason(info, table, min_age, now, boot)
        if why and not protected_reason(info, me, worktree, tmpdir, family):
            found.append((info, why))
    return found


# [@ANCHOR: agent_procs:cmd_reap]
def cmd_reap(args, out=sys.stdout):
    """`reap` subcommand: lists `find_orphans`; with `--apply` stops each through `kill_pid` (SIGTERM, exact start time,
    the hard kill only with `--force`). Returns 0, or 1 when a process was still running after the grace."""
    found = find_orphans(snapshot(), args.min_age)
    code = EX_OK
    for info, why in found:
        print(f"{ps_line(info)}  ORPHAN: {why}", file=out)
        if args.apply:
            try:
                if kill_pid(info.pid, info.start_ticks, force=args.force, out=out):
                    code = EX_FAILED
            except ProcError as error:
                print(f"  skipped: {error}", file=out)
    if not found:
        print("no orphans", file=out)
    elif not args.apply:
        print(f"{len(found)} orphan(s); dry run, add --apply to stop them", file=out)
    return code


# [@ANCHOR: agent_procs:cmd_stop]
def cmd_stop(args, out=sys.stdout):
    """`stop` subcommand: `stop_job` with the command-line options."""
    return stop_job(args.name, args.grace, args.force, args.i_am_the_owner_and_checked, out)


# [@ANCHOR: agent_procs:cmd_kill_pid]
def cmd_kill_pid(args, out=sys.stdout):
    """`kill-pid` subcommand: `kill_pid` for the one pid on the command line."""
    return kill_pid(args.pid, args.start_time, args.cwd_under, args.grace, args.force, args.i_am_the_owner_and_checked,
                    args.signal, out)


# [@ANCHOR: agent_procs:build_parser]
def build_parser():
    """The argparse parser of the command line, without the command after `--` (`parse_args` adds it)."""
    parser = argparse.ArgumentParser(prog="agent_procs.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    find = sub.add_parser("find", help="list processes (never this one, its ancestors or descendants)")
    find.add_argument("--cwd-under", metavar="PATH")
    find.add_argument("--cmd-contains", metavar="TEXT", help="literal substring of the full command line")
    find.add_argument("--cmd-regex", metavar="RE")
    find.add_argument("--user", metavar="NAME", help="default: your own user")
    find.add_argument("--any-user", action="store_true", help="also list other users' processes (shown as protected)")
    find.add_argument("--pid-file", metavar="FILE", help="lines PID or PID:START_TICKS")
    find.add_argument("--json", action="store_true")
    find.add_argument("--quiet", action="store_true", help="print nothing; the exit code says whether anything matched")
    wait = sub.add_parser("wait", help="wait for an exact pid, a named job, or a file")
    wait.add_argument("--pid", type=int)
    wait.add_argument("--start-time", metavar="TICKS", help="the ticks= value printed by find (or its start= timestamp)")
    wait.add_argument("--job", metavar="NAME")
    wait.add_argument("--cwd-under", metavar="PATH", help="selector: processes whose cwd is under PATH (exactly one must match)")
    wait.add_argument("--cmd-contains", metavar="TEXT", help="selector: literal substring of the full command line")
    wait.add_argument("--cmd-regex", metavar="RE", help="selector: regular expression on the full command line")
    wait.add_argument("--user", metavar="NAME", help="selector: default your own user")
    wait.add_argument("--any-user", action="store_true", help="selector: also other users' processes")
    wait.add_argument("--all", action="store_true", help="selector matching several: wait until every match has ended")
    wait.add_argument("--allow-missing", action="store_true", help="selector matching nothing is done (default: an error)")
    wait.add_argument("--file", metavar="PATH")
    wait.add_argument("--contains", metavar="RE", help="with --file: the file must contain a match")
    wait.add_argument("--fail-contains", metavar="RE", help="with --file: a match means failure (exit 1)")
    wait.add_argument("--while-pid", type=int, metavar="PID", help="with --file: exit 1 if this pid ends first")
    wait.add_argument("--timeout", type=float, default=DEFAULT_WAIT_TIMEOUT, metavar="SECONDS")
    wait.add_argument("--interval", type=float, default=POLL_INTERVAL, metavar="SECONDS")
    run = sub.add_parser("run", help="start a named job in its own session")
    run.add_argument("name")
    run.add_argument("--tag", metavar="AGENT", help="the owning agent, recorded in the job record")
    run.add_argument("--cwd", metavar="DIR")
    run.add_argument("--timeout", type=float, metavar="SECONDS",
                     help=f"stop the whole process group after this long (default {DEFAULT_TIMEOUT}; 0: no timeout)")
    run.add_argument("--mem-max", metavar="SIZE", help=f"memory cap, e.g. 4G (default {DEFAULT_MEM_MAX}; none: no cap)")
    run.add_argument("--nice", type=int, metavar="N", help="run under nice -n N")
    reap = sub.add_parser("reap", help="list (and with --apply stop) your orphaned agent-worktree processes")
    reap.add_argument("--min-age", type=float, default=DEFAULT_MIN_AGE, metavar="SECONDS")
    reap.add_argument("--apply", action="store_true", help="stop them (default: dry run)")
    reap.add_argument("--force", action="store_true", help="with --apply: hard-kill one that survives SIGTERM")
    jobs = sub.add_parser("jobs", help="list job records")
    jobs.add_argument("--json", action="store_true")
    stop = sub.add_parser("stop", help="stop a named job (its process group)")
    stop.add_argument("name")
    stop.add_argument("--grace", type=float, default=DEFAULT_GRACE, metavar="SECONDS")
    stop.add_argument("--force", action="store_true", help="SIGKILL the verified group if SIGTERM was not enough")
    stop.add_argument(OWNER_OVERRIDE_FLAG, action="store_true", dest="i_am_the_owner_and_checked",
                      help="override the protected-process refusal for your own job")
    kill = sub.add_parser("kill-pid", help="signal one exact, verified pid")
    kill.add_argument("pid", type=int)
    kill.add_argument("--start-time", metavar="TICKS", help="the ticks= value printed by find (or its start= timestamp)")
    kill.add_argument("--cwd-under", metavar="PATH")
    kill.add_argument("--signal", default="TERM", metavar="NAME")
    kill.add_argument("--grace", type=float, default=DEFAULT_GRACE, metavar="SECONDS")
    kill.add_argument("--force", action="store_true", help="SIGKILL this pid if it survives the grace period")
    kill.add_argument(OWNER_OVERRIDE_FLAG, action="store_true", dest="i_am_the_owner_and_checked",
                      help="override the protected-process refusal for your own recorded job")
    wrap = sub.add_parser("_wrap")
    wrap.add_argument("name")
    wrap.add_argument("--timeout", type=float, default=0)
    wrap.add_argument("--kill-grace", type=float, default=KILL_GRACE)
    wrap.add_argument("--mem-max", default="none")
    wrap.add_argument("--nice", type=int, default=0)
    return parser


# [@ANCHOR: agent_procs:parse_args]
def parse_args(argv=None):
    """Parse the command line. For `run` and the internal `_wrap`, everything after the first `--` is the command and is
    returned as `args.command` (a list, possibly empty); the rest goes through `build_parser` so a command's own options can
    never be taken for ours. Exits with status 2 (argparse) on a usage error."""
    argv = list(sys.argv[1:] if argv is None else argv)
    command = []
    if argv[:1] in (["run"], ["_wrap"]) and "--" in argv:
        index = argv.index("--")
        argv, command = argv[:index], argv[index + 1:]
    args = build_parser().parse_args(argv)
    args.command = command
    return args


# [@ANCHOR: agent_procs:main]
def main(argv=None):
    """Command-line entry point. Returns the exit code: 0 done, 1 failed or nothing found, 2 usage, 3 refused (protected
    process, reused pid, own ancestry), 75 timeout. A `ProcError` prints `agent_procs: <message>` on standard error."""
    args = parse_args(argv)
    try:
        if args.cmd == "_wrap":
            return wrapper_main(args.name, args.command, timeout=args.timeout, kill_grace=args.kill_grace,
                            mem_max=None if args.mem_max == "none" else args.mem_max, nice=args.nice)
        handler = {"find": cmd_find, "wait": cmd_wait, "run": cmd_run, "jobs": cmd_jobs, "reap": cmd_reap, "stop": cmd_stop,
                   "kill-pid": cmd_kill_pid}[args.cmd]
        return handler(args)
    except ProcError as error:
        print(f"agent_procs: {error}", file=sys.stderr)
        return error.code


if __name__ == "__main__":
    sys.exit(main())
