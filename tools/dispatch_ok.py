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

    python3 hams_shared/tools/dispatch_ok.py            # OK / HOLD + reasons, exit 0 / 1
    python3 hams_shared/tools/dispatch_ok.py --quiet    # exit status only
    python3 hams_shared/tools/dispatch_ok.py --file F --max-age 90
"""

import argparse
import json
import math
import sys
import time

# [@ANCHOR: dispatch_ok:stats_format]
# The part of the load display's stats.json this tool reads; see tools/README.md.
DEFAULT_STATS = "/dev/shm/loadscreen/stats.json"
MAX_FUTURE_SECONDS = 5.0  # a stamp further ahead than this is not from a running display (the clock stepped back, or it was written by hand)
DEFAULT_MAX_AGE_SECONDS = 60.0  # the display rewrites every 5 s; a minute without a rewrite means it is not running


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


# [@ANCHOR: dispatch_ok:main]
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", default=DEFAULT_STATS)
    parser.add_argument("--max-age", type=float, default=DEFAULT_MAX_AGE_SECONDS, help="seconds before the stats count as stale")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    state, lines = evaluate(args.file, args.max_age)
    if not args.quiet:
        print(state)
        for line in lines:
            print("  - " + line)
    return 0 if state == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())
