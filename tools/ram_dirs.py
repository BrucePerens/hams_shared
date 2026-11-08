#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Scratch directories in RAM (`/dev/shm`) instead of on the SSD, with a loud fallback, an owner marker, a sweep of dead
jobs' directories and a per-job size and memory guard (Bruce, 2026-11-08: use the spare RAM to cut SSD writes).

What goes in RAM: Cargo target directories (a build writes 5-12 GB per job and throws it away), the lint Cargo caches
(`lint_cache`), and the TMPDIR of a test run that shares the host (Chrome profiles). What stays on disk: anything that
must survive a reboot, and the PostgreSQL data of the test databases.

Rules every caller gets from this module:
  * A directory goes to RAM only when `/dev/shm` is a tmpfs with at least `min_free_gb` (default 20) free. Otherwise
    the caller gets the SSD path AND a warning on standard error naming the free space and the minimum. Never silent.
  * A machine with less than `MIN_RAM_GB` of memory is not a RAM-directory machine: it keeps its SSD paths without a
    warning (a laptop or a build VM is not expected to hold build output in RAM). `HAMS_RAM_DIRS=0` turns the feature off
    everywhere, `HAMS_RAM_DIRS=1` turns it on regardless of the memory size.
  * Every per-job directory carries an owner marker (`.hams-ram-owner`: pid and process start time), so `sweep` can tell
    a crashed job's directory from a live one and remove it. A job that ends normally, or by a signal, removes its own.
  * `run_job` stops a job whose directory outgrows `job_max_gb` (default 30) or that drives the free space of `/dev/shm`
    below `hard_free_gb` (default 5), so one runaway build cannot take all of the RAM.

CLI (all output on standard error except `path`, which prints the directory):
  ram_dirs.py run JOB [--keep] [--ssd] -- COMMAND...   run COMMAND with CARGO_TARGET_DIR set to the job's directory; the
                                                       directory is removed afterwards (the job-end trap)
  ram_dirs.py path JOB [--owner-pid PID]               create and print the job's directory, owned by PID (default: the
                                                       parent process); remove it with `release JOB`
  ram_dirs.py release JOB                              remove the job's directory
  ram_dirs.py sweep                                    remove the directories of dead jobs
  ram_dirs.py status                                   free space and every directory with its owner and size

Full documentation of every function: tools/README.md "Build and test scratch directories in RAM".
"""

import argparse
import dataclasses
import json
import os
import re
import resource
import shutil
import signal
import subprocess
import sys
import threading
import time

GB = 1024 ** 3
SHM_ROOT = "/dev/shm"
CARGO_PREFIX = "hams-cargo-target-"
TEST_TMP_NAME = "hams-test-tmp"
LINT_CARGO_NAME = "hams-lint-cargo"
OWNER_FILE = ".hams-ram-owner"
DEFAULT_MIN_FREE_GB = 20.0
DEFAULT_JOB_MAX_GB = 30.0
DEFAULT_HARD_FREE_GB = 5.0
MIN_RAM_GB = 64.0
UNMARKED_HOURS = 24.0
TEST_TMP_STALE_HOURS = 12.0
LINT_UNUSED_DAYS = 2
EX_TEMPFAIL = 75
_JOB_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_warned = set()


# [@ANCHOR: ram_dirs:RamPolicy]
@dataclasses.dataclass(frozen=True)
class RamPolicy:
    """The settings every decision in this module reads. See tools/README.md for the field table."""

    mode: str = "auto"
    root: str = SHM_ROOT
    min_free_gb: float = DEFAULT_MIN_FREE_GB
    job_max_gb: float = DEFAULT_JOB_MAX_GB
    hard_free_gb: float = DEFAULT_HARD_FREE_GB
    min_ram_gb: float = MIN_RAM_GB
    require_tmpfs: bool = True


# [@ANCHOR: ram_dirs:policy]
def policy(environ=None):
    """The `RamPolicy` the environment asks for: HAMS_RAM_DIRS (0/off/no, 1/on/yes, else auto), HAMS_RAM_ROOT (another tmpfs or
    directory; naming one turns the tmpfs check off), HAMS_RAM_MIN_FREE_GB, HAMS_RAM_JOB_MAX_GB, HAMS_RAM_HARD_FREE_GB. A number that is missing, not finite or negative
    keeps its default."""
    environ = os.environ if environ is None else environ
    word = (environ.get("HAMS_RAM_DIRS") or "auto").strip().lower()
    mode = "off" if word in ("0", "off", "no", "false") else "on" if word in ("1", "on", "yes", "true") else "auto"

    def number(name, default):
        try:
            value = float(environ.get(name, ""))
        except ValueError:
            return default
        return value if value == value and 0 <= value < float("inf") else default

    return RamPolicy(
        mode=mode,
        root=environ.get("HAMS_RAM_ROOT") or SHM_ROOT,
        min_free_gb=number("HAMS_RAM_MIN_FREE_GB", DEFAULT_MIN_FREE_GB),
        job_max_gb=number("HAMS_RAM_JOB_MAX_GB", DEFAULT_JOB_MAX_GB),
        hard_free_gb=number("HAMS_RAM_HARD_FREE_GB", DEFAULT_HARD_FREE_GB),
        require_tmpfs=not environ.get("HAMS_RAM_ROOT"),
    )


# [@ANCHOR: ram_dirs:total_ram_gb]
def total_ram_gb():
    """Installed memory in GiB from /proc/meminfo, or None when it cannot be read."""
    try:
        with open("/proc/meminfo", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / (1024 ** 2)
    except (OSError, ValueError, IndexError):
        pass
    return None


# [@ANCHOR: ram_dirs:is_tmpfs]
def is_tmpfs(path):
    """True when the file system holding `path` is a tmpfs (the longest matching mount point in /proc/mounts)."""
    best = ("", "")
    try:
        with open("/proc/mounts", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                fields = line.split()
                if len(fields) < 3:
                    continue
                point = fields[1].replace("\\040", " ")
                full = os.path.realpath(path)
                if (full == point or full.startswith(point.rstrip("/") + "/")) and len(point) >= len(best[0]):
                    best = (point, fields[2])
    except OSError:
        return False
    return best[1] == "tmpfs"


# [@ANCHOR: ram_dirs:free_gb]
def free_gb(path):
    """Free space in GiB on the file system holding `path` (its nearest existing parent), or None when unknown."""
    probe = path
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        stat = os.statvfs(probe)
    except OSError:
        return None
    return stat.f_bavail * stat.f_frsize / GB


# [@ANCHOR: ram_dirs:warn_once]
def _warn(key, text, out=None):
    """Print `text` on standard error, once per process for each `key`."""
    if key in _warned:
        return
    _warned.add(key)
    print(f"ram_dirs: WARNING: {text}", file=out or sys.stderr, flush=True)


# [@ANCHOR: ram_dirs:Placement]
@dataclasses.dataclass(frozen=True)
class Placement:
    """Where a directory was put and why. `path` is the directory to use, `in_ram` whether it is under the RAM root,
    `reason` a one-line explanation (always set; a fallback's reason is also printed as a warning)."""

    path: str
    in_ram: bool
    reason: str


# [@ANCHOR: ram_dirs:place]
def place(name, ssd_path, pol=None, environ=None, warn=_warn):
    """Choose between `<root>/<name>` (RAM) and `ssd_path`. Never raises.

    RAM is chosen only when the feature is on, the machine counts as a RAM-directory machine, the root exists as a
    tmpfs (unless `pol.require_tmpfs` is False) and has `min_free_gb` free. A machine below `pol.min_ram_gb` of memory
    in auto mode, and `HAMS_RAM_DIRS=0`, keep the SSD path quietly; every other refusal keeps it with a warning."""
    pol = pol or policy(environ)
    if pol.mode == "off":
        return Placement(ssd_path, False, "RAM directories are switched off (HAMS_RAM_DIRS=0)")
    if pol.mode == "auto":
        ram = total_ram_gb()
        if ram is not None and ram < pol.min_ram_gb:
            return Placement(ssd_path, False, f"this machine has {ram:.0f} GB of memory, under {pol.min_ram_gb:g} GB")
    if not os.path.isdir(pol.root):
        reason = f"{pol.root} does not exist"
    elif pol.require_tmpfs and not is_tmpfs(pol.root):
        reason = f"{pol.root} is not a tmpfs"
    else:
        free = free_gb(pol.root)
        if free is None:
            reason = f"the free space of {pol.root} cannot be read"
        elif free < pol.min_free_gb:
            reason = f"{pol.root} has {free:.1f} GB free, under the {pol.min_free_gb:g} GB minimum (HAMS_RAM_MIN_FREE_GB)"
        else:
            return Placement(os.path.join(pol.root, name), True, f"{pol.root} has {free:.0f} GB free")
    warn(("fallback", reason), f"{reason}; using {ssd_path} on the disk instead of RAM.")
    return Placement(ssd_path, False, reason)


# [@ANCHOR: ram_dirs:lint_cargo_base]
def lint_cargo_base(environ=None, pol=None):
    """`/dev/shm/hams-lint-cargo` when the lint Cargo caches may live in RAM, else None (the caller keeps
    `<cache>/hams-lint-cargo`). None when `XDG_CACHE_HOME` is set (the caller chose a cache location, as the tests and the
    remote runners do) or when `place` refuses RAM (with its warning). Does not create the directory."""
    environ = os.environ if environ is None else environ
    if environ.get("XDG_CACHE_HOME"):
        return None
    placed = place(LINT_CARGO_NAME, "", pol, environ)
    return placed.path if placed.in_ram else None


# [@ANCHOR: ram_dirs:under_ram_root]
def under_ram_root(path, pol=None, environ=None):
    """True when `path` lies below the RAM root (by name; it need not exist)."""
    pol = pol or policy(environ)
    root = os.path.abspath(pol.root).rstrip("/") + "/"
    return os.path.abspath(path).startswith(root)


# [@ANCHOR: ram_dirs:job_directories]
def job_directories(job, pol=None, environ=None):
    """Both places a job's directory can be: `[<root>/hams-cargo-target-<job>, ~/.cache/hams-cargo-target-<job>]`. The job
    may have been placed in either (RAM space changes between `path` and `release`), so release looks in both."""
    environ = os.environ if environ is None else environ
    pol = pol or policy(environ)
    _check_job_name(job)
    base = environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return [os.path.join(pol.root, CARGO_PREFIX + job), os.path.join(base, CARGO_PREFIX + job)]


# [@ANCHOR: ram_dirs:check_job_name]
def _check_job_name(job):
    if not _JOB_RE.match(job or ""):
        raise ValueError(f"job name {job!r} must be 1-64 characters of A-Za-z0-9._- and start with a letter or digit")
    return job


# [@ANCHOR: ram_dirs:cargo_target_dir]
def cargo_target_dir(job, pol=None, environ=None, home=None):
    """The Cargo target directory for job `job`: a `Placement` of `/dev/shm/hams-cargo-target-<job>` or, when RAM is
    refused, `~/.cache/hams-cargo-target-<job>` (the location AGENTS.md used before). Does not create it. Raises
    ValueError for a job name that is empty, longer than 64 characters or has a character outside A-Za-z0-9._-."""
    environ = os.environ if environ is None else environ
    _check_job_name(job)
    base = environ.get("XDG_CACHE_HOME") or os.path.join(home or os.path.expanduser("~"), ".cache")
    return place(CARGO_PREFIX + job, os.path.join(base, CARGO_PREFIX + job), pol, environ)


# [@ANCHOR: ram_dirs:test_tmp_base]
def test_tmp_base(environ=None, pol=None):
    """The base TMPDIR for a test run that shares the host with other runs, `/dev/shm/hams-test-tmp` (created, mode
    1777), or None to keep the caller's default. None also inside the full pipeline's private namespace
    (`HAMS_ISOLATED_NS=1` with `HAMS_TEST_LOCK_HELD=1`): there `/var` and `/tmp` already sit on a memory overlay that
    goes with the namespace, and a directory in the host's /dev/shm would outlive a killed run."""
    environ = os.environ if environ is None else environ
    if environ.get("HAMS_ISOLATED_NS") == "1" and environ.get("HAMS_TEST_LOCK_HELD") == "1":
        return None
    placed = place(TEST_TMP_NAME, "", pol, environ)
    if not placed.in_ram:
        return None
    try:
        os.makedirs(placed.path, exist_ok=True)
        if os.stat(placed.path).st_mode & 0o7777 != 0o1777:
            os.chmod(placed.path, 0o1777)
    except OSError as error:
        _warn(("tmpbase", str(error)), f"cannot prepare {placed.path} ({error}); using the disk TMPDIR instead of RAM.")
        return None
    return placed.path


# [@ANCHOR: ram_dirs:start_ticks]
def _start_ticks(pid):
    """Field 22 of /proc/<pid>/stat (the process start time in clock ticks), or None when the process is gone."""
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as handle:
            text = handle.read()
        return int(text[text.rindex(")") + 2:].split()[19])
    except (OSError, ValueError, IndexError):
        return None


# [@ANCHOR: ram_dirs:claim]
def claim(path, pid=None, now=time.time):
    """Mark directory `path` (created if missing, mode 0700) as owned by process `pid` (default: this one) by writing
    `.hams-ram-owner` in it: JSON with `pid`, `start` (the process start time in clock ticks, so a reused pid is not
    mistaken for the owner) and `created` (seconds since the epoch). Returns `path`. Raises OSError when the marker
    cannot be written."""
    pid = os.getpid() if pid is None else int(pid)
    os.makedirs(path, mode=0o700, exist_ok=True)
    record = {"pid": pid, "start": _start_ticks(pid), "created": int(now())}
    temporary = os.path.join(path, OWNER_FILE + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(record, handle)
    os.replace(temporary, os.path.join(path, OWNER_FILE))
    return path


# [@ANCHOR: ram_dirs:owner_state]
def owner_state(path):
    """`"alive"`, `"dead"` or `"unmarked"` for the directory `path`. alive: its marker names a process that still runs
    with the same start time. dead: the marker names a process that is gone (or whose pid now belongs to another
    process). unmarked: no readable marker."""
    try:
        with open(os.path.join(path, OWNER_FILE), encoding="utf-8") as handle:
            record = json.load(handle)
        pid = int(record["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return "unmarked"
    start = _start_ticks(pid)
    if start is None:
        return "dead"
    recorded = record.get("start")
    return "alive" if recorded is None or recorded == start else "dead"


# [@ANCHOR: ram_dirs:newest_mtime]
def newest_mtime(path, depth=2):
    """Latest modification time of `path` and of its entries down to `depth` levels (a build touches `debug/deps`)."""
    newest = 0.0
    try:
        newest = os.lstat(path).st_mtime
        if depth > 0:
            with os.scandir(path) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        newest = max(newest, newest_mtime(entry.path, depth - 1))
                    else:
                        newest = max(newest, entry.stat(follow_symlinks=False).st_mtime)
    except OSError:
        pass
    return newest


# [@ANCHOR: ram_dirs:remove_tree]
def remove_tree(path):
    """Remove `path` (a symbolic link is only unlinked). True when it is gone afterwards. Never raises."""
    try:
        if os.path.islink(path):
            os.unlink(path)
        else:
            shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass
    return not os.path.lexists(path)


# [@ANCHOR: ram_dirs:sweep]
def sweep(pol=None, environ=None, say=None, now=time.time, remove=remove_tree):
    """Remove the per-job directories (`hams-cargo-target-*`) under the RAM root whose owner is gone, and stale test
    TMPDIR leftovers. Returns the sorted names removed. Never raises.

    A marked directory is removed when `owner_state` is `dead`. An unmarked one (made by hand, as AGENTS.md used to
    describe) is removed when nothing in it changed for `UNMARKED_HOURS` (24) hours. In `hams-test-tmp`, entries that
    are not `slot-K` directories (a run in slot 1 keeps its files directly in the base) go when untouched for
    `TEST_TMP_STALE_HOURS` (12); `slot-K` directories belong to test.py's own slot sweep. A directory that cannot be
    removed (another user's, no permission) is reported, not raised."""
    pol = pol or policy(environ)
    say = say or (lambda text: print(f"ram_dirs: {text}", file=sys.stderr, flush=True))
    removed = []
    try:
        names = sorted(os.listdir(pol.root))
    except OSError:
        return removed
    current = now()
    for name in names:
        full = os.path.join(pol.root, name)
        if name.startswith(CARGO_PREFIX) and os.path.isdir(full) and not os.path.islink(full):
            state = owner_state(full)
            if state == "alive":
                continue
            if state == "unmarked" and current - newest_mtime(full) < UNMARKED_HOURS * 3600:
                continue
            if remove(full):
                removed.append(name)
                say(f"removed {full} ({'its job is gone' if state == 'dead' else 'unmarked and idle'})")
            else:
                say(f"could not remove {full}; remove it with: sudo rm -rf {full}")
        elif name == TEST_TMP_NAME and os.path.isdir(full) and not os.path.islink(full):
            for child in sorted(os.listdir(full)):
                path = os.path.join(full, child)
                if re.match(r"^slot-[0-9]+\Z", child) or os.path.islink(path):
                    continue
                if current - newest_mtime(path) >= TEST_TMP_STALE_HOURS * 3600 and remove(path):
                    removed.append(f"{name}/{child}")
                    say(f"removed stale {path}")
    return sorted(removed)


# [@ANCHOR: ram_dirs:tree_bytes]
def tree_bytes(path):
    """Sum of file sizes under `path` (links not followed). 0 when it does not exist."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


# [@ANCHOR: ram_dirs:guard_verdict]
def guard_verdict(size_bytes, free_now_gb, pol, in_ram):
    """Why a running job must be stopped, or None. For a job whose directory is in RAM: its size above `pol.job_max_gb`,
    or the free space of the RAM root below `pol.hard_free_gb`. A job on the disk is never stopped by this guard."""
    if not in_ram:
        return None
    if size_bytes > pol.job_max_gb * GB:
        return f"its directory reached {size_bytes / GB:.1f} GB, over the {pol.job_max_gb:g} GB per-job limit (HAMS_RAM_JOB_MAX_GB)"
    if free_now_gb is not None and free_now_gb < pol.hard_free_gb:
        return f"{pol.root} fell to {free_now_gb:.1f} GB free, under the {pol.hard_free_gb:g} GB hard floor (HAMS_RAM_HARD_FREE_GB)"
    return None


# [@ANCHOR: ram_dirs:JobResult]
@dataclasses.dataclass
class JobResult:
    """What `run_job` reports: `returncode` (the command's, 128+signal for a signal, `EX_TEMPFAIL` when a live job
    already uses the directory), `target`, `in_ram`, `stopped` (the guard's reason or None), `peak_bytes`, `elapsed`
    (seconds) and `block_write_bytes` (bytes the command and its children caused to be written to block devices, from
    getrusage; 0 for a tmpfs)."""

    returncode: int
    target: str
    in_ram: bool
    stopped: object = None
    peak_bytes: int = 0
    elapsed: float = 0.0
    block_write_bytes: int = 0


# [@ANCHOR: ram_dirs:run_job]
def run_job(job, argv, keep=False, force_ssd=False, pol=None, environ=None, interval=5.0, say=None, popen=subprocess.Popen,
            grace=10.0):
    """Run `argv` with `CARGO_TARGET_DIR` set to the job's own fresh directory, guard it, and remove the directory
    afterwards. Returns a `JobResult`.

    Steps: sweep dead jobs; place the directory (`cargo_target_dir`; `force_ssd` skips RAM); refuse (returncode
    `EX_TEMPFAIL`) when a live job owns the same directory; empty and claim it; start the command in its own session
    so the guard can stop the whole process group; every `interval` seconds measure the directory (peak is kept)
    and apply `guard_verdict` (SIGTERM to the group, SIGKILL after `grace`); on SIGINT, SIGTERM or SIGHUP forward the
    signal to the group; when the command ends remove the directory (unless `keep`) and print one summary line.
    Blocks until the command ends. The remove step is in a `finally`, so an exception here does not leave the directory."""
    environ = dict(os.environ if environ is None else environ)
    say = say or (lambda text: print(f"ram_dirs: {text}", file=sys.stderr, flush=True))
    pol = pol or policy(environ)
    sweep(pol, say=say)
    if force_ssd:
        base = environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
        placed = Placement(os.path.join(base, CARGO_PREFIX + _check_job_name(job)), False, "--ssd")
    else:
        placed = cargo_target_dir(job, pol, environ)
    target = placed.path
    if os.path.lexists(target):
        if owner_state(target) == "alive":
            say(f"the directory {target} belongs to a running job; use another job name (two jobs must not share one)")
            return JobResult(EX_TEMPFAIL, target, placed.in_ram)
        remove_tree(target)
    claim(target)
    environ["CARGO_TARGET_DIR"] = target
    say(f"job {job}: target {target} ({'RAM' if placed.in_ram else 'disk'}: {placed.reason})")
    started = time.monotonic()
    before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_oublock
    state = {"stopped": None, "peak": 0}
    child = None
    previous = {}
    try:
        child = popen(argv, env=environ, start_new_session=True)

        def forward(signum, _frame):
            try:
                os.killpg(child.pid, signum)
            except OSError:
                pass

        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                previous[signum] = signal.signal(signum, forward)
        while True:
            try:
                returncode = child.wait(timeout=interval)
                break
            except subprocess.TimeoutExpired:
                size = tree_bytes(target)
                state["peak"] = max(state["peak"], size)
                reason = None if state["stopped"] else guard_verdict(size, free_gb(pol.root), pol, placed.in_ram)
                if reason:
                    state["stopped"] = reason
                    say(f"STOPPING job {job}: {reason}")
                    _stop_group(child, grace)
        state["peak"] = max(state["peak"], tree_bytes(target))
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        if child is not None and child.poll() is None:
            _stop_group(child, grace)
        if not keep:
            remove_tree(target)
    elapsed = time.monotonic() - started
    blocks = (resource.getrusage(resource.RUSAGE_CHILDREN).ru_oublock - before) * 512
    code = returncode if returncode >= 0 else 128 - returncode
    result = JobResult(code, target, placed.in_ram, state["stopped"], state["peak"], elapsed, blocks)
    say(f"job {job}: exit {code} after {elapsed:.0f} s, directory peaked at {state['peak'] / GB:.2f} GB"
        f" ({'RAM' if placed.in_ram else 'disk'}), block writes {blocks / GB:.2f} GB, "
        + ("kept" if keep else "removed"))
    return result


# [@ANCHOR: ram_dirs:stop_group]
def _stop_group(child, grace):
    """SIGTERM the child's process group, SIGKILL it after `grace` seconds."""
    for signum, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 5.0)):
        try:
            os.killpg(child.pid, signum)
        except OSError:
            return
        try:
            child.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


# [@ANCHOR: ram_dirs:status_lines]
def status_lines(pol=None, environ=None):
    """Lines describing the RAM root: free space, and each of our directories with its owner state and size."""
    pol = pol or policy(environ)
    lines = [f"{pol.root}: {free_gb(pol.root) or 0:.1f} GB free, minimum {pol.min_free_gb:g}, per-job limit {pol.job_max_gb:g}, hard floor {pol.hard_free_gb:g} (mode {pol.mode})"]
    try:
        names = sorted(os.listdir(pol.root))
    except OSError:
        return lines
    for name in names:
        if name.startswith(CARGO_PREFIX) or name in (TEST_TMP_NAME, LINT_CARGO_NAME):
            full = os.path.join(pol.root, name)
            lines.append(f"  {name}: {tree_bytes(full) / GB:.2f} GB, {owner_state(full)}")
    return lines


# [@ANCHOR: ram_dirs:main]
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("job")
    run.add_argument("--keep", action="store_true", help="leave the directory in place afterwards")
    run.add_argument("--ssd", action="store_true", help="use ~/.cache instead of RAM (still removed afterwards)")
    run.add_argument("command_line", nargs=argparse.REMAINDER)
    path = sub.add_parser("path")
    path.add_argument("job")
    path.add_argument("--owner-pid", type=int, default=None)
    release = sub.add_parser("release")
    release.add_argument("job")
    sub.add_parser("sweep")
    sub.add_parser("status")
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            command_line = args.command_line[1:] if args.command_line[:1] == ["--"] else args.command_line
            if not command_line:
                parser.error("run needs a command after --")
            return run_job(args.job, command_line, keep=args.keep, force_ssd=args.ssd).returncode
        if args.command == "path":
            placed = cargo_target_dir(args.job)
            if os.path.lexists(placed.path) and owner_state(placed.path) == "alive":
                print(f"ram_dirs: {placed.path} belongs to a running job; use another job name", file=sys.stderr)
                return EX_TEMPFAIL
            sweep()
            remove_tree(placed.path)
            claim(placed.path, args.owner_pid if args.owner_pid else os.getppid())
            print(placed.path)
            return 0
        if args.command == "release":
            for candidate in job_directories(args.job):
                if os.path.lexists(candidate):
                    remove_tree(candidate)
            return 0
        if args.command == "sweep":
            sweep()
            return 0
        print("\n".join(status_lines()))
        return 0
    except ValueError as error:
        print(f"ram_dirs: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
