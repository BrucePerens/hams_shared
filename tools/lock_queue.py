#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
FIFO wait queue in front of test.py's box-wide single-instance lock.

test.py's lock (an exclusive, non-blocking flock on one fixed file) is fail-fast: a second
invocation exits at once with "Another instance of test.py is already running". Sessions that
want to run anyway polled `pgrep` and retried, which is a race: whoever re-ran first after the
holder exited won, a waiter starting in the same instant lost, and some starved for hours.

This module adds an OPT-IN queue (test.py --wait / HAMS_TEST_WAIT=1). It does NOT replace or
change the lock. The lock stays the one flock, taken exactly as before; the queue only decides
WHO is allowed to try for it next:

  * A waiter takes a ticket: a file named ``<seq:010d>-<pid>-<starttime>`` in the queue directory
    (default: the lock path plus ``.queue.d``, so a test lock path gives a test queue). The
    sequence number is made unique by creating the final name with link(2), which fails if it
    exists; the content is written to a temporary file first so a reader never sees a half-written
    ticket.
  * Only the lowest LIVE ticket may try the flock. A ticket is live while its pid exists, is not a
    zombie, and still has the recorded start time (so a recycled pid does not keep a dead waiter's
    place). Dead tickets are ignored, and unlinked when permissions allow.
  * Waiters poll every second or two (cheap: one directory listing and a few /proc reads), print
    a queue-position line periodically, and give up after an optional timeout.
  * The ticket is released on any way out of the wait: success paths aside, that is a timeout, an
    exception, SIGINT, SIGTERM and SIGHUP.
  * On success the ticket is KEPT. test.py re-execs itself (sudo, systemd-run, unshare) and the
    exec keeps the pid, so the ticket stays live for the whole run and the re-exec'd process adopts
    it through HAMS_TEST_QUEUE_TICKET instead of queueing behind its own parent.

Callers that do not pass --wait still fail fast and can still barge in ahead of the queue; that is
the point of keeping the default. Holder records (``holder-<pid>-<starttime>``) exist only so that
queue lines and ``--queue-status`` can say who holds the lock and what it is running.
"""

import json
import os
import signal
import sys
import time
import uuid

DEFAULT_POLL_SECONDS = 1.5
DEFAULT_REPORT_SECONDS = 60.0

ACQUIRED = "acquired"
TIMEOUT = "timeout"
GATE_FAILED = "gate_failed"

EXIT_WAIT_TIMEOUT = 75  # EX_TEMPFAIL, distinct from the fail-fast exit code 1.


# [@ANCHOR: lock_queue:queue_dir_for_lock]
def queue_dir_for_lock(lock_path, environ=None):
    """Queue directory for a lock file: HAMS_TEST_QUEUE_DIR, else ``<lock_path>.queue.d``."""
    environ = os.environ if environ is None else environ
    return environ.get("HAMS_TEST_QUEUE_DIR") or (lock_path + ".queue.d")


# [@ANCHOR: lock_queue:proc_start_time]
def proc_start_time(pid):
    """Start time (clock ticks since boot) of a live, non-zombie process, else None."""
    try:
        with open("/proc/%d/stat" % int(pid), "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
    except (OSError, ValueError):
        return None
    # The command name (field 2) may contain spaces and parentheses; the rest starts after the LAST ')'.
    rest = raw[raw.rfind(")") + 1:].split()
    # rest[0] is field 3 (state); starttime is field 22.
    if len(rest) < 20 or rest[0] in ("Z", "X"):
        return None
    try:
        return int(rest[19])
    except ValueError:
        return None


# [@ANCHOR: lock_queue:is_live]
def is_live(pid, start):
    """True if `pid` exists with exactly the recorded start time (guards against pid reuse)."""
    return proc_start_time(pid) == start


def _ensure_dir(qdir):
    if not os.path.isdir(qdir):
        os.makedirs(qdir, mode=0o1777, exist_ok=True)
    try:
        os.chmod(qdir, 0o1777)
    except OSError:
        pass  # not ours; the creator already set it


def _write_atomic(qdir, final_name, payload):
    """Create `final_name` with `payload` atomically; False if the name already exists."""
    tmp = os.path.join(qdir, ".tmp-%d-%s" % (os.getpid(), uuid.uuid4().hex))
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        try:
            os.link(tmp, os.path.join(qdir, final_name))
        except FileExistsError:
            return False
        return True
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _parse_name(name):
    """``<seq>-<pid>-<start>`` -> (seq, pid, start), or None."""
    parts = name.split("-")
    if len(parts) != 3:
        return None
    try:
        return int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None


def _read_payload(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _unlink_quiet(path):
    try:
        os.unlink(path)
    except OSError:
        pass  # already gone, or owned by another user in a sticky directory


# [@ANCHOR: lock_queue:take_ticket]
def take_ticket(qdir, info=None, pid=None, start=None, now=time.time):
    """Join the queue; returns the ticket name. Numbers only ever increase past the highest
    ticket present, and two concurrent callers can never receive the same number."""
    pid = os.getpid() if pid is None else pid
    start = proc_start_time(pid) if start is None else start
    if start is None:
        raise RuntimeError("cannot read the start time of pid %d" % pid)
    _ensure_dir(qdir)
    payload = {"pid": pid, "start": start, "queued_at": now(), "info": info or ""}
    while True:
        highest = 0
        for name in os.listdir(qdir):
            parsed = _parse_name(name)
            if parsed:
                highest = max(highest, parsed[0])
        name = "%010d-%d-%d" % (highest + 1, pid, start)
        if _write_atomic(qdir, name, payload):
            return name


# [@ANCHOR: lock_queue:live_tickets]
def live_tickets(qdir, clean=True):
    """Live tickets, lowest (earliest) first, as dicts with name/seq/pid/start/queued_at/info.
    Dead ones are skipped and, when `clean`, unlinked."""
    try:
        names = os.listdir(qdir)
    except OSError:
        return []
    found = []
    for name in names:
        if name.startswith((".", "holder")):
            continue
        parsed = _parse_name(name)
        if not parsed:
            continue
        seq, pid, start = parsed
        if is_live(pid, start):
            payload = _read_payload(os.path.join(qdir, name))
            found.append({
                "name": name, "seq": seq, "pid": pid, "start": start,
                "queued_at": payload.get("queued_at"), "info": payload.get("info", ""),
            })
        elif clean:
            _unlink_quiet(os.path.join(qdir, name))
    found.sort(key=lambda t: t["seq"])
    return found


# [@ANCHOR: lock_queue:release_ticket]
def release_ticket(qdir, name):
    """Leave the queue. Safe to call twice, or for a ticket that is already gone."""
    if name:
        _unlink_quiet(os.path.join(qdir, name))


# [@ANCHOR: lock_queue:write_holder]
def write_holder(qdir, info, pid=None, now=time.time):
    """Record that this process holds the lock; returns the record name (or None on failure,
    which must never stop a test run)."""
    pid = os.getpid() if pid is None else pid
    start = proc_start_time(pid)
    if start is None:
        return None
    try:
        _ensure_dir(qdir)
        name = "holder-%d-%d" % (pid, start)
        path = os.path.join(qdir, name)
        _unlink_quiet(path)
        _write_atomic(qdir, name, {"pid": pid, "start": start, "since": now(), "info": info})
        return name
    except OSError:
        return None


# [@ANCHOR: lock_queue:read_holder]
def read_holder(qdir):
    """Newest live holder record as a dict (pid/start/since/info), or None."""
    best = None
    try:
        names = os.listdir(qdir)
    except OSError:
        return None
    for name in names:
        if not name.startswith("holder-"):
            continue
        bits = name.split("-")
        try:
            pid, start = int(bits[1]), int(bits[2])
        except (IndexError, ValueError):
            pid = start = None
        if pid is None or not is_live(pid, start):
            _unlink_quiet(os.path.join(qdir, name))
            continue
        payload = _read_payload(os.path.join(qdir, name))
        payload.setdefault("pid", pid)
        payload.setdefault("start", start)
        if best is None or (payload.get("since") or 0) > (best.get("since") or 0):
            best = payload
    return best


# [@ANCHOR: lock_queue:format_duration]
def format_duration(seconds):
    """'45 s', '12 min', '2 h 05 min'."""
    seconds = max(0, int(seconds))
    if seconds < 90:
        return "%d s" % seconds
    minutes = seconds // 60
    if minutes < 120:
        return "%d min" % minutes
    return "%d h %02d min" % (minutes // 60, minutes % 60)


# [@ANCHOR: lock_queue:describe_queue]
def describe_queue(qdir, my_ticket=None, now=time.time):
    """Human-readable status lines: the holder, then each live ticket in order. Read-only apart
    from dead-record cleanup."""
    lines = []
    holder = read_holder(qdir)
    if holder:
        lines.append("holder: pid %s running %s for %s" % (
            holder.get("pid"), holder.get("info") or "?",
            format_duration(_age(holder, now))))
    else:
        lines.append("holder: none recorded")
    tickets = live_tickets(qdir)
    if not tickets:
        lines.append("queue: empty")
    for i, t in enumerate(tickets, 1):
        waited = format_duration(now() - t["queued_at"]) if t.get("queued_at") else "?"
        lines.append("queue %d: pid %d %s waiting %s%s" % (
            i, t["pid"], t["info"] or "?", waited, "  <- you" if t["name"] == my_ticket else ""))
    return lines


def _age(holder, now):
    since = holder.get("since")
    return 0 if since is None else now() - since


def _queue_line(qdir, ticket, now):
    tickets = live_tickets(qdir, clean=False)
    names = [t["name"] for t in tickets]
    position = names.index(ticket) + 1 if ticket in names else len(names) + 1
    holder = read_holder(qdir)
    if holder:
        who = "holder pid %s running %s for %s" % (
            holder.get("pid"), holder.get("info") or "?",
            format_duration(_age(holder, now)))
    else:
        who = "holder unknown"
    return "queued: position %d of %d, %s" % (position, max(len(names), position), who)


class _Interrupted(BaseException):
    pass


# [@ANCHOR: lock_queue:acquire]
def acquire(qdir, try_lock, info="", ticket=None, timeout=None, poll=DEFAULT_POLL_SECONDS,
            report_every=DEFAULT_REPORT_SECONDS, on_head=None, sleep=time.sleep,
            now=time.time, out=print, handle_signals=True):
    """Wait in line, then take the lock.

    `try_lock()` makes ONE non-blocking attempt at the real lock and returns True on success; it
    is only called while this waiter holds the lowest live ticket. `ticket` adopts a ticket that
    was already taken (a re-exec'd child of the process that queued); `on_head()` runs once when
    the waiter first reaches the front, before the lock is tried, and returns False to give up.

    Returns (status, ticket): status is ACQUIRED (ticket kept, see the module docstring), TIMEOUT
    or GATE_FAILED; the ticket is released for the latter two and on any exception or signal."""
    adopted = bool(ticket) and any(t["name"] == ticket for t in live_tickets(qdir, clean=False))
    if not adopted:
        ticket = take_ticket(qdir, info=info, now=now)
    old_handlers = {}
    if handle_signals:
        def _raise(signum, _frame):
            raise _Interrupted(signum)
        for sig in (signal.SIGTERM, signal.SIGHUP):
            try:
                old_handlers[sig] = signal.signal(sig, _raise)
            except (ValueError, OSError):
                pass  # not the main thread
    started = now()
    next_report = started
    gated = adopted or on_head is None
    done = False
    try:
        while True:
            tickets = live_tickets(qdir)
            if not any(t["name"] == ticket for t in tickets):
                ticket = take_ticket(qdir, info=info, now=now)  # lost our file: rejoin at the back
                continue
            if tickets[0]["name"] == ticket:
                if not gated:
                    if not on_head():
                        return GATE_FAILED, ticket
                    gated = True
                if try_lock():
                    done = True
                    return ACQUIRED, ticket
            if now() >= next_report:
                out("[*] " + _queue_line(qdir, ticket, now))
                next_report = now() + report_every
            if timeout is not None and now() - started >= timeout:
                out("[*] gave up waiting for the test lock after %s." % format_duration(timeout))
                return TIMEOUT, ticket
            sleep(poll)
    except _Interrupted as e:
        sig = e.args[0] if e.args else signal.SIGTERM
        raise SystemExit(128 + int(sig))
    finally:
        if not done:
            release_ticket(qdir, ticket)
        for sig, handler in old_handlers.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass


# [@ANCHOR: lock_queue:wait_options_from_argv]
def wait_options_from_argv(argv, environ=None):
    """Parse the queue flags out of argv without disturbing the rest.

    Returns dict(wait, timeout, status). --wait / HAMS_TEST_WAIT=1 queue; --wait-timeout SECONDS
    (or HAMS_TEST_WAIT_TIMEOUT) bounds the wait; --queue-status prints holder and queue."""
    import argparse
    environ = os.environ if environ is None else environ
    p = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    p.add_argument("--wait", action="store_true")
    p.add_argument("--wait-timeout", type=float, default=None)
    p.add_argument("--queue-status", action="store_true")
    ns, _ = p.parse_known_args(argv)
    wait = ns.wait or environ.get("HAMS_TEST_WAIT") == "1"
    timeout = ns.wait_timeout
    if timeout is None and environ.get("HAMS_TEST_WAIT_TIMEOUT"):
        try:
            timeout = float(environ["HAMS_TEST_WAIT_TIMEOUT"])
        except ValueError:
            timeout = None
    return {"wait": wait or timeout is not None, "timeout": timeout, "status": ns.queue_status}


if __name__ == "__main__":  # pragma: no cover
    print("\n".join(describe_queue(queue_dir_for_lock(os.environ.get(
        "HAMS_TEST_LOCK_PATH") or "/tmp/hams_odoo_test_runner.lock"))), file=sys.stdout)
