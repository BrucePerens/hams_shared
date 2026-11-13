#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Event-driven Chrome teardown for Odoo browser tests (HttpCase browser_js / start_tour).

Why this exists: Odoo's `ChromeBrowser._chrome_start` ends with `proc.terminate()` followed by
`psutil.wait_procs(<chrome and its descendants>, 5)`. On the test box nearly every Chrome still had a
descendant alive when that call began, and the call then sat out its full 5 s (117 waits, about 585 s
summed, in one full-system run; see tmp/reports/test-speedup.md). The wait is a poll with a fixed budget
and nothing makes it return early except every process being gone, so the cost is whatever the slowest
straggler takes, rounded up by psutil's own polling interval.

What this does instead, without editing the vendored Odoo: `install()` makes `odoo.tests.common` start
Chrome in its OWN session (`start_new_session=True`, so Chrome is the leader of a process group the
harness owns) and returns a Popen whose `terminate()` is the wait below. Odoo's own `wait_procs` that
follows then finds everything already gone and returns at once.

terminate_group(): SIGTERM the group; wait for the leader with a pidfd (an event, no polling); reap it;
then wait for the rest of the group to empty by checking `killpg(pgid, 0)` every few milliseconds; if the
group is still not empty after `cap` seconds, SIGKILL the group and LOG the survivors (name, pid, state,
command line) at WARNING: a Chrome descendant that ignores SIGTERM is a finding, not noise. Descendants
that leave the group (setsid themselves) are not covered; the survivors check is by process group, and
Odoo's own wait_procs afterwards still covers them as before.
"""
import logging
import os
import select
import subprocess
import time
import types

import psutil

_logger = logging.getLogger("hams.chrome_teardown")

DEFAULT_CAP_SECONDS = 10.0
DEFAULT_POLL_SECONDS = 0.01


def _group_alive(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _program(cmdline):
    """The program name of a command line (or the `[name]` of a process without one): its arguments can hold a secret, so the log never carries them."""
    if cmdline.startswith("["):
        return cmdline
    first = cmdline.split(" ", 1)[0]
    return os.path.basename(first) or first


def group_members(pgid):
    """[(pid, state, cmdline)] of every process currently in process group `pgid` (from /proc)."""
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", "rb") as handle:
                stat = handle.read()
            # comm may hold spaces and parens; the fields after the LAST ')' are fixed.
            rest = stat[stat.rindex(b")") + 2:].split()
            state, group = rest[0].decode(), int(rest[2])
            if group != pgid:
                continue
            with open(f"/proc/{entry}/cmdline", "rb") as handle:
                cmd = handle.read().replace(b"\0", b" ").decode(errors="replace").strip()
            if not cmd:
                with open(f"/proc/{entry}/comm", encoding="utf-8", errors="replace") as handle:
                    cmd = f"[{handle.read().strip()}]"
        except (OSError, ValueError, IndexError):
            continue
        found.append((int(entry), state, cmd[:200]))
    return found


def _signal_group(pgid, sig):
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


def _snapshot(proc):
    """The leader and all its current descendants as psutil.Process objects (pid + create time, so a reused
    pid number is never mistaken for the original)."""
    try:
        leader = psutil.Process(proc.pid)
        return [leader] + leader.children(recursive=True)
    except psutil.NoSuchProcess:
        return []


def _alive(p):
    try:
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _signal_pid(p, sig):
    try:
        p.send_signal(sig)
    except psutil.NoSuchProcess:
        pass


def terminate_group(proc, cap=DEFAULT_CAP_SECONDS, poll=DEFAULT_POLL_SECONDS, logger=_logger):
    """SIGTERM proc, its process group and every descendant it has right now, then wait (event driven) for
    all of them to be gone.

    Descendants are tracked individually as well as by group because the test box starts Chrome as
    `setsid -w unshare ... chrome`: `setsid` forks when it is a group leader, so the real browser tree is
    in a DIFFERENT group than the Popen leader. `proc` should still be started with start_new_session=True,
    so signalling its group cannot touch the harness.

    Returns the list of (pid, state, cmdline) that had to be killed after `cap` seconds (empty when
    everything exited by itself)."""
    pgid = proc.pid
    start = time.monotonic()
    deadline = start + cap
    tree = _snapshot(proc)
    if proc.poll() is None:
        _signal_group(pgid, 15)
        for p in tree:
            _signal_pid(p, 15)
        pidfd = os.pidfd_open(proc.pid)
        try:
            poller = select.poll()
            poller.register(pidfd, select.POLLIN)
            poller.poll(max(0.0, deadline - time.monotonic()) * 1000)
        finally:
            os.close(pidfd)
        # Reap the leader (a zombie would keep the group "alive" for killpg) unless it ignored the term
        # signal for the whole cap: it is then handled, with everything else, by the loop below.
        if proc.poll() is None and time.monotonic() < deadline:
            proc.wait()
    survivors = []
    while True:
        alive = [p for p in tree if _alive(p)]
        if not alive and not _group_alive(pgid):
            break
        if time.monotonic() >= deadline:
            for p in alive:
                try:
                    survivors.append((p.pid, p.status(), _program(" ".join(p.cmdline())) or f"[{p.name()}]"))
                except psutil.NoSuchProcess:
                    pass
            survivors += [(pid, state, _program(cmd)) for pid, state, cmd in group_members(pgid) if pid not in {s[0] for s in survivors}]
            logger.warning(
                "chrome tree of %s: %d process(es) still alive %.1fs after the term signal, killing:%s",
                pgid, len(survivors), cap,
                "".join(f"\n- pid {p} ({state}) {name}" for p, state, name in survivors),
            )
            _signal_group(pgid, 9)
            for p in alive:
                _signal_pid(p, 9)
            break
        time.sleep(poll)
    try:
        proc.wait(5)  # reap the leader, else its zombie keeps the group "alive"
    except subprocess.TimeoutExpired:
        logger.warning("chrome leader %s did not exit after the final kill", pgid)
    if survivors:
        kill_deadline = time.monotonic() + 5.0
        while time.monotonic() < kill_deadline and (any(_alive(p) for p in tree) or _group_alive(pgid)):
            time.sleep(poll)
    logger.debug("chrome tree of %s torn down in %.3fs", pgid, time.monotonic() - start)
    return survivors


# [@ANCHOR: chrome_teardown:chrome_teardown_group_popen_class]
class GroupPopen(subprocess.Popen):
    """Popen whose terminate() takes the whole process group down and waits for it (see module doc)."""

    teardown_cap = DEFAULT_CAP_SECONDS

    def terminate(self):
        terminate_group(self, cap=self.teardown_cap)


def install(common_module):
    """Make `common_module.ChromeBrowser` start Chrome in its own session with GroupPopen. Idempotent."""
    if getattr(common_module, "_hams_chrome_teardown", False):
        return
    real_subprocess = common_module.subprocess

    def popen(*args, **kwargs):
        kwargs.setdefault("start_new_session", True)
        return GroupPopen(*args, **kwargs)

    proxy = types.SimpleNamespace(**{k: getattr(real_subprocess, k) for k in dir(real_subprocess)
                                     if not k.startswith("__")})
    proxy.Popen = popen
    common_module.subprocess = proxy
    common_module._hams_chrome_teardown = True
