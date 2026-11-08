#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Coalescing batch queue for the shared lint and test runs on the dev box (ADR 0115).

Many agents ask for a lint or test run at the same time. One run at a time is the rule on this box, so
requests used to queue one by one, each paying the whole run. This module makes requests that arrive
WHILE a run is in progress join the NEXT run, which then covers all of their branches at once:

  * Per machine, per repository and per kind ("lint", "test") there is at most one RUNNING batch and
    one FORMING batch, kept in one JSON state file guarded by an flock (`locked`).
  * A request with nothing running seals a batch of its own and runs at once (no added latency).
  * A request that arrives while a batch runs joins the forming batch (`join`); when the running batch
    ends, the OLDEST live member of the forming batch becomes the leader, seals it (`seal`) and runs it.
  * A batch is capped by number of branches and by an estimated duration (`join` returns None when
    the forming batch is full; the caller waits and tries again).
  * The leader builds ONE combined tree (a temporary git worktree at the base with every branch merged
    in, `build_combined_tree`), runs the real linter or test command once over it and settles the result
    per branch (`settle`): attribute failures to the branches that own the named files or modules, mark
    the rest passed at once, and split the batch in halves when a failure cannot be attributed.
  * Every caller blocks for ITS OWN answer (`coalesce` returns its verdict, exit code and log path), so
    callers (prmerge.sh, agents' commands) need no change.
  * Robustness: every member and the runner are recorded with pid and process start time
    (`lock_queue.is_live`), a dead waiter is dropped when the batch seals, a dead leader releases the
    running slot and its members re-queue themselves (`coalesce`), and the verdict file is written
    BEFORE the running slot is cleared so nobody sees a gap.

The queue does not replace the machine lock (lint_lock.RunLock, test.py's flock): the combined run takes
them exactly as a single run does. It decides only WHO runs next and WHAT that run covers.
"""

import contextlib
import fcntl
import json
import math
import os
import re
import shutil
import signal
import subprocess
import time
import uuid

import lock_queue

STATE_VERSION = 1
DEFAULT_MAX_BRANCHES = 8
DEFAULT_MAX_SECONDS = 1800.0
DEFAULT_POLL_SECONDS = 1.0
DEFAULT_REPORT_SECONDS = 60.0
MAX_REQUEUES = 3
MAX_SETTLE_RUNS = 9
VERDICT_KEEP_SECONDS = 86400.0

PASSED = "passed"
FAILED = "failed"
SOLO = "solo"
TIMEOUT = "timeout"
GAVE_UP = "gave_up"


# [@ANCHOR: batch_queue:Member]
# One waiting request: {"pid", "start", "branch", "tip", "scope": [str], "queued_at", "solo", "info"} and, only when the
# caller asked for any, "install": [str] (modules a test run installs without testing them; an old member has no such key).
# See docs/runbooks/LINT_TEST_BATCHING.md for the field table.
def make_member(branch, tip, scope=(), solo=False, info="", pid=None, start=None, now=time.time, install=()):
    """Build a `Member` record for the calling process (or for `pid`/`start`, used by tests)."""
    pid = os.getpid() if pid is None else pid
    start = lock_queue.proc_start_time(pid) if start is None else start
    member = {"pid": pid, "start": start, "branch": branch, "tip": tip, "scope": sorted(set(scope)),
              "queued_at": now(), "solo": bool(solo), "info": info}
    if install:  # absent when empty, so a state file written here stays readable by a leader that predates the field
        member["install"] = sorted(set(install) - set(scope))
    return member


# [@ANCHOR: batch_queue:BatchState]
# The state file: {"version", "next_id", "forming": Batch|None, "running": Batch|None}.
# A forming Batch is {"id", "members", "solo", "created"}; a running one adds {"runner": {"pid","start"},
# "started", "log"}. See docs/runbooks/LINT_TEST_BATCHING.md.
def empty_state():
    """A state with nothing forming and nothing running."""
    return {"version": STATE_VERSION, "next_id": 1, "forming": None, "running": None}


def _cache_base(environ):
    return environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")


# [@ANCHOR: batch_queue:repo_key]
def repo_key(repo_root):
    """Identity of the repository behind a checkout or worktree: the real path of the main checkout
    (the parent of `git rev-parse --git-common-dir`), so every worktree of one repository shares one
    queue and two repositories never do. Falls back to the real path of `repo_root`."""
    try:
        out = subprocess.run(["git", "-C", repo_root, "rev-parse", "--git-common-dir"], capture_output=True,
                             text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        return os.path.realpath(repo_root)
    common = out if os.path.isabs(out) else os.path.join(repo_root, out)
    return os.path.realpath(os.path.dirname(os.path.normpath(common)))


# [@ANCHOR: batch_queue:state_dir]
def state_dir(kind, key, environ=None):
    """Directory of the queue for (`kind`, repository `key`): HAMS_BATCH_DIR or
    ``~/.cache/hams-lint/batches``, then ``<kind>-<sanitised key>``."""
    environ = os.environ if environ is None else environ
    base = environ.get("HAMS_BATCH_DIR") or os.path.join(_cache_base(environ), "hams-lint", "batches")
    return os.path.join(base, "%s-%s" % (kind, re.sub(r"[^A-Za-z0-9_.-]+", "_", key).strip("_")))


def _state_path(sdir):
    return os.path.join(sdir, "state.json")


# [@ANCHOR: batch_queue:locked]
@contextlib.contextmanager
def locked(sdir):
    """Exclusive flock on the queue, yielding the loaded state; the state is saved (atomically) when
    the block ends without an exception. Critical sections are short: no waiting or running inside."""
    os.makedirs(sdir, exist_ok=True)
    fd = os.open(os.path.join(sdir, "state.lock"), os.O_CREAT | os.O_RDWR, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            with open(_state_path(sdir), encoding="utf-8") as handle:
                state = json.load(handle)
            if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
                state = empty_state()
        except (OSError, ValueError):
            state = empty_state()
        before = json.dumps(state, sort_keys=True)
        yield state
        if json.dumps(state, sort_keys=True) == before and os.path.exists(_state_path(sdir)):
            return  # nothing changed: a poll must not rewrite a large file every second
        tmp = _state_path(sdir) + ".tmp-%d-%s" % (os.getpid(), uuid.uuid4().hex)
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        os.replace(tmp, _state_path(sdir))
    finally:
        os.close(fd)


def _alive(member, live):
    return live(member["pid"], member["start"])


# [@ANCHOR: batch_queue:prune]
def prune(state, live=lock_queue.is_live):
    """Drop dead waiters from the forming batch (an emptied batch disappears) and free the running slot
    when its runner is dead. Returns the events, e.g. ``[("runner_died", 7)]``. Mutates `state`."""
    events = []
    forming = state.get("forming")
    if forming:
        forming["members"] = [m for m in forming["members"] if _alive(m, live)]
        if not forming["members"]:
            state["forming"] = None
    running = state.get("running")
    if running and not live(running["runner"]["pid"], running["runner"]["start"]):
        events.append(("runner_died", running["id"]))
        state["running"] = None
    return events


# [@ANCHOR: batch_queue:caps_from_env]
def caps_from_env(environ=None):
    """Batch caps: {"max_branches": int >= 1, "max_seconds": float >= 0 (0 = no duration cap)} from
    HAMS_BATCH_MAX_BRANCHES (default 8) and HAMS_BATCH_MAX_SECONDS (default 1800). Bad values fall back."""
    environ = os.environ if environ is None else environ
    try:
        branches = max(1, int(environ.get("HAMS_BATCH_MAX_BRANCHES", DEFAULT_MAX_BRANCHES)))
    except ValueError:
        branches = DEFAULT_MAX_BRANCHES
    try:
        seconds = float(environ.get("HAMS_BATCH_MAX_SECONDS", DEFAULT_MAX_SECONDS))
    except ValueError:
        seconds = DEFAULT_MAX_SECONDS
    if not math.isfinite(seconds) or seconds < 0:
        seconds = DEFAULT_MAX_SECONDS
    return {"max_branches": branches, "max_seconds": seconds}


# [@ANCHOR: batch_queue:join]
def join(state, member, caps, now=time.time, estimate=None):
    """Put `member` into the forming batch (creating it when there is none) and return the batch id,
    or None when the batch is full or solo-only and the caller must wait and try again. A second request
    with the same tip joins the same slot (one branch, however many callers). `estimate(scopes)` takes
    the union of member scopes and returns estimated seconds (or None); a batch whose estimate would
    exceed caps["max_seconds"] accepts no further branch (the first branch is always accepted)."""
    forming = state.get("forming")
    if forming is not None and any((m["pid"], m["start"]) == (member["pid"], member["start"])
                                   for m in forming["members"]):
        return forming["id"]  # already in: joining twice must not duplicate the member
    if forming is None:
        forming = {"id": state["next_id"], "members": [], "solo": bool(member.get("solo")), "created": now()}
        state["next_id"] += 1
        state["forming"] = forming
    elif forming.get("solo") or member.get("solo"):
        return None
    tips = {m["tip"] for m in forming["members"]}
    if forming["members"] and member["tip"] not in tips:
        if len(tips) >= caps["max_branches"]:
            return None
        if estimate is not None and caps.get("max_seconds"):
            scope = set(member.get("scope", ()))
            for other in forming["members"]:
                scope |= set(other.get("scope", ()))
            seconds = estimate(sorted(scope))
            if seconds is not None and seconds > caps["max_seconds"]:
                return None
    forming["members"].append(member)
    return forming["id"]


# [@ANCHOR: batch_queue:leave]
def leave(state, member):
    """Remove `member` (same pid and start) from the forming batch, e.g. after a timeout."""
    forming = state.get("forming")
    if forming:
        forming["members"] = [m for m in forming["members"]
                              if (m["pid"], m["start"]) != (member["pid"], member["start"])]
        if not forming["members"]:
            state["forming"] = None


# [@ANCHOR: batch_queue:seal]
def seal(state, member, now=time.time, log=""):
    """If `member` is the leader (the oldest live member of the forming batch) and nothing is running,
    move the forming batch to running with `member` as its runner and return it; else None. Call
    `prune` first so a dead oldest member does not block the batch."""
    forming = state.get("forming")
    if not forming or state.get("running") or not forming["members"]:
        return None
    lead = forming["members"][0]
    if (lead["pid"], lead["start"]) != (member["pid"], member["start"]):
        return None
    running = {"id": forming["id"], "members": forming["members"], "solo": forming.get("solo", False),
               "runner": {"pid": member["pid"], "start": member["start"]}, "started": now(), "log": log}
    state["forming"] = None
    state["running"] = running
    return running


# [@ANCHOR: batch_queue:finish]
def finish(state, batch_id):
    """Free the running slot when it still holds batch `batch_id`."""
    running = state.get("running")
    if running and running["id"] == batch_id:
        state["running"] = None


def _verdict_path(sdir, batch_id):
    return os.path.join(sdir, "verdicts", "%d.json" % batch_id)


# [@ANCHOR: batch_queue:write_verdicts]
def write_verdicts(sdir, batch_id, doc, now=time.time):
    """Publish the result of batch `batch_id` atomically (tmp file + rename) and delete verdict files
    older than a day. `doc` is {"verdicts": {tip: Verdict}, "log": path, "members": [...]}."""
    vdir = os.path.join(sdir, "verdicts")
    os.makedirs(vdir, exist_ok=True)
    doc = dict(doc, batch=batch_id, finished=now())
    tmp = os.path.join(vdir, ".tmp-%d-%s" % (os.getpid(), uuid.uuid4().hex))
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(doc, handle)
    os.replace(tmp, _verdict_path(sdir, batch_id))
    for name in os.listdir(vdir):
        path = os.path.join(vdir, name)
        try:
            if name.endswith(".json") and now() - os.path.getmtime(path) > VERDICT_KEEP_SECONDS:
                os.unlink(path)
        except OSError:
            pass


# [@ANCHOR: batch_queue:read_verdicts]
def read_verdicts(sdir, batch_id):
    """The published result of batch `batch_id`, or None while it is not (or never will be) written."""
    try:
        with open(_verdict_path(sdir, batch_id), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


class _Interrupted(BaseException):
    pass


def _describe_running(running, now):
    started = running.get("started")
    return "batch %d running for %s (%d branch%s)" % (
        running["id"], lock_queue.format_duration(now() - started) if started else "?",
        len({m["tip"] for m in running["members"]}), "" if len({m["tip"] for m in running["members"]}) == 1 else "es")


# [@ANCHOR: batch_queue:coalesce]
def coalesce(sdir, member, run_batch, caps=None, estimate=None, poll=DEFAULT_POLL_SECONDS,
             report_every=DEFAULT_REPORT_SECONDS, timeout=None, out=print, now=time.time,
             sleep=time.sleep, live=lock_queue.is_live, handle_signals=True, log_for=None):
    """Join (or start) a batch for `member` and block until this member's verdict is known.

    `run_batch(batch_id, members, log_path)` is called ONLY in the process that becomes leader and returns
    {"verdicts": {tip: Verdict}, "log": path}; it does the actual work for the whole batch. Everyone
    else polls the verdict file. Returns {"status": PASSED|FAILED|SOLO|TIMEOUT|GAVE_UP, "verdict": the
    member's Verdict or None, "batch": id, "others": branches that shared the batch, "ran": True when this
    process was the leader, "doc": the whole verdict document or None}. A batch whose runner died without a verdict is
    abandoned: the member joins again, at most MAX_REQUEUES times, then GAVE_UP (the caller runs alone).
    TIMEOUT: `timeout` seconds passed while waiting. A SIGTERM/SIGHUP while waiting leaves the queue
    and exits 128+signal. Prints "joined batch N with K other branches" once per join."""
    caps = caps or caps_from_env()
    old_handlers = {}
    if handle_signals:
        def _raise(signum, _frame):
            raise _Interrupted(signum)
        for sig in (signal.SIGTERM, signal.SIGHUP):
            try:
                old_handlers[sig] = signal.signal(sig, _raise)
            except (ValueError, OSError):
                pass
    started = now()
    next_report = started
    batch_id = None
    requeues = 0
    announced_wait = False
    runner_of = None
    try:
        while True:
            lead_run = None
            refused = False
            with locked(sdir) as state:
                for event, ident in prune(state, live):
                    if event == "runner_died":
                        out("[*] batch %d: its runner died; waiting members re-queue" % ident)
                if batch_id is None:
                    batch_id = join(state, member, caps, now=now, estimate=estimate)
                    refused = batch_id is None
                    if batch_id is not None:
                        forming = state["forming"]
                        others = sorted({m["branch"] for m in forming["members"] if m["tip"] != member["tip"]})
                        running = state.get("running")
                        if running is None and forming["members"][0]["tip"] == member["tip"] and not others:
                            out("[*] batch %d: nothing is running here; starting at once" % batch_id)
                        else:
                            out("[*] joined batch %d with %d other branch%s%s" % (
                                batch_id, len(others), "" if len(others) == 1 else "es",
                                " (" + _describe_running(running, now) + ")" if running else ""))
                if batch_id is not None:
                    doc = read_verdicts(sdir, batch_id)
                    if doc is not None and not any((m["pid"], m["start"]) == (member["pid"], member["start"])
                                                   for m in doc.get("members", [])):
                        doc = None  # a stale file from an older state: not ours
                    if doc is not None:
                        verdict = (doc.get("verdicts") or {}).get(member["tip"])
                        if verdict is not None:
                            others = sorted({m["branch"] for m in doc.get("members", []) if m["tip"] != member["tip"]})
                            status = verdict.get("status")
                            return {"status": status if status in (PASSED, FAILED, SOLO) else FAILED,
                                    "verdict": verdict, "batch": batch_id, "others": others, "ran": False,
                                    "doc": doc}
                        batch_id = None  # a verdict without us: join again
                    else:
                        forming, running = state.get("forming"), state.get("running")
                        runner_of = running
                        if forming and forming["id"] == batch_id:
                            lead_run = seal(state, member, now=now,
                                            log=log_for(batch_id) if log_for else "")
                        elif running and running["id"] == batch_id:
                            pass
                        else:
                            requeues += 1
                            out("[*] batch %d ended without a verdict (its runner died); joining again" % batch_id)
                            batch_id = None
                            if requeues > MAX_REQUEUES:
                                return {"status": GAVE_UP, "verdict": None, "batch": None, "others": [],
                                        "ran": False, "doc": None}
                            continue
            if lead_run is not None:
                members = lead_run["members"]
                others = sorted({m["branch"] for m in members if m["tip"] != member["tip"]})
                out("[*] batch %d starts with %d branch%s: %s" % (
                    lead_run["id"], len({m["tip"] for m in members}),
                    "" if len({m["tip"] for m in members}) == 1 else "es",
                    ", ".join(sorted({m["branch"] for m in members}))))
                doc = None
                try:
                    doc = run_batch(lead_run["id"], members, lead_run.get("log", ""))
                finally:
                    with locked(sdir) as state:
                        if doc is not None:
                            doc = dict(doc, members=members, batch=lead_run["id"])
                            write_verdicts(sdir, lead_run["id"], doc, now=now)
                        finish(state, lead_run["id"])
                verdict = (doc.get("verdicts") or {}).get(member["tip"]) if doc else None
                status = (verdict or {}).get("status")
                return {"status": status if status in (PASSED, FAILED, SOLO) else FAILED, "verdict": verdict,
                        "batch": lead_run["id"], "others": others, "ran": True, "doc": doc}
            if now() >= next_report:
                if batch_id is not None and runner_of is not None:
                    if runner_of["id"] == batch_id:
                        out("[*] waiting for %s" % _describe_running(runner_of, now))
                    else:
                        out("[*] batch %d is forming behind %s" % (batch_id, _describe_running(runner_of, now)))
                elif refused and not announced_wait:
                    out("[*] the forming batch is full (%d branches): waiting for the next one" % caps["max_branches"])
                    announced_wait = True
                next_report = now() + report_every
            if timeout is not None and now() - started >= timeout:
                with locked(sdir) as state:
                    leave(state, member)
                return {"status": TIMEOUT, "verdict": None, "batch": batch_id, "others": [], "ran": False,
                        "doc": None}
            sleep(poll)
    except _Interrupted as interrupted:
        try:
            with locked(sdir) as state:
                leave(state, member)
        except OSError:
            pass
        raise SystemExit(128 + int(interrupted.args[0]))
    finally:
        for sig, handler in old_handlers.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass


# ---- combined tree -------------------------------------------------------------------------------

GIT_IDENTITY = ["-c", "user.name=hams-batch", "-c", "user.email=batch@localhost", "-c", "commit.gpgsign=false"]


def _git(repo, *args, run=subprocess.run):
    return run(["git", *GIT_IDENTITY, "-C", repo, *args], capture_output=True, text=True)


# [@ANCHOR: batch_queue:tree_parent]
def tree_parent(kind, repo_name, pid=None, start=None, suffix="", environ=None):
    """Directory that will hold one combined tree: ``<HAMS_BATCH_TREE_DIR or ~/workspace/tmp>/
    batch_<kind>_<repo>_<pid>-<start><suffix>``. It holds nothing else, so the linters' "sibling
    checkout beside the repository" lookup finds no stray neighbour. The tree is `<parent>/<repo_name>`."""
    environ = os.environ if environ is None else environ
    pid = os.getpid() if pid is None else pid
    start = lock_queue.proc_start_time(pid) if start is None else start
    base = environ.get("HAMS_BATCH_TREE_DIR") or os.path.join(os.path.expanduser("~"), "workspace", "tmp")
    return os.path.join(base, "batch_%s_%s_%d-%s%s" % (kind, repo_name, pid, start, suffix))


# [@ANCHOR: batch_queue:build_combined_tree]
def build_combined_tree(repo, base, members, dest, run=subprocess.run):
    """Create a detached git worktree of `repo` at `dest` on `base` and merge each member's tip into it,
    in order (no fast-forward, so the result is one commit on top of `base` per branch). A member whose
    merge conflicts is aborted and dropped. Returns {"merged": [members], "conflicted": [(member, text)],
    "head": sha or None}; "head" is None when the worktree could not be created (then "error" says why).
    Side effects: a new worktree registered in `repo`'s git metadata (remove it with `remove_tree`)."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    added = _git(repo, "worktree", "add", "--detach", dest, base, run=run)
    if added.returncode != 0:
        return {"merged": [], "conflicted": [], "head": None, "error": (added.stderr or added.stdout).strip()}
    merged, conflicted, seen = [], [], set()
    for member in members:
        if member["tip"] in seen:
            merged.append(member)
            continue
        seen.add(member["tip"])
        result = _git(dest, "merge", "--no-ff", "--no-edit", "-m", "batch: merge %s" % member["branch"],
                      member["tip"], run=run)
        if result.returncode == 0:
            merged.append(member)
        else:
            conflicted.append((member, (result.stdout + result.stderr).strip()[-400:]))
            _git(dest, "merge", "--abort", run=run)
            _git(dest, "reset", "--hard", "-q", "HEAD", run=run)
    head = _git(dest, "rev-parse", "HEAD", run=run).stdout.strip() or None
    return {"merged": merged, "conflicted": conflicted, "head": head}


# [@ANCHOR: batch_queue:remove_tree]
def remove_tree(repo, dest, run=subprocess.run):
    """Remove a combined tree and its parent directory, and prune git's worktree list. Safe to call twice.
    Files a test run left owned by another user are removed with a non-interactive `sudo -n rm -rf`."""
    _git(repo, "worktree", "remove", "--force", dest, run=run)
    shutil.rmtree(os.path.dirname(dest), ignore_errors=True)
    if os.path.exists(os.path.dirname(dest)):
        # A test run as the odoo user leaves files this user cannot delete: retry with sudo, never leave the tree.
        run(["sudo", "-n", "rm", "-rf", os.path.dirname(dest)], capture_output=True, text=True)
    _git(repo, "worktree", "prune", run=run)


# [@ANCHOR: batch_queue:reap_orphan_trees]
def reap_orphan_trees(repo, kind, repo_name, environ=None, live=lock_queue.is_live):
    """Remove combined trees whose creating process is dead (a leader that was killed). Returns the
    removed parent directories. Only names of the form ``batch_<kind>_<repo>_<pid>-<start>[suffix]``."""
    environ = os.environ if environ is None else environ
    base = environ.get("HAMS_BATCH_TREE_DIR") or os.path.join(os.path.expanduser("~"), "workspace", "tmp")
    pattern = re.compile(r"^batch_%s_%s_(\d+)-(\d+)" % (re.escape(kind), re.escape(repo_name)))
    removed = []
    try:
        names = os.listdir(base)
    except OSError:
        return removed
    for name in names:
        found = pattern.match(name)
        if found and not live(int(found.group(1)), int(found.group(2))):
            parent = os.path.join(base, name)
            remove_tree(repo, os.path.join(parent, repo_name))
            removed.append(parent)
    return removed


# ---- attribution and settling -----------------------------------------------------------------------

# [@ANCHOR: batch_queue:RunResult]
# What one run over a (sub)batch returns to `settle`: {"exit_code": int, "failures": {step_or_module: text},
# "detail": str}. exit_code 0 means every selected check passed.
# [@ANCHOR: batch_queue:Verdict]
# One branch's answer: {"status": PASSED|FAILED|SOLO, "exit_code": int, "detail": str, "findings": [str],
# "log": path, "batch": id} and, on a PASSED one, "with": [tips] (the other branches of the run it passed in; a pass
# stamp names them). SOLO means "the merge conflicted: run alone".


# [@ANCHOR: batch_queue:blame_by_tokens]
def blame_by_tokens(failures, owners, find_tokens):
    """Attribute failed checks to branches.

    `failures` is {name: output text} for each failed step or module; `owners` maps a token (a changed
    file path, a module name) to the list of tips that own it; `find_tokens(line)` yields the tokens a
    line of output names. Returns ({tip: [lines that name its tokens]}, [names of failures in which no
    line named any owned token]). A failure with at least one owned token is attributed to the owners
    of the tokens it names; one with none is unattributable (red main, a whole-tree check)."""
    blamed, unattributed = {}, []
    for name, text in failures.items():
        hit = False
        for line in str(text).splitlines():
            for token in find_tokens(line):
                for tip in owners.get(token, ()):
                    entry = "%s: %s" % (name, line.strip()[:300])
                    if entry not in blamed.setdefault(tip, []):  # a line naming two tokens counts once
                        blamed[tip].append(entry)
                    hit = True
        if not hit:
            unattributed.append(name)
    return blamed, unattributed


# [@ANCHOR: batch_queue:other_tips]
def _other_tips(group, member):
    """The tips of `group` other than `member`'s own, in order, each once: the branches the run that passed `member`
    covered. A pass stamp names them (`with=`) so a reader can see what the branch was checked with."""
    seen = []
    for other in group:
        if other["tip"] != member["tip"] and other["tip"] not in seen:
            seen.append(other["tip"])
    return seen


# [@ANCHOR: batch_queue:with_field]
def with_field(with_tips):
    """` with=<12 hex>,<12 hex>` naming the other tips a batched pass covered, or "" when there are none."""
    return " with=" + ",".join(tip[:12] for tip in with_tips) if with_tips else ""


# [@ANCHOR: batch_queue:settle]
def settle(members, run_once, attribute, budget=MAX_SETTLE_RUNS, log=print):
    """Run `members` once and turn the result into one Verdict per tip.

    `run_once(members) -> RunResult-like dict` plus optional "conflicted": [(member, text)] and
    "stamp_ok"; `attribute(failures, members) -> (blamed {tip: [lines]}, unattributed [names])`.
    Exit 0: all pass. Failures all attributed: blamed tips fail, every other tip passes at once.
    Any unattributable failure: the batch is split in halves and each half is settled again (a member
    alone is failed with the failure text); the number of runs is capped by `budget` (default 9), after
    which the remaining members fail with a note. A member whose merge conflicted gets status SOLO.
    Returns {tip: Verdict}. Cost bound: 2 * log2(K) + 1 runs for K members."""
    state = {"left": budget}

    def one(group, depth):
        verdicts = {}
        if state["left"] <= 0:
            for m in group:
                verdicts[m["tip"]] = {"status": FAILED, "exit_code": 1, "findings": [],
                                      "detail": "failure could not be attributed and the rerun budget is used up"}
            return verdicts
        state["left"] -= 1
        result = run_once(group)
        for member, text in result.get("conflicted", []):
            verdicts[member["tip"]] = {"status": SOLO, "exit_code": 0, "findings": [],
                                       "detail": "merge conflict in the combined tree: run alone. " + text[-200:]}
        live = [m for m in group if m["tip"] not in verdicts]
        if not live:
            return verdicts
        if result.get("fatal"):
            for m in live:
                verdicts[m["tip"]] = {"status": FAILED, "exit_code": result.get("exit_code", 1), "findings": [],
                                      "detail": result.get("detail") or "the run could not start"}
            return verdicts
        if result.get("exit_code", 1) == 0:
            for m in live:
                verdicts[m["tip"]] = {"status": PASSED, "exit_code": 0, "findings": [], "detail": "",
                                      "with": _other_tips(live, m)}
            return verdicts
        blamed, unattributed = attribute(result.get("failures", {}), live)
        if not unattributed and blamed:
            for m in live:
                lines = blamed.get(m["tip"])
                verdicts[m["tip"]] = ({"status": FAILED, "exit_code": 1, "findings": lines[:40],
                                       "detail": "%d finding(s) name this branch's files or modules" % len(lines)}
                                      if lines else {"status": PASSED, "exit_code": 0, "findings": [],
                                                     "detail": "checked clean (the failures belong to other branches)",
                                                     "with": _other_tips(live, m)})
            return verdicts
        if len(live) == 1:
            only = live[0]
            verdicts[only["tip"]] = {"status": FAILED, "exit_code": 1, "findings": [],
                                     "detail": "failed alone with no attributable file or module (red main?): "
                                               + ", ".join(unattributed or result.get("failures", {}))}
            return verdicts
        half = len(live) // 2
        log("[*] failure in %s cannot be attributed to a branch: splitting %d branches into %d + %d" % (
            ", ".join(unattributed) or "the run", len(live), half, len(live) - half))
        verdicts.update(one(live[:half], depth + 1))
        verdicts.update(one(live[half:], depth + 1))
        return verdicts

    return one(list(members), 0)


# [@ANCHOR: batch_queue:format_verdicts]
def format_verdicts(doc, my_tip):
    """Printable lines: one per branch of the batch, and a header with the shared log path."""
    lines = ["batch %s verdicts (log: %s):" % (doc.get("batch"), doc.get("log") or "?")]
    seen = set()
    for member in doc.get("members", []):
        if member["tip"] in seen:
            continue
        seen.add(member["tip"])
        verdict = (doc.get("verdicts") or {}).get(member["tip"], {})
        lines.append("  %-8s %s (%s)%s%s" % (
            str(verdict.get("status", "?")).upper(), member["branch"], member["tip"][:10],
            "  <- you" if member["tip"] == my_tip else "",
            ("  " + verdict["detail"]) if verdict.get("detail") and verdict.get("status") != PASSED else ""))
    return lines
