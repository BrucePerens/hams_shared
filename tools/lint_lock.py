#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Run lock and load gate for run_linters.py.

Many agents lint at once on the shared dev box. Each `run_linters.py --changed` starts as many
workers as half the cores; ten of them together push the load to 50, every timing-sensitive test
fails, and each run takes 20-40 minutes instead of 5. So heavy runs queue:

  * RUN LOCK. One heavy run per machine (and per user): an exclusive flock on
    `~/.cache/hams-lint/run.lock`, fronted by the FIFO ticket queue of `lock_queue.py` (the queue
    test.py uses), so waiters are served in order and each prints
    "waiting for the lint run in <worktree> (pid N), position K". The kernel drops the flock when
    the holder dies, and a dead waiter's ticket is ignored (pid and start time are checked), so
    nothing can be left stale. The lock is per machine: a run on another host (run_linters.py
    --remote) takes that host's own lock.
  * CHEAP BYPASS. A run whose uncached steps all have a known duration and add up to no more than
    HAMS_LINT_CHEAP_SECONDS (default 60) takes no lock and no gate: `--only check_summation_bias`
    must not wait behind a half-hour run. A step with no recorded duration counts as heavy. A run
    in which every step is a cached pass takes no lock either.
  * LOAD GATE. After the lock is held, a heavy run waits (bounded) until the one-minute load
    average is at or below the limit (default: the number of cores) and fails clearly when it
    does not get there. Same environment names as test.py's CI load gate: HAMS_SKIP_CI_LOAD_GATE=1
    skips it, HAMS_CI_LOAD_GATE_RATIO scales the limit (cores x ratio), HAMS_CI_LOAD_GATE_TIMEOUT
    bounds the wait; HAMS_LINT_LOAD_LIMIT / HAMS_LINT_LOAD_TIMEOUT (or --load-limit /
    --load-timeout) override those for lint only. The runner-process check of test.py's gate is not
    used here: a lint run does not need the box free of CI, only not overloaded.
  * DISTRIBUTION (2026-11-03). A run that would have to queue here, because a run holds or waits for the
    lock or the load is above the limit, does not wait while another machine is idle: run_linters.py
    --remote auto ships the whole run (every step the machine can run) to the least-loaded capable
    machine, which has its own lock and gate, and takes this lock only for the steps that must stay
    here. That remainder (git history, network, this checkout: about 75 s in all) does not queue behind
    other runs either when its known durations total at most HAMS_LINT_RESIDUE_SECONDS (default 240): it
    waits only for the load gate. `local_busy` is that decision; `claim` keeps two runs on this machine from picking the same
    idle machine at the same moment (an flock per machine, held while the run's share is executing).
  * OVERRIDES. HAMS_LINT_NO_LOCK=1 (CI, a machine that is used by one run at a time) takes no lock;
    the gate has its own switch above. The holder exports HAMS_LINT_LOCK_HELD so the runs it
    starts itself (tests that call run_linters.py) never queue behind their own parent.
"""

import fcntl
import json
import math
import os
import statistics
import sys
import time

import lock_queue

DEFAULT_CHEAP_SECONDS = 60.0
DEFAULT_RESIDUE_SECONDS = 240.0
DEFAULT_LOAD_TIMEOUT_SECONDS = 900.0
EXIT_TEMPFAIL = lock_queue.EXIT_WAIT_TIMEOUT  # 75, same as test.py's wait timeout
POLL_SECONDS = 15.0
HELD_ENV = "HAMS_LINT_LOCK_HELD"


def cache_dir(environ=None):
    environ = os.environ if environ is None else environ
    base = environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "hams-lint")


def lock_path(environ=None):
    return os.path.join(cache_dir(environ), "run.lock")


def queue_dir(environ=None):
    return lock_queue.queue_dir_for_lock(lock_path(environ), {})


def _finite(text, default):
    try:
        value = float(text)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def cheap_seconds(environ=None):
    environ = os.environ if environ is None else environ
    return _finite(environ.get("HAMS_LINT_CHEAP_SECONDS"), DEFAULT_CHEAP_SECONDS)


def known_durations(directory=None):
    """step name -> median recorded seconds, over every cache file of this user (`lint_exec.Cache`
    stores the last measured duration of each step it ran). Unreadable files are skipped."""
    directory = directory or cache_dir()
    samples = {}
    try:
        names = os.listdir(directory)
    except OSError:
        return {}
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(directory, name), encoding="utf-8") as handle:
                data = json.load(handle)
            seconds = data.get("seconds", {})
        except (OSError, ValueError, AttributeError):
            continue
        for step, value in seconds.items():
            if isinstance(value, (int, float)) and math.isfinite(value):
                samples.setdefault(step, []).append(float(value))
    return {step: statistics.median(values) for step, values in samples.items()}


def is_cheap(step_names, durations, threshold):
    """True when every step has a recorded duration and they add up to at most `threshold`.
    Steps with no recorded duration are heavy. No steps at all is cheap (nothing to protect)."""
    total = 0.0
    for name in step_names:
        if name not in durations:
            return False
        total += durations[name]
    return total <= threshold


def lock_decision(environ, pending_names, durations):
    """(take_lock: bool, reason: str). `pending_names` are the steps that will actually run
    (cached passes and static failures excluded)."""
    if environ.get("HAMS_LINT_NO_LOCK") == "1":
        return False, "HAMS_LINT_NO_LOCK=1"
    if environ.get(HELD_ENV):
        return False, "the lock is held by the parent run (pid %s)" % environ[HELD_ENV]
    if not pending_names:
        return False, "no step left to run here (cached passes, or sent to other machines)"
    threshold = cheap_seconds(environ)
    if is_cheap(pending_names, durations, threshold):
        return False, "cheap run (known steps total at most %g s)" % threshold
    return True, "heavy run"


def residue_seconds(environ=None):
    environ = os.environ if environ is None else environ
    return _finite(environ.get("HAMS_LINT_RESIDUE_SECONDS"), DEFAULT_RESIDUE_SECONDS)


def describe_info(worktree, command):
    return "%s\t%s" % (worktree, command)


def _split_info(info):
    worktree, _, command = (info or "").partition("\t")
    return worktree or "?", command


def waiting_message(qdir, pid=None):
    """'waiting for the lint run in <worktree> (pid N), position K'."""
    pid = os.getpid() if pid is None else pid
    holder = lock_queue.read_holder(qdir)
    # The running holder keeps its own ticket (lock_queue keeps it for the whole run); it is not ahead
    # of anyone in the waiting line.
    tickets = [t for t in lock_queue.live_tickets(qdir, clean=False) if not (holder and t["pid"] == holder.get("pid"))]
    position = len(tickets) + 1
    for index, ticket in enumerate(tickets, 1):
        if ticket["pid"] == pid:
            position = index
            break
    if holder:
        worktree, _command = _split_info(holder.get("info"))
        return "waiting for the lint run in %s (pid %s), position %d" % (worktree, holder.get("pid"), position)
    return "waiting for another lint run (holder not recorded), position %d" % position


class RunLock:
    """The machine-wide run lock. `acquire` returns True when held, False on timeout."""

    def __init__(self, environ=None):
        self.environ = os.environ if environ is None else environ
        self.path = lock_path(self.environ)
        self.qdir = queue_dir(self.environ)
        self.fd = None
        self.ticket = None

    def _try_flock(self):
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def acquire(self, info, timeout=None, out=print, report_every=30.0, poll=1.5, sleep=time.sleep, now=time.time):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o666)  # not inheritable: steps never hold it
        status, ticket = lock_queue.acquire(
            self.qdir,
            self._try_flock,
            info=info,
            timeout=timeout,
            poll=poll,
            report_every=report_every,
            sleep=sleep,
            now=now,
            out=lambda _line: out("[*] " + waiting_message(self.qdir)),
        )
        if status != lock_queue.ACQUIRED:
            os.close(self.fd)
            self.fd = None
            return False
        self.ticket = ticket
        lock_queue.write_holder(self.qdir, info)
        # Runs started by this one (tests that call run_linters.py) must not queue behind us.
        os.environ[HELD_ENV] = str(os.getpid())
        return True

    def release(self):
        if self.ticket:
            lock_queue.release_ticket(self.qdir, self.ticket)
            self.ticket = None
        holder = lock_queue.read_holder(self.qdir)
        if holder and holder.get("pid") == os.getpid():
            try:
                os.unlink(os.path.join(self.qdir, "holder-%d-%d" % (holder["pid"], holder["start"])))
            except OSError:
                pass
        if self.fd is not None:
            try:
                os.close(self.fd)  # drops the flock
            except OSError:
                pass
            self.fd = None
        os.environ.pop(HELD_ENV, None)


def local_busy(environ=None, loadavg=None, cpu_count=None, qdir=None):
    """(busy, reason): True when a heavy run started now would wait here, because a lint run holds or waits
    for the lock, or the 1-minute load average is above the limit. Takes no lock; reads the queue only."""
    environ = os.environ if environ is None else environ
    qdir = qdir or queue_dir(environ)
    tickets = lock_queue.live_tickets(qdir, clean=False)
    holder = lock_queue.read_holder(qdir)
    if tickets or holder:
        return True, "%d lint run(s) hold or wait for the lock here" % max(len(tickets), 1)
    if environ.get("HAMS_SKIP_CI_LOAD_GATE") == "1":
        return False, "the lock is free"
    try:
        current = (loadavg or (lambda: os.getloadavg()[0]))()
    except OSError:
        return False, "the lock is free"
    limit = load_limit(environ, cpu_count=cpu_count)
    if current > limit:
        return True, "load %.1f is above the lint limit %.1f" % (current, limit)
    return False, "the lock is free and the load is %.1f" % current


def claim_dir(environ=None):
    return os.path.join(cache_dir(environ), "claims")


def try_claim(machine, environ=None):
    """Claim `machine` for the whole run of this process: an exclusive non-blocking flock on
    claims/<machine>.lock. Returns the open file (close it to release; the kernel drops it when the process
    dies) or None when another run on this machine has it."""
    directory = claim_dir(environ)
    try:
        os.makedirs(directory, exist_ok=True)
        handle = open(os.path.join(directory, machine + ".lock"), "a+", encoding="utf-8")  # noqa: SIM115 - held by the caller
    except OSError:
        return None
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


# [@ANCHOR: lint_lock:held_locks]
class HeldLocks:
    """Several open lock files released together: `close()` closes every one (and may be called again). Full entry: tools/README.md."""

    def __init__(self, handles):
        self.handles = [handle for handle in handles if handle is not None]

    def close(self):
        for handle in self.handles:
            handle.close()


ODOO_TEST_LOCK_DIR = os.path.expanduser("~/.cache/hams-remote-test")


# [@ANCHOR: lint_lock:try_odoo_test_lock]
def try_odoo_test_lock(lock_name, directory=None):
    """Take the dev box's Odoo-test lock for a remote host without waiting; None when an Odoo test run holds it. Full entry: tools/README.md."""
    directory = directory or ODOO_TEST_LOCK_DIR
    try:
        os.makedirs(directory, exist_ok=True)
        handle = open(os.path.join(directory, lock_name + ".lock"), "a+", encoding="utf-8")  # noqa: SIM115 - held by the caller
    except OSError:
        return None
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write("pid %d lint run since %s\n" % (os.getpid(), time.strftime("%Y-%m-%dT%H:%M:%S")))
    handle.flush()
    return handle


def claimed(machines, environ=None):
    """The subset of `machines` that another run holds a claim on right now."""
    taken = set()
    for machine in machines:
        handle = try_claim(machine, environ)
        if handle is None:
            taken.add(machine)
        else:
            handle.close()
    return taken


def status_lines(environ=None):
    """For `--lock-status`: the holder and the queue."""
    qdir = queue_dir(environ)
    return lock_queue.describe_queue(qdir)


def load_limit(environ=None, cpu_count=None, override=None):
    """The 1-minute load average above which a heavy run does not start."""
    environ = os.environ if environ is None else environ
    cores = cpu_count or os.cpu_count() or 1
    if override is not None:
        return float(override)
    if environ.get("HAMS_LINT_LOAD_LIMIT"):
        return _finite(environ["HAMS_LINT_LOAD_LIMIT"], float(cores))
    ratio = _finite(environ.get("HAMS_CI_LOAD_GATE_RATIO"), 1.0)
    return max(1.0, cores * ratio)


def load_timeout(environ=None, override=None):
    environ = os.environ if environ is None else environ
    if override is not None:
        return float(override)
    for name in ("HAMS_LINT_LOAD_TIMEOUT", "HAMS_CI_LOAD_GATE_TIMEOUT"):
        if environ.get(name):
            return _finite(environ[name], DEFAULT_LOAD_TIMEOUT_SECONDS)
    return DEFAULT_LOAD_TIMEOUT_SECONDS


def wait_for_load(limit, timeout, environ=None, loadavg=None, sleep=time.sleep, now=time.monotonic, out=print):
    """Block until the 1-minute load average is at or below `limit`. True when the run may start,
    False when `timeout` seconds passed first. HAMS_SKIP_CI_LOAD_GATE=1 skips the wait."""
    environ = os.environ if environ is None else environ
    if environ.get("HAMS_SKIP_CI_LOAD_GATE") == "1":
        out("[*] Skipping the lint load gate (HAMS_SKIP_CI_LOAD_GATE=1).")
        return True
    loadavg = loadavg or (lambda: os.getloadavg()[0])
    try:
        current = loadavg()
    except OSError:
        return True  # no load average on this platform: nothing to gate on
    if current <= limit:
        return True
    deadline = now() + timeout
    out(
        "[*] The one-minute load average is %.1f, above the lint limit of %.1f: waiting up to %d s for it to "
        "fall (HAMS_SKIP_CI_LOAD_GATE=1 skips this, HAMS_LINT_LOAD_LIMIT / --load-limit change it)."
        % (current, limit, int(timeout))
    )
    while now() < deadline:
        sleep(min(POLL_SECONDS, max(0.0, deadline - now())))
        try:
            current = loadavg()
        except OSError:
            return True
        if current <= limit:
            out("[*] Load average %.1f: starting." % current)
            return True
    out(
        "ERROR: the load average is still %.1f (limit %.1f) after %d s. Not starting a heavy lint run: its "
        "timing tests would fail for load, not for code. Run again when the box is quiet, raise "
        "--load-timeout, or set HAMS_SKIP_CI_LOAD_GATE=1." % (current, limit, int(timeout))
    )
    return False


def begin_heavy_run(environ, pending_names, durations, worktree, command, lock_timeout=None,
                    load_limit_override=None, load_timeout_override=None, out=print, residue=False):
    """Everything run_linters.py does before it starts steps. Returns (ok, RunLock or None); on
    not ok the caller exits with EXIT_TEMPFAIL after printing what `out` received.

    `residue`: the rest of this run went to other machines, so these are the few steps that had to stay
    (or came back): when they are short they take no place in the queue, only the load gate."""
    take, reason = lock_decision(environ, pending_names, durations)
    if take and residue and is_cheap(pending_names, durations, residue_seconds(environ)):
        out("[*] lint lock not needed: the steps left here total at most %g s (the rest of the run is on other machines)"
            % residue_seconds(environ))
        if not wait_for_load(load_limit(environ, override=load_limit_override), load_timeout(environ, load_timeout_override),
                             environ=environ, out=out):
            return False, None
        return True, None
    if not take:
        if reason.startswith(("cheap", "no step")):
            out("[*] lint lock not needed: " + reason)
        return True, None
    lock = RunLock(environ)
    if not lock.acquire(describe_info(worktree, command), timeout=lock_timeout, out=out):
        out("ERROR: gave up waiting for the lint run lock after %g s (%s)." % (lock_timeout, lock.path))
        return False, None
    limit = load_limit(environ, override=load_limit_override)
    if not wait_for_load(limit, load_timeout(environ, load_timeout_override), environ=environ, out=out):
        lock.release()
        return False, None
    return True, lock


if __name__ == "__main__":  # pragma: no cover
    print("\n".join(status_lines()))
    sys.exit(0)
