#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Who is the night-watch agent right now, and when did it last do anything?

Bruce, 2026-09-16: "the night-watch agent does the actual work, and the cron jobs just wake it up if
it's not awake already." A cron-fired session has no memory and cannot see session titles, so it
needs a durable answer to "is there already an agent, and which session is it". This file keeps
that answer in one small JSON state file on the dev box. It is runtime state, not project history,
so it lives outside every git repository.

The liveness question itself is NOT answered here. Only `ListAgents` (a Claude tool, not a shell
command) can say whether a session is still running, and session names are reused over time, so
the caller compares the recorded `ref` (the short id `ListAgents` prints beside a name) against the
live list. This tool only records and reports.

    night_watch_agent.py show
    night_watch_agent.py claim --name workspace-4a --ref e5ef45 [--force]
    night_watch_agent.py heartbeat --ref e5ef45 --state working|idle
    night_watch_agent.py release --ref e5ef45

Exit codes: 0 success; 3 claim refused because another agent is recorded (confirm it is gone with
`ListAgents`, then pass --force); 4 heartbeat/release refused because the recorded agent is a
different session (someone took over; stop acting as the agent).
"""

import argparse
import contextlib
import datetime
import fcntl
import json
import os
import sys
import tempfile

DEFAULT_STATE_PATH = os.path.expanduser("~/.local/state/night-watch/agent.json")
STATES = ("working", "idle")

EXIT_CLAIM_REFUSED = 3
EXIT_NOT_THE_AGENT = 4


def state_path():
    return os.environ.get("NIGHT_WATCH_STATE") or DEFAULT_STATE_PATH


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


@contextlib.contextmanager
def _locked(path):
    """Serialise read-modify-write across concurrent cron runs and the agent itself."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def read_state(path):
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_state(path, state):
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".agent.", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def describe(state, now=None):
    """The record plus how many minutes ago the agent last reported, for the cron's decision."""
    if state is None:
        return {"agent": None}
    now = now or _now()
    last = datetime.datetime.strptime(state["last_active"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc
    )
    return {"agent": state, "minutes_since_last_active": int((now - last).total_seconds() // 60)}


def claim(path, name, ref, force=False, now=None):
    now = now or _now()
    with _locked(path):
        current = read_state(path)
        if current is not None and current["ref"] != ref and not force:
            return EXIT_CLAIM_REFUSED, current
        state = {
            "name": name,
            "ref": ref,
            "state": "working",
            "claimed_at": _iso(now),
            "last_active": _iso(now),
        }
        if current is not None and current["ref"] == ref:
            state["claimed_at"] = current["claimed_at"]
        _write_state(path, state)
        return 0, state


def heartbeat(path, ref, new_state, now=None):
    if new_state not in STATES:
        raise ValueError(f"state must be one of {STATES}, not {new_state!r}")
    now = now or _now()
    with _locked(path):
        current = read_state(path)
        if current is None or current["ref"] != ref:
            return EXIT_NOT_THE_AGENT, current
        current["state"] = new_state
        current["last_active"] = _iso(now)
        _write_state(path, current)
        return 0, current


def release(path, ref):
    with _locked(path):
        current = read_state(path)
        if current is None or current["ref"] != ref:
            return EXIT_NOT_THE_AGENT, current
        os.remove(path)
        return 0, None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Record which session is the night-watch agent.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show")
    p_claim = sub.add_parser("claim")
    p_claim.add_argument("--name", required=True)
    p_claim.add_argument("--ref", required=True)
    p_claim.add_argument("--force", action="store_true")
    p_beat = sub.add_parser("heartbeat")
    p_beat.add_argument("--ref", required=True)
    p_beat.add_argument("--state", required=True, choices=STATES)
    p_release = sub.add_parser("release")
    p_release.add_argument("--ref", required=True)
    args = parser.parse_args(argv)

    path = state_path()
    if args.command == "show":
        print(json.dumps(describe(read_state(path)), indent=2, sort_keys=True))
        return 0
    if args.command == "claim":
        rc, state = claim(path, args.name, args.ref, force=args.force)
    elif args.command == "heartbeat":
        rc, state = heartbeat(path, args.ref, args.state)
    else:
        rc, state = release(path, args.ref)
    print(json.dumps(describe(state), indent=2, sort_keys=True))
    return rc


if __name__ == "__main__":
    sys.exit(main())
