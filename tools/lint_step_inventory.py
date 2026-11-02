#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Coverage proof for the lint step list: which tests each pytest step collects, and whether any test
is collected by more than one step.

    lint_step_inventory.py collect STEPS_JSON OUT_JSON [--tree DIR] [--jobs N]
        STEPS_JSON is `run_linters.py --list-json`. Runs every pytest step's own command with
        `--collect-only -q` (same interpreter, cwd and environment) and stores
        {step name: sorted test ids}. Test ids are made absolute and written relative to --tree so a
        "before" and an "after" taken in different worktrees compare equal.
    lint_step_inventory.py compare BEFORE_JSON AFTER_JSON
        Exit 0 when the union of collected test ids is identical and no id is collected twice in
        AFTER; prints the difference otherwise. Step names may differ between the two (steps are
        merged), the SET OF TESTS may not.
    lint_step_inventory.py duplicates OUT_JSON
        Lists every test id that two or more steps collect.
    lint_step_inventory.py table STEPS_JSON [OUT_JSON]
        Markdown table of steps: what each runs, its lock, and (with OUT_JSON) how many tests.

Heavy by design (it imports every test module); run it with the machine quiet.
"""

import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor


def is_pytest_step(step):
    argv = step.get("argv") or []
    return "pytest" in argv


def _collect_one(step, tree):
    argv = list(step["argv"])
    index = argv.index("pytest")
    cwd = step.get("cwd") or os.getcwd()
    command = argv[: index + 1] + ["--collect-only", "-q", "-p", "no:cacheprovider", f"--rootdir={cwd}"]
    command += [a for a in argv[index + 1 :] if a != "-q"]
    env = dict(os.environ)
    env.update(step.get("env_delta") or {})
    res = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True)
    ids = []
    real_tree = os.path.realpath(tree) + os.sep
    for line in res.stdout.splitlines():
        if "::" not in line or line.startswith((" ", "=")):
            continue
        path, _, rest = line.partition("::")
        absolute = os.path.realpath(os.path.join(cwd, path))
        if absolute.startswith(real_tree):
            absolute = absolute[len(real_tree) :]
        ids.append(f"{absolute}::{rest}")
    summary = res.stdout.strip().splitlines()[-1] if res.stdout.strip() else ""
    return step["name"], sorted(ids), res.returncode, summary, res.stderr[-400:]


def cmd_collect(args):
    with open(args.steps, encoding="utf-8") as handle:
        steps = [s for s in json.load(handle) if is_pytest_step(s)]
    out = {}
    failed = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for name, ids, code, summary, err in pool.map(lambda s: _collect_one(s, args.tree), steps):
            out[name] = ids
            # pytest exit 5 = nothing collected, 0 = ok; anything else means a collection error.
            if code not in (0, 5):
                failed.append(f"{name}: exit {code}: {summary} {err}")
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(out, handle, indent=0, sort_keys=True)
    total = sum(len(v) for v in out.values())
    print(f"{len(out)} pytest steps, {total} test ids -> {args.out}")
    for line in failed:
        print("COLLECTION ERROR", line)
    return 1 if failed else 0


def _union(data):
    seen = {}
    for name, ids in data.items():
        for test_id in ids:
            seen.setdefault(test_id, []).append(name)
    return seen


def duplicates_of(data):
    return {test_id: names for test_id, names in _union(data).items() if len(names) > 1}


def cmd_duplicates(args):
    with open(args.collected, encoding="utf-8") as handle:
        data = json.load(handle)
    dup = duplicates_of(data)
    for test_id, names in sorted(dup.items()):
        print(f"{test_id}  <- {', '.join(names)}")
    print(f"{len(dup)} test ids collected by more than one step")
    return 1 if dup else 0


def compare(before, after):
    """(problems, before_count, after_count): empty problems when the sets are identical and no
    id is collected twice in `after`."""
    left, right = set(_union(before)), set(_union(after))
    problems = []
    for test_id in sorted(left - right):
        problems.append(f"LOST after the change: {test_id}")
    for test_id in sorted(right - left):
        problems.append(f"NEW after the change: {test_id}")
    for test_id, names in sorted(duplicates_of(after).items()):
        problems.append(f"DUPLICATE in after ({', '.join(names)}): {test_id}")
    return problems, len(left), len(right)


def cmd_compare(args):
    with open(args.before, encoding="utf-8") as handle:
        before = json.load(handle)
    with open(args.after, encoding="utf-8") as handle:
        after = json.load(handle)
    problems, left, right = compare(before, after)
    for line in problems:
        print(line)
    print(f"before: {left} tests in {len(before)} steps; after: {right} tests in {len(after)} steps")
    if problems:
        return 1
    print("IDENTICAL: same set of tests, each collected exactly once")
    return 0


def cmd_table(args):
    with open(args.steps, encoding="utf-8") as handle:
        steps = json.load(handle)
    counts = {}
    if args.collected:
        with open(args.collected, encoding="utf-8") as handle:
            counts = {k: len(v) for k, v in json.load(handle).items()}
    print("| step | runs | lock | tests |")
    print("|---|---|---|---|")
    for step in steps:
        argv = step.get("argv") or []
        runs = " ".join(os.path.basename(a) if a.startswith("/") else a for a in argv[:4])
        print(f"| {step['name']} | `{runs}` | {step.get('lock') or ''} | {counts.get(step['name'], '')} |")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    collect = sub.add_parser("collect")
    collect.add_argument("steps")
    collect.add_argument("out")
    collect.add_argument("--tree", default=os.getcwd())
    collect.add_argument("--jobs", type=int, default=2)
    collect.set_defaults(func=cmd_collect)
    dup = sub.add_parser("duplicates")
    dup.add_argument("collected")
    dup.set_defaults(func=cmd_duplicates)
    cmp_ = sub.add_parser("compare")
    cmp_.add_argument("before")
    cmp_.add_argument("after")
    cmp_.set_defaults(func=cmd_compare)
    table = sub.add_parser("table")
    table.add_argument("steps")
    table.add_argument("collected", nargs="?")
    table.set_defaults(func=cmd_table)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
