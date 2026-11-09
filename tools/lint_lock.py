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
  * DISTRIBUTION (2026-11-03, narrowed 2026-11-08: overflow only). A run that would have to queue here, because every slot is
    taken or a run waits for one (on a single-slot machine also: the load is above the limit), does not wait while another machine is idle: run_linters.py
    --remote auto ships the whole run (every step the machine can run) to the least-loaded capable
    machine, which has its own lock and gate, and takes this lock only for the steps that must stay
    here. That remainder (git history, network, this checkout: about 75 s in all) does not queue behind
    other runs either when its known durations total at most HAMS_LINT_RESIDUE_SECONDS (default 240): it
    waits only for the load gate. `local_busy` is that decision; `claim` keeps two runs on this machine from picking the same
    idle machine at the same moment (an flock per machine, held while the run's share is executing).
  * SLOTS (2026-11-08). The dev box has 32 cores and 125 GB: it is the primary lint runner and lets several heavy runs go at
    once, side by side. `slot_count` is the number of runs (HAMS_LINT_SLOTS, else the machine's `lint_slots` in
    lint_machines.json, else 1: the small remote machines keep one). Slot 1 is the lock file above; slot K is
    `run.lock.slot-K`. The FIFO queue stays one line in front of all slots (lock_queue.py `slots`), the holder records name
    their slot, and a run in slot K gets its own Cargo target directory (lint_cache.cargo_target_dir adds `-sK`) and its
    own TMPDIR (slots above 1: `<TMPDIR>/s<K>`), so concurrent runs never share a build directory or a scratch file. The load gate still applies to every
    run, and runs under slots go on at `nice` 10 so a saturated box stays usable.
  * OVERRIDES. HAMS_LINT_NO_LOCK=1 (CI, a machine that is used by one run at a time) takes no lock;
    the gate has its own switch above. The holder exports HAMS_LINT_LOCK_HELD so the runs it
    starts itself (tests that call run_linters.py) never queue behind their own parent.
"""

import fcntl
import json
import math
import os
import re
import statistics
import sys
import time

import lock_queue
import runner_slots

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


SLOTS_ENV = "HAMS_LINT_SLOTS"
SLOT_ENV = "HAMS_LINT_SLOT"
SLOT_NICE = 10
MACHINES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lint_machines.json")


SLOTS_FILE = "/opt/hams/etc/lint_slots"


# [@ANCHOR: lint_lock:slots_files]
def slots_files(environ=None):
    """The setting files `slot_count` reads, first match wins: `$HAMS_LINT_SLOTS_FILE` alone when set (tests), else the machine-wide
    `/opt/hams/etc/lint_slots` (like test.py's `test_slots`), then the user's `~/.config/hams-lint/slots`. The first line holds the number.
    Full entry: tools/README.md."""
    environ = os.environ if environ is None else environ
    if environ.get("HAMS_LINT_SLOTS_FILE"):
        return [environ["HAMS_LINT_SLOTS_FILE"]]
    home = environ.get("HOME") or os.path.expanduser("~")
    return [SLOTS_FILE, os.path.join(home, ".config", "hams-lint", "slots")]


# [@ANCHOR: lint_lock:jobs_per_slot]
def jobs_per_slot(cpu_count, slots):
    """Parallel steps (`-j`) one run starts on a machine with `slots` lint slots: half the CPUs (the default of a lone run) divided by the
    slots, at least 2 when there are several slots, so that all slots together do not run more workers than half the CPUs. 32 CPUs
    (16 cores) over 4 slots is 4 each. Full entry: tools/README.md."""
    half = max(1, int(cpu_count) // 2)
    return half if slots <= 1 else max(2, half // slots)


# [@ANCHOR: lint_lock:slot_count]
def slot_count(environ=None, machines_file=None):
    """How many heavy lint runs this machine lets go at once: HAMS_LINT_SLOTS, else the first line of the setting file (`slots_files`), else `lint_slots` of this machine's entry in
    lint_machines.json (the entry lint_cache.machine_name picks), else 1. A value that is not a whole number from 1 to
    runner_slots.MAX_SLOTS is ignored (a typo must not stop the lint, and one slot is the safe answer). Full entry: tools/README.md."""
    environ = os.environ if environ is None else environ
    if environ.get(SLOTS_ENV):
        try:
            return runner_slots.parse_slot_count(environ[SLOTS_ENV], SLOTS_ENV)
        except runner_slots.SlotConfigError:
            return 1
    for path in slots_files(environ):
        try:
            with open(path, encoding="utf-8") as handle:
                first_line = handle.readline()
        except OSError:
            continue  # no such file (the machine has not opted in this way) or unreadable: the next source
        try:
            return runner_slots.parse_slot_count(first_line, path)
        except runner_slots.SlotConfigError:
            return 1
    try:
        import lint_cache

        name = lint_cache.machine_name(environ, machines_file)
        with open(machines_file or MACHINES_FILE, encoding="utf-8") as handle:
            entry = json.load(handle).get("machines", {}).get(name, {})
        return runner_slots.parse_slot_count(entry.get("lint_slots", 1), "lint_slots")
    except (OSError, ValueError, AttributeError, runner_slots.SlotConfigError):
        return 1


# [@ANCHOR: lint_lock:slot_lock_path]
def slot_lock_path(slot, environ=None):
    """The lock file of `slot`: run.lock for slot 1 (so a checkout without slots excludes the slotted runs on it), run.lock.slot-K."""
    return runner_slots.slot_lock_path(lock_path(environ), slot)


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


def waiting_message(qdir, pid=None, slots=1):
    """'waiting for the lint run in <worktree> (pid N), position K'."""
    pid = os.getpid() if pid is None else pid
    holder = lock_queue.read_holder(qdir)
    # The running holder keeps its own ticket (lock_queue keeps it for the whole run); it is not ahead
    # of anyone in the waiting line. With slots every running holder's ticket is one of those.
    running_pids = {h.get("pid") for h in lock_queue.read_holders(qdir)} if slots > 1 else {holder.get("pid")} if holder else set()
    tickets = [t for t in lock_queue.live_tickets(qdir, clean=False) if t["pid"] not in running_pids]
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
    """The machine-wide run lock. `acquire` returns True when held, False on timeout. With `slots` > 1 (see `slot_count`)
    that many runs hold one lock file each at the same time; `slot` is the number taken (1 when there is one slot)."""

    def __init__(self, environ=None, slots=None):
        self.environ = os.environ if environ is None else environ
        self.path = lock_path(self.environ)
        self.qdir = queue_dir(self.environ)
        self.slots = slot_count(self.environ) if slots is None else slots
        self.fd = None
        self.slot = None
        self.ticket = None

    def _try_flock(self):
        """One non-blocking pass over the slot files, lowest first; keeps the first one it locks open in self.fd."""
        for slot in range(1, self.slots + 1):
            fd = os.open(slot_lock_path(slot, self.environ), os.O_CREAT | os.O_RDWR, 0o666)  # not inheritable: steps never hold it
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                continue
            self.fd, self.slot = fd, slot
            return True
        return False

    def acquire(self, info, timeout=None, out=print, report_every=30.0, poll=1.5, sleep=time.sleep, now=time.time):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        status, ticket = lock_queue.acquire(
            self.qdir,
            self._try_flock,
            info=info,
            timeout=timeout,
            poll=poll,
            report_every=report_every,
            sleep=sleep,
            now=now,
            out=lambda _line: out("[*] " + waiting_message(self.qdir, slots=self.slots)),
            slots=self.slots,
        )
        if status != lock_queue.ACQUIRED:
            return False
        self.ticket = ticket
        if self.slots > 1:
            lock_queue.write_holder(self.qdir, info, slot=self.slot, ticket=ticket)
        else:
            lock_queue.write_holder(self.qdir, info)
        # Runs started by this one (tests that call run_linters.py) must not queue behind us.
        os.environ[HELD_ENV] = str(os.getpid())
        os.environ[SLOT_ENV] = str(self.slot)
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
        os.environ.pop(SLOT_ENV, None)


# [@ANCHOR: lint_lock:enter_slot]
def enter_slot(lock, environ=None, base_tmp=None):
    """Give a run that holds a slot of a machine with several its own working space: it runs at nice SLOT_NICE, and with
    a slot above 1 its TMPDIR is `<TMPDIR or /tmp>/s<K>` (created; short, see below). Returns the environment changes
    ({} for a run without a lock or with a single slot) so the caller can pass them on to steps whose environment was
    captured earlier. Full entry: tools/README.md."""
    environ = os.environ if environ is None else environ
    if lock is None or lock.slot is None or lock.slots <= 1:
        return {}
    try:
        os.nice(SLOT_NICE)
    except OSError:
        pass
    base = base_tmp or environ.get("TMPDIR") or "/tmp"
    # Slot 1 keeps the caller's TMPDIR; a later slot gets `<TMPDIR>/s<K>`. The name is deliberately short: Chrome's profile
    # directory lives under TMPDIR and its singleton socket path must stay under the 107 bytes of a unix socket address
    # (`<TMPDIR>/hams-lint-slot-1/owl_template_check_x/com.google.Chrome.x/SingletonSocket` was 119 and failed 2026-11-08).
    if lock.slot > 1:
        if re.fullmatch(r"s\d+", os.path.basename(base)):
            base = os.path.dirname(base)
        base = os.path.join(base, "s%d" % lock.slot)
        os.makedirs(base, exist_ok=True)
    changes = {SLOT_ENV: str(lock.slot), "TMPDIR": base}
    os.environ.update(changes)
    return changes


# [@ANCHOR: lint_lock:running_count]
def running_count(qdir):
    """How many runs hold a slot right now (live holder records, one per slot)."""
    return len(lock_queue.read_holders(qdir))


def local_busy(environ=None, loadavg=None, cpu_count=None, qdir=None, slots=None):
    """(busy, reason): True when a heavy run started now would wait here for a slot: every slot is taken or a run waits for one.
    With a single slot that is "a lint run holds or waits for the lock", as before, and also a 1-minute load average above the
    limit; with several slots the load does not count (the run waits at the load gate instead, which `load_limit` sizes for the
    machine). Takes no lock; reads the queue only."""
    environ = os.environ if environ is None else environ
    qdir = qdir or queue_dir(environ)
    slots = slot_count(environ) if slots is None else slots
    tickets = lock_queue.live_tickets(qdir, clean=False)
    holders = lock_queue.read_holders(qdir)
    if slots <= 1:
        if tickets or lock_queue.read_holder(qdir):
            return True, "%d lint run(s) hold or wait for the lock here" % max(len(tickets), 1)
    else:
        running = {h.get("ticket") for h in holders if h.get("ticket")}
        running_pids = {h.get("pid") for h in holders}  # a holder written by an older checkout names no ticket
        waiting = [t for t in tickets if t["name"] not in running and t["pid"] not in running_pids]
        if len(holders) >= slots:
            return True, "all %d lint slots are busy here (%d waiting)" % (slots, len(waiting))
        if waiting:
            return True, "%d lint run(s) wait for a slot here (%d of %d slots running)" % (len(waiting), len(holders), slots)
    if slots > 1:
        # A machine with several slots (the dev box) is not "busy" because its load is high: that is its other work, mostly niced
        # (cargo tests, the other lint runs), and a smaller machine would finish later than this run waiting at the load gate.
        return False, "a lint slot is free (%d of %d running)" % (len(holders), slots)
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
    return lock_queue.describe_queue(qdir, slots=slot_count(environ))


def load_limit(environ=None, cpu_count=None, override=None):
    """The 1-minute load average above which a heavy run does not start."""
    environ = os.environ if environ is None else environ
    cores = cpu_count or os.cpu_count() or 1
    if override is not None:
        return float(override)
    if environ.get("HAMS_LINT_LOAD_LIMIT"):
        return _finite(environ["HAMS_LINT_LOAD_LIMIT"], float(cores))
    ratio = _finite(environ.get("HAMS_CI_LOAD_GATE_RATIO"), machine_load_ratio(environ))
    return max(1.0, cores * ratio)


# [@ANCHOR: lint_lock:machine_load_ratio]
def machine_load_ratio(environ=None, machines_file=None):
    """The load-per-core ratio of this machine's load gate for lint: `lint_load_ratio` of its entry in lint_machines.json (the dev box: 1.5,
    because with several lint slots and niced cargo tests the load runs above the core count by design), else 1.0. Full entry: tools/README.md."""
    environ = os.environ if environ is None else environ
    try:
        import lint_cache

        name = lint_cache.machine_name(environ, machines_file)
        with open(machines_file or MACHINES_FILE, encoding="utf-8") as handle:
            value = json.load(handle).get("machines", {}).get(name, {}).get("lint_load_ratio", 1.0)
        return _finite(value, 1.0) if isinstance(value, (int, float)) and value > 0 else 1.0
    except (OSError, ValueError, AttributeError):
        return 1.0


def load_timeout(environ=None, override=None):
    environ = os.environ if environ is None else environ
    if override is not None:
        return float(override)
    for name in ("HAMS_LINT_LOAD_TIMEOUT", "HAMS_CI_LOAD_GATE_TIMEOUT"):
        if environ.get(name):
            return _finite(environ[name], DEFAULT_LOAD_TIMEOUT_SECONDS)
    return DEFAULT_LOAD_TIMEOUT_SECONDS


PSI_CPU = "/proc/pressure/cpu"
# The same two signals and limits as test.py's CI load gate (#438, `evaluate_cpu_saturation_gate`): a box is saturated only when the
# processors are busy AND runnable tasks waited for them. test.py is too large to import here, so the few lines are repeated.
DEFAULT_BUSY_PERCENT = 92.0
DEFAULT_PSI_PERCENT = 25.0
SAMPLE_SECONDS = 1.0


# [@ANCHOR: lint_lock:cpu_stall]
def cpu_stall(path=PSI_CPU):
    """The measured CPU stall of this machine: `some avg10` of /proc/pressure/cpu, the percentage of the last ten seconds in which at
    least one runnable task waited for a CPU. None when the kernel does not provide it. Unlike the load average (86 on the 32-thread dev box
    at 18 % stall, 2026-11-08: it counts every runnable or disk-waiting task, niced cargo builds included) it says whether more work would
    only wait. Full entry: tools/README.md."""
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("some "):
                    for field in line.split()[1:]:
                        if field.startswith("avg10="):
                            value = float(field[len("avg10="):])
                            return value if math.isfinite(value) else None
    except (OSError, ValueError):
        pass
    return None


# [@ANCHOR: lint_lock:cpu_busy_percent]
def cpu_busy_percent(window=SAMPLE_SECONDS, path="/proc/stat", sleep=time.sleep):
    """The processors' busy percentage (0 to 100) over `window` seconds from two samples of the first line of /proc/stat (everything but idle and
    iowait is busy, guest time is already inside user and nice). None when the file cannot be read or the counters did not move. Full entry: tools/README.md."""
    def sample():
        try:
            with open(path, encoding="utf-8") as handle:
                fields = handle.readline().split()
            values = [int(v) for v in fields[1:9]]
        except (OSError, ValueError):
            return None
        if fields[:1] != ["cpu"] or len(values) < 5:
            return None
        total = sum(values)
        return total - values[3] - values[4], total

    first = sample()
    sleep(window)
    second = sample()
    if first is None or second is None or second[1] <= first[1]:
        return None
    return max(0.0, min(100.0, 100.0 * (second[0] - first[0]) / (second[1] - first[1])))


def saturation_limits(environ=None):
    """(busy percent, pressure percent) limits: HAMS_CI_LOAD_GATE_CPU_BUSY_PERCENT (92) and HAMS_CI_LOAD_GATE_PSI_PERCENT (25), the names test.py uses."""
    environ = os.environ if environ is None else environ
    return (_finite(environ.get("HAMS_CI_LOAD_GATE_CPU_BUSY_PERCENT"), DEFAULT_BUSY_PERCENT),
            _finite(environ.get("HAMS_CI_LOAD_GATE_PSI_PERCENT"), DEFAULT_PSI_PERCENT))


# [@ANCHOR: lint_lock:saturated]
def saturated(busy, psi, busy_limit, psi_limit):
    """(is_saturated, reason). With both signals the box is saturated only when BOTH are at or above their limits (a full box that keeps up is
    fine; waiting tasks on idle processors are the many-short-processes case the load average mistakes for load); with one missing the other
    decides alone; with neither it is not saturated. Pure. Full entry: tools/README.md."""
    signals = []
    if busy is not None:
        signals.append((busy >= busy_limit, "the processors are %.0f%% busy (limit %.0f%%)" % (busy, busy_limit)))
    if psi is not None:
        signals.append((psi >= psi_limit, "tasks waited for a processor %.0f%% of the last 10 s (limit %.0f%%)" % (psi, psi_limit)))
    if signals and all(flag for flag, _ in signals):
        return True, "the processors are saturated: " + " and ".join(text for _, text in signals)
    return False, ""


# [@ANCHOR: lint_lock:wait_for_stall]
def wait_for_stall(timeout, environ=None, busy=cpu_busy_percent, psi=cpu_stall, sleep=time.sleep, now=time.monotonic, out=print):
    """Block until the processors are not saturated (`saturated`). True when the run may start (also at once when HAMS_SKIP_CI_LOAD_GATE=1 or
    nothing can be measured), False after `timeout` seconds (it prints why). Polls every 15 s. Full entry: tools/README.md."""
    environ = os.environ if environ is None else environ
    if environ.get("HAMS_SKIP_CI_LOAD_GATE") == "1":
        out("[*] Skipping the lint load gate (HAMS_SKIP_CI_LOAD_GATE=1).")
        return True
    busy_limit, psi_limit = saturation_limits(environ)
    full, why = saturated(busy(), psi(), busy_limit, psi_limit)
    if not full:
        return True
    deadline = now() + timeout
    out("[*] %s: waiting up to %d s for it to ease (the same gate as test.py; HAMS_CI_LOAD_GATE_CPU_BUSY_PERCENT / _PSI_PERCENT change it, "
        "HAMS_SKIP_CI_LOAD_GATE=1 skips it)." % (why[0].upper() + why[1:], int(timeout)))
    while now() < deadline:
        sleep(min(POLL_SECONDS, max(0.0, deadline - now())))
        full, why = saturated(busy(), psi(), busy_limit, psi_limit)
        if not full:
            out("[*] The processors have spare time: starting.")
            return True
    out("ERROR: %s after %d s. Not starting a heavy lint run: its timing tests would fail for contention, not for code. Run again when the "
        "box is quieter, raise --load-timeout, or set HAMS_SKIP_CI_LOAD_GATE=1." % (why, int(timeout)))
    return False


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


# [@ANCHOR: lint_lock:gate]
def gate(environ, load_limit_override=None, load_timeout_override=None, out=print, stall=cpu_stall, measurable_busy=lambda: os.path.exists("/proc/stat")):
    """The load gate of a heavy lint run: the measured CPU saturation (`wait_for_stall`, test.py's rule) when it can be measured and nobody set a load
    limit explicitly (`--load-limit`, HAMS_LINT_LOAD_LIMIT, HAMS_CI_LOAD_GATE_RATIO), else the 1-minute load average against
    `load_limit`, as before. True when the run may start. Full entry: tools/README.md."""
    explicit = (load_limit_override is not None or environ.get("HAMS_LINT_LOAD_LIMIT") or environ.get("HAMS_CI_LOAD_GATE_RATIO"))
    timeout = load_timeout(environ, load_timeout_override)
    if not explicit and (stall() is not None or measurable_busy()):
        return wait_for_stall(timeout, environ=environ, psi=stall, out=out)
    return wait_for_load(load_limit(environ, override=load_limit_override), timeout, environ=environ, out=out)


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
        if not gate(environ, load_limit_override, load_timeout_override, out=out):
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
    if not gate(environ, load_limit_override, load_timeout_override, out=out):
        lock.release()
        return False, None
    return True, lock


if __name__ == "__main__":  # pragma: no cover
    print("\n".join(status_lines()))
    sys.exit(0)
