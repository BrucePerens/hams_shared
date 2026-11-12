#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
The nightly whole-system canary (ADR 0118): a sharded whole-system run on a clean snapshot of `origin/main`.

    gate_canary.py run    [--workspace DIR] [--parent DIR] [--state-dir DIR] [--shards N] [--shards-file FILE]
                          [--jobs N] [--max-jobs-when-idle N] [--only-if-new-commit] [--no-bisect] [--no-fetch]
    gate_canary.py status [--state-dir DIR]          # print status.json

`run` builds the gate tree with `gate_snapshot.py` (which refuses a stale input), runs the shards through the ordinary
`test.py` slot queue, collects each shard's `test_results.json`, and writes `<state-dir>/status.json`
(`commit`, `time`, `new_failures`, `tolerated`, `flakes`, and more) and `known_red_candidates.json` in the known-red
baseline's format. When a NEW failure appears it runs `test.py --bisect-modules` on the failing shard to find a minimal module
set, and records the report. It never edits the tracked baseline: a person (or an agent through a pull request) decides which
candidates join it.

It runs on this machine only, at `nice -n 10`, one shard at a time unless the machine is idle (see `--max-jobs-when-idle`), never
signals a process, and refuses on the production host. The systemd user units are `hams-gate-canary.service` and
`hams-gate-canary.timer`; they are provided and not enabled. The reference is the "Nightly whole-system canary" section of tools/README.md.
"""

import argparse
import concurrent.futures
import fcntl
import glob
import json
import os
import re
import shlex
import subprocess
import sys
import time

import gate_snapshot
import known_red
import lint_cache
import runner_slots

DEFAULT_STATE_DIR = os.path.expanduser("~/.cache/hams-test/canary")
STATUS_NAME = "status.json"
CANDIDATES_NAME = "known_red_candidates.json"
NOTE_NAME = "note.txt"
STATUS_VERSION = 1
IDLE_LOAD = 4.0
BISECT_MAX_RUNS = 30
RELAY_REL = os.path.join("daemons", "hams_local_relay")
RELAY_FEATURES = "radioctl,shared_receiver"
RELAY_LOG = "relay_full_suite.log"
RELAY_ID_PREFIX = "relay cargo test: "


class CanaryError(RuntimeError):
    """The canary cannot run (another run holds the lock, or a step failed); the message names the cause."""


# [@ANCHOR: gate_canary:read_shard_results]
def read_shard_results(log_directory):
    """The merged `test_results*.json` of one shard's log directory, or None when there is none (the shard passed, or died
    before a verdict: the exit status says which). Merges lists and dicts (a run in slot K writes `test_results.slot-K.json`).
    Raises `CanaryError` for a file that is not the documented JSON."""
    merged = None
    for path in sorted(glob.glob(os.path.join(log_directory, "test_results*.json"))):
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            data["new_failures"], data["known_red"], data["flakes"]
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise CanaryError("%s: cannot read the shard results: %s" % (path, error)) from error
        if merged is None:
            merged = {"new_failures": [], "known_red": {}, "flakes": {}, "now_passing": [], "fix_me": [], "unparsed": 0,
                      "counts": {"failed": 0, "errors": 0, "total": 0}, "commit": data.get("commit", "")}
        merged["new_failures"] += data["new_failures"]
        merged["known_red"].update(data["known_red"])
        merged["flakes"].update(data["flakes"])
        merged["now_passing"] += data.get("now_passing", [])
        merged["fix_me"] += data.get("fix_me", [])
        merged["unparsed"] += int(data.get("unparsed", 0))
        for key in merged["counts"]:
            merged["counts"][key] += int(data.get("counts", {}).get(key, 0))
    return merged


# [@ANCHOR: gate_canary:shard_tests_run]
def shard_tests_run(log_path):
    """How many tests a passing shard's log says ran: the sum of the N in every `0 failed, 0 error(s) of N tests` line (test.py's `TEST RUN COMPLETE` line and
    Odoo's own `odoo.tests.result:` line), 0 when the log is missing, unreadable or has none. Pure apart from the read."""
    try:
        with open(log_path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return 0
    return sum(int(n) for n in re.findall(r"\b0 failed, 0 error\(s\) of (\d+) tests", text))


# [@ANCHOR: gate_canary:parse_cargo_failures]
def parse_cargo_failures(text):
    """`{"failed": [test paths], "passed": N}` from the output of `cargo test --no-fail-fast`: every `test <path> ... FAILED` line (sorted, de-duplicated) and the
    sum of the `N passed` counts of the `test result:` lines. Pure. Full entry: tools/README.md."""
    failed = sorted(set(re.findall(r"^test (\S+) \.\.\. FAILED$", text, re.M)))
    passed = sum(int(count) for count in re.findall(r"test result: \w+\. (\d+) passed", text))
    return {"failed": failed, "passed": passed}


# [@ANCHOR: gate_canary:relay_command]
def relay_command(relay_dir, target_dir):
    """The shell line of the nightly full relay suite: `cd <relay> && CARGO_TARGET_DIR=<target> CARGO_INCREMENTAL=0 cargo test --locked --features
    radioctl,shared_receiver --no-fail-fast`, every test of every target, all failures listed, not the structural group the lint step runs. Pure. Full entry: tools/README.md."""
    return "cd %s && CARGO_TARGET_DIR=%s CARGO_INCREMENTAL=0 cargo test --locked --features %s --no-fail-fast" % (
        shlex.quote(relay_dir), shlex.quote(target_dir), RELAY_FEATURES)


# [@ANCHOR: gate_canary:run_relay_suite]
def run_relay_suite(com_tree, log_directory, runner=None, now=time.time):
    """Runs the whole relay `cargo test` suite of the snapshot's hams_com tree (`com_tree`) and returns `{"status", "failed", "passed", "exit_status", "seconds",
    "log"}`: `status` is `green` (exit 0), `red` (non-zero with at least one `FAILED` test: `failed` names them) or `error` (non-zero and no failed test: it did not
    build or died, so it is never green) and `skipped` (with `reason`) when `com_tree` is None or holds no relay crate, which the status and note say. The build
    goes to the budgeted Cargo directory `lint_cache.named_cargo_session("relay-nightly")` (warm after the first night). `runner(command, log_path)` runs the shell
    line (default `shell_runner`). Never signals a process. Full entry: tools/README.md."""
    relay = os.path.join(com_tree, RELAY_REL) if com_tree else None
    if not relay or not os.path.isfile(os.path.join(relay, "Cargo.toml")):
        return {"status": "skipped", "reason": "no relay crate in the snapshot", "failed": [], "passed": 0, "exit_status": None, "seconds": 0.0, "log": ""}
    runner = runner or shell_runner
    os.makedirs(log_directory, exist_ok=True)
    log_path = os.path.join(log_directory, RELAY_LOG)
    started = now()
    with lint_cache.named_cargo_session("relay-nightly") as target:
        status = runner(relay_command(relay, target or os.path.join(log_directory, "relay-target")), log_path)
    try:
        with open(log_path, encoding="utf-8", errors="replace") as handle:
            parsed = parse_cargo_failures(handle.read())
    except OSError:
        parsed = {"failed": [], "passed": 0}
    # Green needs positive evidence: exit 0 AND at least one test counted in the log. Exit 0 with no `N passed` line (a cargo that ran nothing,
    # an unreadable log) is an error, never a pass.
    if status == 0:
        verdict = "green" if parsed["passed"] > 0 else "error"
    else:
        verdict = "red" if parsed["failed"] else "error"
    return {"status": verdict, "failed": parsed["failed"], "passed": parsed["passed"], "exit_status": status, "seconds": round(now() - started, 1), "log": log_path}


# [@ANCHOR: gate_canary:aggregate]
def aggregate(shards, commits, now=None, relay=None):
    """The status document of one canary run.

    `shards`: a list of `{"number", "modules", "exit_status", "results"}` (`results` from `read_shard_results`, None when the shard
    wrote none). `commits`: `{"com", "open", "shared"}`. Returns `{"version", "commit" (hams_com), "commits", "time", "new_failures",
    "tolerated", "flakes", "now_passing", "fix_me", "shard_errors", "counts", "shards"}`: every list sorted and de-duplicated. A
    shard that exited non-zero without any results file is a `shard_errors` entry (it has no verdict, so it is never green); a shard with `problems` (its input was stale, it was not run) is one too; an
    exit status of 0 with no results is a pass. An `unparsed` failure count is a shard error too. `relay` (from `run_relay_suite`) adds a `relay`
    entry; each failed relay test is a new failure named `relay cargo test: <path>` and a relay run that is `error` is a shard error. `status` is `red` when there is a new
    failure or shard error, else `green`."""
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) if now is None else now
    new, tolerated, flakes, passing, fix_me, errors = set(), set(), set(), set(), set(), []
    counts = {"failed": 0, "errors": 0, "total": 0}
    detail = {}
    if not shards:
        errors.append("no shard was run: a night with nothing tested has no verdict")
    for shard in shards:
        results = shard.get("results")
        if shard.get("problems"):
            errors.append("shard %s: stale or incomplete input, not run: %s" % (shard.get("number"), "; ".join(shard["problems"])))
            continue
        if results is None:
            if shard.get("exit_status") != 0:
                errors.append("shard %s: exit status %s and no test_results.json (no verdict)" % (shard.get("number"), shard.get("exit_status")))
            elif not shard.get("tests_run"):
                # test.py writes no results file for a passing run, so a pass is exit 0 plus its own "Odoo reports 0 failed ... of N tests" line.
                errors.append("shard %s: exit status 0 but its log shows no tests that ran (zero tests, or no verdict line): not a pass" % shard.get("number"))
            continue
        new.update(results["new_failures"])
        tolerated.update(results["known_red"])
        detail.update({i: v for i, v in results["known_red"].items() if isinstance(v, dict)})
        flakes.update(results["flakes"])
        passing.update(results["now_passing"])
        fix_me.update(results["fix_me"])
        if results["unparsed"]:
            errors.append("shard %s: %d failure(s) no log line named" % (shard.get("number"), results["unparsed"]))
        elif shard.get("exit_status") not in (0, None) and not results["new_failures"]:
            # test.py writes the file for every non-zero Odoo exit and returns non-zero when the run was cut short (CPU limit, a signal): the
            # file then lists only the failures seen before the abort, all tolerated or flaky, and must not make the shard green.
            errors.append("shard %s: exit status %s with a results file and no new failure: the run did not finish" % (shard.get("number"), shard.get("exit_status")))
        for key in counts:
            counts[key] += results["counts"][key]
    if relay:
        new.update(RELAY_ID_PREFIX + name for name in relay.get("failed", []))
        if relay.get("status") == "error":
            errors.append("relay full suite: exit status %s and no failed test or no passed test (it did not build, ran nothing or died); log %s" % (relay.get("exit_status"), relay.get("log")))
    return {
        "relay": relay, "version": STATUS_VERSION, "commit": commits.get("com", ""), "commits": commits, "time": now,
        "status": "red" if (new or errors) else "green", "new_failures": sorted(new), "tolerated": sorted(tolerated),
        "flakes": sorted(flakes), "now_passing": sorted(passing), "fix_me": sorted(fix_me), "shard_errors": errors,
        "counts": counts, "tolerated_detail": detail,
        "shards": [{"number": s.get("number"), "modules": s.get("modules"), "exit_status": s.get("exit_status")} for s in shards],
    }


# [@ANCHOR: gate_canary:baseline_candidates]
def baseline_candidates(status, known):
    """The known-red baseline format (`{"version": 1, "entries": [...]}`) of this run's red tests: each tolerated test keeps its
    entry from `known` (`{id: {"owner", "reason", "first_seen"}}`, from the results), each new failure becomes an entry with
    `first_seen` the hams_com commit, owner `unassigned` and a reason naming the canary run. Sorted by id. Pure. It is a
    proposal: nothing writes it into a tracked baseline."""
    entries = []
    for test_id in status["tolerated"]:
        kept = known.get(test_id, {})
        entries.append({"test_id": test_id, "first_seen": kept.get("first_seen", status["commit"]),
                        "first_seen_time": kept.get("first_seen_time", status["time"]), "owner": kept.get("owner", "unassigned"),
                        "reason": kept.get("reason", "listed in the baseline")})
    for test_id in status["new_failures"]:
        if test_id.startswith(RELAY_ID_PREFIX):
            continue  # a relay cargo test is not an Odoo test id: it is reported, never proposed for the Odoo baseline
        entries.append({"test_id": test_id, "first_seen": status["commit"], "first_seen_time": status["time"], "owner": "unassigned",
                        "reason": "canary: failed on %s (%s); investigate" % (status["commit"][:12], status["time"])})
    return {"version": known_red.BASELINE_VERSION, "entries": sorted(entries, key=lambda e: e["test_id"])}


# [@ANCHOR: gate_canary:note_lines]
def note_lines(status, lint_lines=()):
    """The canary note: every tolerated item, so none can be forgotten. Returns lines: the run's verdict and commit, each tolerated
    test with its age and owner (`tolerated_detail`), each flake, each flake above the threshold (FIX ME), each baselined test that now
    passes (remove its entry), each expired or new failure, the shard errors, and `lint_lines` (the lint baseline's
    `--baseline-show` lines) after a heading. Pure."""
    lines = ["canary %s of %s at %s: %d new failure(s), %d tolerated test(s), %d flake(s)" % (
        status["status"], status["commit"][:12], status["time"], len(status["new_failures"]), len(status["tolerated"]),
        len(status["flakes"]))]
    for test_id in status["new_failures"]:
        lines.append("NEW or EXPIRED failure: %s" % test_id)
    for error in status["shard_errors"]:
        lines.append("SHARD ERROR: %s" % error)
    for test_id in status["tolerated"]:
        item = status.get("tolerated_detail", {}).get(test_id, {})
        lines.append("tolerated (known red): %s, known red for %s days, owner %s: %s" % (
            test_id, item.get("age_days", "?"), item.get("owner", "?"), item.get("reason", "")))
    relay = status.get("relay")
    if relay:
        if relay["status"] == "skipped":
            lines.append("relay full suite: NOT RUN (%s)" % relay.get("reason", ""))
        else:
            lines.append("relay full suite: %s, %d passed, %d failed, %.0f s, log %s" % (relay["status"], relay["passed"], len(relay["failed"]), relay["seconds"], relay["log"]))
    lines += ["flaky: %s" % i for i in status["flakes"]] + ["FIX ME (flaky above the threshold): %s" % i for i in status["fix_me"]]
    lines += ["remove from the baseline (now passing): %s" % i for i in status["now_passing"]]
    if lint_lines:
        lines.append("lint known-red baseline:")
        lines += list(lint_lines)
    return lines


# [@ANCHOR: gate_canary:failure_tags]
def failure_tags(test_ids):
    """The `--test-tags` selector for a bisection of these ids: `/module:Class` for every distinct class, sorted (a class is the
    smallest unit that keeps its setUpClass). Ids that are not `module:Class.method` are ignored. Pure."""
    classes = set()
    for test_id in test_ids:
        module, _, rest = test_id.partition(":")
        if module and "." in rest:
            classes.add("/%s:%s" % (module, rest.split(".")[0]))
    return ",".join(sorted(classes))


# [@ANCHOR: gate_canary:bisect_command]
def bisect_command(shard_command, tags, report_path):
    """The bisection command of a shard: the shard's own command line (same tree, environment, account, `-u` module list) with
    `--bisect-modules --test-tags TAGS --bisect-max-runs 30 --bisect-report REPORT` appended. Pure."""
    return "%s --bisect-modules --test-tags %s --bisect-max-runs %d --bisect-report %s" % (
        shard_command, tags, BISECT_MAX_RUNS, report_path)


# [@ANCHOR: gate_canary:choose_jobs]
def choose_jobs(requested, idle_cap, slots, shard_count, load=None):
    """How many shards to run at once: `requested` (at least 1), raised to `idle_cap` when the one-minute load average is below
    4 (nothing else is running), never more than `slots` (the machine's test slots) or `shard_count`, and at least 1."""
    load = os.getloadavg()[0] if load is None else load
    jobs = max(requested, idle_cap) if load < IDLE_LOAD else requested
    return max(1, min(jobs, slots, shard_count))


# [@ANCHOR: gate_canary:shell_runner]
def shell_runner(command, log_path):
    """Runs a shell command line with its output appended to `log_path`; returns its exit status. The command is a
    `gate_snapshot.shard_command` string; no process is signalled."""
    with open(log_path, "ab") as log:
        return subprocess.call(["bash", "-c", command], stdout=log, stderr=subprocess.STDOUT)


def _write_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    temporary = "%s.tmp%d" % (path, os.getpid())
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=1, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)


# [@ANCHOR: gate_canary:run_canary]
def run_canary(workspace=gate_snapshot.DEFAULT_WORKSPACE, parent=None, state_dir=DEFAULT_STATE_DIR, shards=gate_snapshot.DEFAULT_SHARDS,
               shards_file=None, jobs=1, idle_jobs=2, only_if_new_commit=False, bisect=True, fetch=True, runner=None, snapshot=None,
               out=print, hostname=None, slots=None, load=None, relay=True):
    """One canary run; returns `(exit status, status document or None)`.

    Takes `<state_dir>/canary.lock` (a second run exits 3 at once, nothing is waited for). With `only_if_new_commit` and the
    commits of the last green or red run equal to `origin/main` now, returns `(0, None)` without building. Builds the snapshot
    (`snapshot` replaces `gate_snapshot.create_snapshot` in tests), runs the shard commands with `choose_jobs` at a time, and writes
    `status.json` and `known_red_candidates.json` to `state_dir`. With `bisect`, every shard with a new failure is bisected (one
    after the other) and the report path is added to the status as `bisect`. Exit status: 0 green, 1 red (a new failure or shard error),
    2 refused (production host, stale or missing input), 3 already running. `runner(command, log_path)` runs a command line (default
    `shell_runner`)."""
    runner = runner or shell_runner
    try:
        gate_snapshot.refuse_production(hostname)
    except gate_snapshot.SnapshotError as error:
        out("REFUSING: %s" % error)
        return 2, None
    os.makedirs(state_dir, exist_ok=True)
    lock = open(os.path.join(state_dir, "canary.lock"), "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        out("gate_canary: another canary run holds %s" % os.path.join(state_dir, "canary.lock"))
        return 3, None
    try:
        os.nice(10)
    except OSError:
        pass
    status_path = os.path.join(state_dir, STATUS_NAME)
    try:
        if only_if_new_commit and _unchanged(workspace, status_path, fetch):
            out("gate_canary: origin/main has not moved since the last run; nothing to do")
            return 0, None
        make = snapshot or gate_snapshot.create_snapshot
        manifest = make(workspace, parent, shards, shards_file or _saved_shards(state_dir), fetch, True, None, out=out)
    except gate_snapshot.SnapshotError as error:
        out("REFUSING: %s" % error)
        return 2, None
    trees = manifest["trees"]
    shard_modules, commands = manifest["shards"], manifest["commands"]
    _write_json(os.path.join(state_dir, "gate_shards.json"), shard_modules)
    width = choose_jobs(jobs, idle_jobs, slots if slots is not None else _slots(), len(commands), load)
    out("gate_canary: %d shard(s), %d at a time, tree %s" % (len(commands), width, trees["parent"]))

    def one(index):
        directory = os.path.join(trees["parent"], "logs", "shard%d" % (index + 1))
        problems = gate_snapshot.verify_snapshot(trees["parent"], fetch=False, environ={gate_snapshot.OPEN_TREE_ENV: trees["open"]},
                                                 cargo_target_dir=manifest["cargo_target_dir"])
        # origin/main moving while an earlier shard ran is normal (merges); the snapshot was current when it was made and the
        # status names its commits, so only a damaged tree or artifact stops a later shard.
        problems = [problem for problem in problems if "is behind origin/main" not in problem]
        if problems:
            return {"number": index + 1, "modules": shard_modules[index], "exit_status": 2, "results": None, "problems": problems}
        for stale in glob.glob(os.path.join(directory, "test_results*.json")):
            os.unlink(stale)  # a rerun on the same commits must not read the verdict file of an earlier run
        log_path = os.path.join(trees["parent"], "logs", "shard%d.log" % (index + 1))
        status = runner(commands[index], log_path)
        return {"number": index + 1, "modules": shard_modules[index], "exit_status": status, "results": read_shard_results(directory),
                "tests_run": shard_tests_run(log_path)}

    with concurrent.futures.ThreadPoolExecutor(max_workers=width) as pool:
        shard_results = list(pool.map(one, range(len(commands))))
    commits = {"com": trees["com_commit"], "open": trees["open_commit"], "shared": trees["shared_commit"]}
    relay_result = run_relay_suite(trees.get("com"), os.path.join(trees["parent"], "logs"), runner) if relay else None
    status = aggregate(shard_results, commits, relay=relay_result)
    known = {}
    for shard in shard_results:
        known.update((shard["results"] or {}).get("known_red", {}))
    _write_json(os.path.join(state_dir, CANDIDATES_NAME), baseline_candidates(status, known))
    if bisect and status["new_failures"]:
        status["bisect"] = _bisect_failures(status, shard_results, commands, trees, runner, out)
    _write_json(status_path, status)
    note = note_lines(status, _lint_baseline_lines())
    with open(os.path.join(state_dir, NOTE_NAME), "w", encoding="utf-8") as handle:
        handle.write("\n".join(note) + "\n")
    out("gate_canary: %s; %d new failure(s), %d tolerated, %d flake(s); status: %s" % (
        status["status"], len(status["new_failures"]), len(status["tolerated"]), len(status["flakes"]), status_path))
    return (0 if status["status"] == "green" else 1), status


def _lint_baseline_lines():
    """`--baseline-show` lines of the lint baseline of hams_com, or () when the module or a record is unavailable."""
    try:
        import lint_baseline

        return [line for line in lint_baseline.show_lines("hams_com") if "none recorded" not in line]
    except (ImportError, ValueError, OSError):
        return ()


def _slots():
    try:
        return runner_slots.configured_slot_count()
    except (runner_slots.SlotConfigError, OSError):
        return 1


def _saved_shards(state_dir):
    path = os.path.join(state_dir, "gate_shards.json")
    return path if os.path.isfile(path) else None


def _unchanged(workspace, status_path, fetch):
    try:
        with open(status_path, encoding="utf-8") as handle:
            last = json.load(handle)["commits"]
        com = gate_snapshot.origin_main(os.path.join(workspace, "hams_com"), fetch)
        open_ = gate_snapshot.origin_main(os.path.join(workspace, "hams_open"), fetch)
    except (OSError, ValueError, KeyError, gate_snapshot.SnapshotError):
        return False
    return last.get("com") == com and last.get("open") == open_


def _bisect_failures(status, shard_results, commands, trees, runner, out):
    """Bisects each shard that has a new failure; returns `{shard number: {"tags", "report", "exit_status"}}`."""
    wanted = set(status["new_failures"])
    done = {}
    for shard in shard_results:
        failed = sorted(wanted & set((shard["results"] or {}).get("new_failures", [])))
        tags = failure_tags(failed)
        if not tags:
            continue
        number = shard["number"]
        report = os.path.join(trees["parent"], "logs", "shard%d" % number, "bisect_report.json")
        out("gate_canary: bisecting shard %d for %s" % (number, tags))
        code = runner(bisect_command(commands[number - 1], tags, report), os.path.join(trees["parent"], "logs", "bisect%d.log" % number))
        done[str(number)] = {"tags": tags, "report": report, "exit_status": code}
    return done


# [@ANCHOR: gate_canary:main]
def main(argv=None, out=print):
    """The command line: `run` and `status`. Returns the exit status of `run_canary` (0 green, 1 red, 2 refused, 3 already running);
    `status` prints `status.json` and returns 0 (1 when there is none)."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="build a clean snapshot of origin/main, run the shards, write the status")
    run.add_argument("--workspace", default=gate_snapshot.DEFAULT_WORKSPACE)
    run.add_argument("--parent", default=None)
    run.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    run.add_argument("--shards", type=int, default=gate_snapshot.DEFAULT_SHARDS)
    run.add_argument("--shards-file", default=None)
    run.add_argument("--jobs", type=int, default=1, help="shards at once (default 1)")
    run.add_argument("--max-jobs-when-idle", type=int, default=2, help="shards at once when the machine is idle (default 2)")
    run.add_argument("--only-if-new-commit", action="store_true", help="do nothing when origin/main is what the last run tested")
    run.add_argument("--no-bisect", action="store_true")
    run.add_argument("--no-fetch", action="store_true")
    run.add_argument("--no-relay", action="store_true", help="skip the whole relay `cargo test` suite (it runs after the shards by default)")
    status = sub.add_parser("status", help="print status.json")
    status.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    args = parser.parse_args(argv)
    if args.command == "status":
        try:
            with open(os.path.join(args.state_dir, STATUS_NAME), encoding="utf-8") as handle:
                out(handle.read().rstrip())
            return 0
        except OSError as error:
            out("gate_canary: no status: %s" % error)
            return 1
    try:
        code, _ = run_canary(args.workspace, args.parent, args.state_dir, args.shards, args.shards_file, args.jobs,
                             args.max_jobs_when_idle, args.only_if_new_commit, not args.no_bisect, not args.no_fetch, out=out, relay=not args.no_relay)
    except CanaryError as error:
        out("gate_canary: %s" % error)
        return 1
    return code


if __name__ == "__main__":
    sys.exit(main())
