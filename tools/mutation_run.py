#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Unattended mutation testing over a list of targets: one JSON result per target, no agent turn until a survivor needs a test.

    mutation_run.py TARGETS.toml [--out-dir DIR] [--only NAME[,NAME]] [--force] [--max-age-days N] [--budget-total MINUTES] [--dry-run]

TARGETS.toml has one `[[target]]` table per target:

    [[target]]
    name = "known_red"                      # result file <out-dir>/known_red.json
    root = "~/workspace/hams_open/hams_shared"   # the tree that holds the file (a live checkout is fine: it is only COPIED)
    file = "tools/known_red.py"             # relative to root
    cwd = "tools"                           # where the tests run, relative to root
    test = "python3 -m pytest -q -x test_known_red.py"
    budget_minutes = 20                     # no new mutant starts after this (default 20)
    workers = 4                             # parallel mutants (default 4)
    timeout = 120                           # seconds per mutant (default 120)
    funcs = ["check_target"]                # optional: only mutants inside these functions

Safety (incident 2026-11-09): the live checkout is never mutated. `root` is copied (rsync, without .git, node_modules, target, tmp) into a
scratch directory made by `ram_dirs.py run` (RAM when there is room, removed when the job ends), `mutation_harness.py` clones that copy again
and refuses any path outside its own clone, and the result records whether the live file was byte-identical afterwards (`original_unchanged`).
The harness runs at nice 10. The job is a named `agent_procs.py` job; the budget is also enforced from outside (stopped at 1.5 x budget + 10 min).

Result `<out-dir>/<name>.json`: name, file, status (`done`, `partial` budget reached, `baseline_fails`, `error`, `timeout`), mutants, run, killed,
survived, not_run, score (percent), survivors `[{file, line, operator}]`, seconds, source_sha256, test, original_unchanged, log. A target whose
`done` result is younger than `--max-age-days` (default 7) for the same file hash, test and funcs is skipped (idempotent re-runs); `--force` reruns.
Exit 0 when every selected target is done, partial or skipped, 1 when one failed, 4 when the load display says HOLD or memory is low (nothing ran).
Reference: tools/README.md "Overnight automation".
"""

import argparse
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import overnight_common as common  # noqa: E402

DEFAULT_BUDGET_MINUTES = 20
EXCLUDES = (".git", "node_modules", "target", "__pycache__", "/tmp", ".pytest_cache", ".hypothesis")
OUT_SUBDIR = "mutation"


# [@ANCHOR: mutation_run:result_path]
def result_path(out_dir, name):
    """`<out_dir>/<slug(name)>.json`."""
    return os.path.join(out_dir, common.slug(name) + ".json")


# [@ANCHOR: mutation_run:fingerprint]
def fingerprint(target):
    """What decides that an earlier `done` result still holds: the target file's sha256 (None when it is missing), the test command and the funcs."""
    path = os.path.join(target["root"], target["file"])
    digest = common.sha256_file(path) if os.path.isfile(path) else None
    return {"source_sha256": digest, "test": target.get("test", ""), "funcs": sorted(target.get("funcs", []))}


# [@ANCHOR: mutation_run:is_fresh]
def is_fresh(previous, current, max_age_days, now=time.time):
    """True when `previous` (a result document or None) is `done`, has the same fingerprint as `current` and is younger than `max_age_days`."""
    if not isinstance(previous, dict) or previous.get("status") != "done":
        return False
    if any(previous.get(key) != value for key, value in current.items()):
        return False
    return now() - float(previous.get("finished_epoch", 0)) < max_age_days * 86400


# [@ANCHOR: mutation_run:parse_harness_log]
def parse_harness_log(text):
    """`{mutants, killed, run, partial, survivors}` from the harness's output: `N mutants`, `score K/R = P%`, `PARTIAL: ...` and one
    `SURVIVED line L: OPERATOR` per survivor. Missing pieces are 0 / []. Pure."""
    mutants = re.search(r"^(\d+) mutants", text, re.M)
    score = re.search(r"^score (\d+)/(\d+)", text, re.M)
    survivors = [{"line": int(m.group(1)), "operator": m.group(2).strip()} for m in re.finditer(r"^SURVIVED line (\d+): (.*)$", text, re.M)]
    return {"mutants": int(mutants.group(1)) if mutants else 0, "killed": int(score.group(1)) if score else 0,
            "run": int(score.group(2)) if score else 0, "partial": bool(re.search(r"^PARTIAL:", text, re.M)), "survivors": survivors}


# [@ANCHOR: mutation_run:build_result]
def build_result(target, parsed, status, seconds, log, unchanged, now=time.time):
    """The result document for one target (see the module text). `score` is killed / run in percent (None when nothing ran)."""
    run = parsed["run"]
    document = {
        "name": target["name"], "file": target["file"], "status": status, "mutants": parsed["mutants"], "run": run,
        "killed": parsed["killed"], "survived": len(parsed["survivors"]), "not_run": max(0, parsed["mutants"] - run),
        "score": round(100.0 * parsed["killed"] / run, 1) if run else None,
        "survivors": [{"file": target["file"], "line": s["line"], "operator": s["operator"]} for s in parsed["survivors"]],
        "seconds": round(seconds, 1), "test": target.get("test", ""), "log": log, "original_unchanged": unchanged,
        "finished_epoch": now(), "finished": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now())),
    }
    document.update(fingerprint(target))
    return document


def copy_tree(root, destination):
    """rsync copy of `root` into `destination` (a real copy, no hardlinks to the live checkout, symlinks kept as symlinks)."""
    command = ["rsync", "-a"] + ["--exclude=" + pattern for pattern in EXCLUDES] + [root.rstrip("/") + "/", destination.rstrip("/") + "/"]
    done = subprocess.run(command, capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError("rsync failed: " + done.stderr.strip()[-300:])


# [@ANCHOR: mutation_run:run_worker]
def run_worker(spec_path):
    """Worker mode (started by `ram_dirs.py run` inside the job): copy the tree into `$CARGO_TARGET_DIR/src`, run the harness on the copy
    (as a job of its own, with the budget), parse its output and write the result file named in the spec. Returns the exit status."""
    target = common.read_json(spec_path)
    scratch = os.environ.get("CARGO_TARGET_DIR")
    if not target or not scratch:
        print("mutation_run worker: no spec or no scratch directory", file=sys.stderr)
        return 2
    started = time.time()
    unchanged, status, log = None, "error", ""
    parsed = {"mutants": 0, "killed": 0, "run": 0, "partial": False, "survivors": []}
    live = os.path.join(target["root"], target["file"])
    before = common.sha256_file(live) if os.path.isfile(live) else None
    try:
        source = os.path.join(scratch, "src")
        copy_tree(target["root"], source)
        harness = [sys.executable, os.path.join(HERE, "mutation_harness.py"), "--root", source, "--file", target["file"], "--cwd", target.get("cwd", "."),
                   "--test", target["test"], "--workers", str(target.get("workers", 4)), "--timeout", str(target.get("timeout", 120)),
                   "--work", os.path.join(scratch, "work"), "--budget", str(float(target.get("budget_minutes", DEFAULT_BUDGET_MINUTES)) * 60),
                   "--out", os.path.join(scratch, "raw.json")]
        if target.get("funcs"):
            harness += ["--funcs", ",".join(target["funcs"])]
        outcome = common.run_job("mut-" + common.slug(target["name"]), harness, timeout=float(target.get("budget_minutes", DEFAULT_BUDGET_MINUTES)) * 90 + 600)
        log = outcome.log
        text = open(log, encoding="utf-8", errors="replace").read() if os.path.exists(log) else ""
        parsed = parse_harness_log(text)
        if "BASELINE FAILS" in text:
            status = "baseline_fails"
        elif outcome.status == "timeout":
            status = "timeout"
        elif outcome.status == "ok":
            status = "partial" if parsed["partial"] else "done"
    except (OSError, RuntimeError) as error:
        print("mutation_run worker: %s" % error, file=sys.stderr)
    after = common.sha256_file(live) if os.path.isfile(live) else None
    unchanged = before == after
    if not unchanged:
        status = "error"
    common.write_json(target["result"], build_result(target, parsed, status, time.time() - started, log, unchanged))
    return 0 if status in ("done", "partial") else 1


# [@ANCHOR: mutation_run:run_target]
def run_target(target, out_dir, force=False, max_age_days=7.0, dry_run=False, now=time.time):
    """Runs one target and returns `(status, result path)`: `skipped` (a fresh result exists), `dry-run`, `missing` (the file is not there) or the result's status."""
    path = result_path(out_dir, target["name"])
    for key in ("root", "file", "test"):
        if not target.get(key):
            raise ValueError("target %s has no `%s`" % (target["name"], key))
    if not os.path.isfile(os.path.join(target["root"], target["file"])):
        common.write_json(path, dict(build_result(target, {"mutants": 0, "killed": 0, "run": 0, "partial": False, "survivors": []}, "error", 0, "", None, now),
                                     detail="the target file does not exist"))
        return "missing", path
    if not force and is_fresh(common.read_json(path), fingerprint(target), max_age_days, now):
        return "skipped", path
    if dry_run:
        return "dry-run", path
    try:
        os.unlink(path)  # an older result must not stand in for a run that dies before it writes one
    except FileNotFoundError:
        pass
    spec = dict(target, result=path)
    spec_path = os.path.join(out_dir, ".spec-%s.json" % common.slug(target["name"]))
    common.write_json(spec_path, spec)
    ram_dirs = os.path.join(HERE, "ram_dirs.py")
    budget = float(target.get("budget_minutes", DEFAULT_BUDGET_MINUTES))
    outcome = common.run_job("mutrun-" + common.slug(target["name"]),
                             [sys.executable, ram_dirs, "run", "mutation-" + common.slug(target["name"]), "--",
                              "nice", "-n", "10", sys.executable, os.path.abspath(__file__), "--worker", spec_path],
                             timeout=budget * 90 + 1200)
    try:
        os.unlink(spec_path)
    except OSError:
        pass
    result = common.read_json(path)
    if outcome.status == "timeout" or not isinstance(result, dict):
        common.write_json(path, dict(build_result(target, {"mutants": 0, "killed": 0, "run": 0, "partial": False, "survivors": []},
                                                  "timeout" if outcome.status == "timeout" else "error", 0, outcome.log, None, now),
                                     detail=outcome.text or "the worker wrote no result"))
        return ("timeout" if outcome.status == "timeout" else "error"), path
    return result.get("status", "error"), path


# [@ANCHOR: mutation_run:summary_line]
def summary_line(results):
    """One line: `mutation: 3 target(s): 2 done, 1 skipped; survivors 4`, from `[(name, status, path)]` and the result files."""
    counts = {}
    survivors = 0
    for _, status, path in results:
        counts[status] = counts.get(status, 0) + 1
        document = common.read_json(path, {})
        survivors += int(document.get("survived", 0)) if status in ("done", "partial") else 0
    return "mutation: %d target(s): %s; survivors %d" % (len(results), ", ".join("%d %s" % (n, s) for s, n in sorted(counts.items())) or "none", survivors)


# [@ANCHOR: mutation_run:main]
def main(argv=None, load=common.read_load):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("targets", nargs="?")
    parser.add_argument("--worker", help="internal: run one target from this spec file")
    parser.add_argument("--out-dir")
    parser.add_argument("--only")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--max-age-days", type=float, default=7.0)
    parser.add_argument("--budget-total", type=float, default=0.0, help="minutes: start no new target after this long (0 = no limit)")
    parser.add_argument("--dry-run", action="store_true")
    options = parser.parse_args(argv)
    if options.worker:
        return run_worker(options.worker)
    if not options.targets:
        parser.error("a targets file is required")
    out_dir = options.out_dir or os.path.join(common.reports_dir(), OUT_SUBDIR)
    log_path = os.path.join(common.reports_dir(), "overnight.log")
    try:
        targets = common.load_targets(options.targets, required=("name", "root", "file", "test"))
    except ValueError as error:
        print("mutation_run: %s" % error, file=sys.stderr)
        return 2
    if options.only:
        wanted = set(options.only.split(","))
        targets = [t for t in targets if t["name"] in wanted]
    results, started, failed = [], time.time(), False
    for target in targets:
        reason = load().critical()
        if reason and not options.dry_run:
            common.log_line(log_path, "mutation: stopped before %s: %s" % (target["name"], reason))
            print("mutation_run: %s" % reason, file=sys.stderr)
            if not results:
                return common.EXIT_NOT_IDLE
            break
        if options.budget_total and time.time() - started > options.budget_total * 60:
            break
        os.makedirs(out_dir, exist_ok=True)
        status, path = run_target(target, out_dir, options.force, options.max_age_days, options.dry_run)
        results.append((target["name"], status, path))
        failed = failed or status in ("error", "timeout", "baseline_fails", "missing")
        common.log_line(log_path, "mutation: %s %s -> %s" % (target["name"], status, path))
    line = summary_line(results)
    common.log_line(log_path, line)
    print(line)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
