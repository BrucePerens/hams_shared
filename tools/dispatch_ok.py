#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
dispatch_ok: may a coordinator start another agent right now?

Prints OK or HOLD (with the reasons) and exits 0 for OK, 1 for HOLD. It reads the admission-control verdict that the load
display writes to /dev/shm/loadscreen/stats.json (its `dispatch` field: HOLD when a lint or test run has waited too long, too
many lint runs are queued, or the CPU or memory is over its limit). It never starts, probes or loads anything itself.

A missing, unreadable or stale file is HOLD: no verdict means no new agent. Coordinators start a new agent only when this says OK.
If the server says HOLD but `remote_capacity` lists a free overflow machine, the line after the verdict says so; work that routes there
may still be dispatched at the coordinator's discretion (the exit status stays 1: the verdict is about this server).

A second, separate signal (Bruce, 2026-11-09): the merge queue is the bottleneck, so what is throttled is PR LANDING, not agent starts. After the
agent-start verdict the tool prints LANDING_OK or LANDING_HOLD from the merge queue file (`~/.cache/hams-merge-queue/queue.json`, read only):
LANDING_HOLD when more than 25 PRs are queued+testing or the oldest queued PR has waited over 60 minutes. LANDING_HOLD never stops an agent from
starting (ten agents run at all times): it tells running agents to finish and commit but fold new PRs into an open one or hold them, and to do
gate-free work meanwhile. A missing queue file is an empty queue (LANDING_OK); an unreadable one is LANDING_HOLD.

    python3 hams_shared/tools/dispatch_ok.py            # OK / HOLD + reasons, then LANDING_OK / LANDING_HOLD + counts; exit 0 / 1 (agent start)
    python3 hams_shared/tools/dispatch_ok.py --landing  # same output; exit 0 for LANDING_OK, 2 for LANDING_HOLD (PR landing)
    python3 hams_shared/tools/dispatch_ok.py --quiet    # exit status only
    python3 hams_shared/tools/dispatch_ok.py --file F --max-age 90 --queue-file Q
"""

import argparse
import json
import math
import os
import sys
import time

# [@ANCHOR: dispatch_ok:stats_format]
# The part of the load display's stats.json this tool reads; see tools/README.md.
DEFAULT_STATS = "/dev/shm/loadscreen/stats.json"
MAX_FUTURE_SECONDS = 5.0  # a stamp further ahead than this is not from a running display (the clock stepped back, or it was written by hand)
DEFAULT_MAX_AGE_SECONDS = 60.0  # the display rewrites every 5 s; a minute without a rewrite means it is not running

# [@ANCHOR: dispatch_ok:landing_limits]
# Merge-queue limits for the LANDING verdict. Queue entries are written by devbox_tools/merge_queue.py (states "queued", "testing", ...).
DEFAULT_QUEUE_FILE = os.path.join(os.path.expanduser("~"), ".cache", "hams-merge-queue", "queue.json")
MAX_LIVE_PRS = 25  # LANDING_HOLD when more than this many PRs are queued or testing, all repositories together
MAX_OLDEST_QUEUED_SECONDS = 60 * 60  # LANDING_HOLD when the oldest queued PR has waited longer than this
LIVE_STATES = ("queued", "testing")
EXIT_LANDING_HOLD = 2


# [@ANCHOR: dispatch_ok:evaluate]
def evaluate(path=DEFAULT_STATS, max_age=DEFAULT_MAX_AGE_SECONDS, now=time.time):
    """(state, lines): state is "OK" or "HOLD"; lines are the human-readable reasons, then one line per free overflow machine.
    Any problem reading the file (missing, bad JSON, no `dispatch` field, older than `max_age` seconds, or dated more than a few seconds ahead) is HOLD."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as error:
        return "HOLD", ["no load stats at %s (%s): the load display is not running" % (path, error.__class__.__name__)]
    if not isinstance(data, dict):
        return "HOLD", ["load stats at %s are not a JSON object" % path]
    try:
        written = float(data["written_epoch"])
        if not math.isfinite(written):
            raise ValueError("not finite")
        age = now() - written
    except (KeyError, TypeError, ValueError):
        return "HOLD", ["load stats have no written_epoch"]
    if age > max_age:
        return "HOLD", ["load stats are %d s old (limit %d s): the load display is not running" % (age, max_age)]
    if age < -MAX_FUTURE_SECONDS:
        return "HOLD", ["load stats are dated %d s in the future: the load display is not running" % -age]
    verdict = data.get("dispatch")
    if not isinstance(verdict, dict) or verdict.get("state") not in ("OK", "HOLD"):
        return "HOLD", ["load stats carry no dispatch verdict (old load display?)"]
    lines = [str(reason) for reason in verdict.get("reasons", [])]
    remote = data.get("remote_capacity")
    if verdict["state"] == "HOLD" and isinstance(remote, dict):
        free = [h.get("name") for h in remote.get("hosts", []) if h.get("state") == "free"]
        if free:
            lines.append("overflow capacity free on: %s (informational; work routed there may still go)" % ", ".join(map(str, free)))
    return verdict["state"], lines


# [@ANCHOR: dispatch_ok:landing_verdict]
def landing_verdict(entries, now=time.time):
    """(state, lines, summary) from merge queue entries (the `entries` list of queue.json).

    state is "LANDING_HOLD" when more than MAX_LIVE_PRS entries are queued+testing or the oldest queued entry has waited more than
    MAX_OLDEST_QUEUED_SECONDS, else "LANDING_OK". `lines` are the reasons. `summary` is a one-line count: `queue: 12 queued, 3 testing (...)`.
    Pure; entries that are not dicts or lack a state are ignored. Only the `queued` wait counts for the age limit (a testing PR is already
    being served)."""
    counts = {}
    queued_total = testing_total = 0
    oldest = 0.0
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("state") not in LIVE_STATES:
            continue
        name = str(entry.get("slug") or os.path.basename(str(entry.get("repo", "?"))))
        name = name.rsplit("/", 1)[-1]
        per = counts.setdefault(name, {"queued": 0, "testing": 0})
        per[entry["state"]] += 1
        if entry["state"] == "queued":
            queued_total += 1
            try:
                enqueued = float(entry["enqueued"])
            except (KeyError, TypeError, ValueError):
                continue
            if math.isfinite(enqueued):
                oldest = max(oldest, now() - enqueued)
        else:
            testing_total += 1
    live = queued_total + testing_total
    reasons = []
    if live > MAX_LIVE_PRS:
        reasons.append("%d PRs queued+testing is over %d" % (live, MAX_LIVE_PRS))
    if oldest > MAX_OLDEST_QUEUED_SECONDS:
        reasons.append("oldest queued PR has waited %d min, over %d min" % (oldest / 60, MAX_OLDEST_QUEUED_SECONDS / 60))
    per_repo = ", ".join("%s %dq/%dt" % (n, c["queued"], c["testing"]) for n, c in sorted(counts.items()))
    summary = "queue: %d queued, %d testing%s, oldest queued wait %d min" % (
        queued_total, testing_total, " (%s)" % per_repo if per_repo else "", oldest / 60)
    return ("LANDING_HOLD" if reasons else "LANDING_OK"), reasons, summary


# [@ANCHOR: dispatch_ok:landing]
def landing(path=DEFAULT_QUEUE_FILE, now=time.time):
    """(state, lines, summary) for the queue file at `path`: a missing file is an empty queue (LANDING_OK); an unreadable or malformed one is
    LANDING_HOLD. Never raises. Read-only, takes no lock (the queue file is replaced atomically)."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return landing_verdict([], now)
    except (OSError, ValueError) as error:
        return "LANDING_HOLD", ["merge queue file %s unreadable (%s)" % (path, error.__class__.__name__)], "queue: unknown"
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return "LANDING_HOLD", ["merge queue file %s has no entries list" % path], "queue: unknown"
    return landing_verdict(entries, now)


# [@ANCHOR: dispatch_ok:main]
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", default=DEFAULT_STATS)
    parser.add_argument("--max-age", type=float, default=DEFAULT_MAX_AGE_SECONDS, help="seconds before the stats count as stale")
    parser.add_argument("--queue-file", default=DEFAULT_QUEUE_FILE, help="the merge queue file for the LANDING verdict")
    parser.add_argument("--landing", action="store_true", help="exit with the LANDING verdict (0 LANDING_OK, 2 LANDING_HOLD) instead of the agent-start one")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    state, lines = evaluate(args.file, args.max_age)
    land_state, land_lines, summary = landing(args.queue_file)
    if not args.quiet:
        print(state)
        for line in lines:
            print("  - " + line)
        print(land_state)
        for line in land_lines:
            print("  - " + line)
        print("  - " + summary)
        if land_state == "LANDING_HOLD":
            print("  - agents keep running: finish and commit, but fold new PRs into an open one or hold them, and do gate-free work")
    if args.landing:
        return 0 if land_state == "LANDING_OK" else EXIT_LANDING_HOLD
    return 0 if state == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())
