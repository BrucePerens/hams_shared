#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Rust coverage instrumentation (ANCHOR_COVERAGE_AND_REMEDIATION_PLAN.md Stage 2's Rust sub-track)

The Rust analogue of `test.py --coverage`'s Python sub-track: runs a real test suite under
`cargo llvm-cov` for one crate and writes a repo-relative-path-keyed JSON report, in the same
`{"files": {relpath: {"executed_lines": [...], "missing_lines": [...]}}}` shape Stage 3's own
cross-reference against `check_function_test_anchors.py`-style anchor keys will need to join
against -- keys chosen to match `os.path.relpath(filepath, repo_root)`, the exact convention the
Python sub-track's own `relative_files = True` rcfile setting was verified to already produce.

**Real environmental finding this spike surfaced, not assumed**: `cargo llvm-cov`'s own
`--remap-path-prefix` flag produces file paths relative to the crate's own manifest directory
(e.g. `src/ambe/decode.rs`), not repo-relative -- there is no cargo-llvm-cov flag that produces
repo-relative paths directly, since cargo-llvm-cov has no concept of a "repo root" above the crate
it's building. This script does the join itself: `os.path.relpath(crate_dir, repo_root)` prepended
to each crate-relative path, verified against the real `ham_digital_modes` crate (nested three
directories below `hams_open`'s own root) to land on exactly `daemons/ham_digital_modes/src/ambe/
decode.rs`, the same key shape `rust_function_test_anchor_baseline_hams_open.json` already uses.

**Why LCOV, not the native JSON report**: `cargo llvm-cov --json` reports coverage as raw per-region
"segments" (line, column, execution count, and several boolean flags) -- reconstructing accurate
per-line executed/missing status from that by hand is exactly the kind of "silently subtly wrong"
derivation this codebase's own "verified, not assumed" discipline warns against. `--lcov` instead
asks `cargo llvm-cov` itself (backed by LLVM's own well-established coverage-report machinery, the
same one every other LCOV consumer trusts) to do that line-level reduction, emitting the standard,
widely-used `DA:<line>,<count>` record format this script parses directly -- simpler and more
trustworthy than re-deriving the same computation independently.

Crate-agnostic by design: takes one crate directory per invocation (mirroring `test.py --coverage`'s
own per-target-module scope, not a whole-repo sweep) since this repo's own real crates differ
enough in build time and test shape (see `ANCHOR_COVERAGE_AND_REMEDIATION_PLAN.md`'s own Stage 2
section) that a single blind "cover everything" invocation isn't the right default; which crates to
run, and in what order/parallelism, is Stage 3/CI's own orchestration question, not this script's.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

DA_RE = re.compile(r"^DA:(\d+),(-?\d+)")
SF_RE = re.compile(r"^SF:(.+)$")


def find_repo_root(start_dir):
    """Walks upward from `start_dir` looking for a `.git` entry. Best-effort only -- a crate that
    lives under a submodule-style nested checkout (e.g. `hams_shared` vendored inside both
    `hams_open` and `hams_com`) has more than one real "repo root" a caller might mean, so callers
    that care which one should pass `--repo-root` explicitly rather than trust this."""
    current = os.path.abspath(start_dir)
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def run_llvm_cov_lcov(crate_dir, release=True):
    """Runs `cargo llvm-cov` for the crate at `crate_dir`, real test suite included, and returns
    the raw LCOV text. Returns None on any real build/test failure -- a coverage report from a
    crate that doesn't even build cleanly would be actively misleading, not a degraded-but-useful
    partial result."""
    with tempfile.NamedTemporaryFile(suffix=".lcov", delete=False) as tmp:
        lcov_path = tmp.name
    try:
        cmd = ["cargo", "llvm-cov"]
        if release:
            cmd.append("--release")
        cmd.extend(["--remap-path-prefix", "--lcov", "--output-path", lcov_path])
        proc = subprocess.run(cmd, cwd=crate_dir, capture_output=True, text=True)
        if proc.returncode != 0:
            print(proc.stdout, file=sys.stderr)
            print(proc.stderr, file=sys.stderr)
            return None
        with open(lcov_path, "r", encoding="utf-8") as f:
            return f.read()
    finally:
        try:
            os.unlink(lcov_path)
        except OSError:
            pass


def parse_lcov(lcov_text):
    """Real LCOV `SF:`/`DA:`/`end_of_record` parsing -- returns
    {crate_relative_path: {"executed_lines": [...], "missing_lines": [...]}}, sorted ascending.
    A `DA:` count > 0 is executed; 0 or negative is missing, matching `coverage.py`'s own
    "count > 0 is covered" convention so the two languages' reports stay comparable. (An earlier
    version of this doc comment speculated `cargo llvm-cov` "sometimes emits" negative counts for
    unreachable code -- checked directly against a real report from this repo's own
    `ham_digital_modes` crate and found zero negative `DA:` records anywhere; that claim was never
    actually verified and is dropped here rather than repeated as if it were.)

    Tracks the highest count seen per line rather than the first/last, so a real line appearing in
    more than one `DA:` record for the same file (never observed from a real `cargo llvm-cov` run
    against this repo, but not something the LCOV format itself forbids) can't land in both
    `executed_lines` and `missing_lines` at once -- caught by
    `test_a_repeated_da_record_for_the_same_line_never_lands_in_both_buckets`
    (`test_run_rust_coverage.py`), matching real LCOV merge semantics (`lcov --add-tracefile` sums
    repeated records for the same line -- any positive contribution makes it covered)."""
    files = {}
    current_file = None
    for line in lcov_text.splitlines():
        sf_match = SF_RE.match(line)
        if sf_match:
            current_file = sf_match.group(1)
            files.setdefault(current_file, {})
            continue
        if line == "end_of_record":
            current_file = None
            continue
        da_match = DA_RE.match(line)
        if da_match and current_file is not None:
            line_no, count = int(da_match.group(1)), int(da_match.group(2))
            file_lines = files[current_file]
            file_lines[line_no] = max(count, file_lines.get(line_no, count))

    return {
        path: {
            "executed_lines": sorted(ln for ln, count in line_counts.items() if count > 0),
            "missing_lines": sorted(ln for ln, count in line_counts.items() if count <= 0),
        }
        for path, line_counts in files.items()
    }


def to_repo_relative(per_file, crate_dir, repo_root):
    """Prepends `crate_dir`'s own path relative to `repo_root` onto every crate-relative LCOV
    path, landing on the same `os.path.relpath(filepath, repo_root)` key shape
    `check_function_test_anchors.py`-style scanners already use."""
    crate_rel = os.path.relpath(os.path.abspath(crate_dir), repo_root)
    return {os.path.normpath(os.path.join(crate_rel, path)): data for path, data in per_file.items()}


def default_output_path():
    return os.path.join(os.path.expanduser("~/tmp"), "coverage_report", "rust_coverage.json")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("crate_dir", help="Directory containing the crate's own Cargo.toml")
    parser.add_argument(
        "--repo-root",
        default=None,
        help="Repo root to make file paths relative to (default: nearest .git above crate_dir)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help=f"Output JSON path (default: {default_output_path()})",
    )
    parser.add_argument(
        "--debug-build",
        action="store_true",
        help="Use a debug build instead of --release (faster to build, slower to run)",
    )
    args = parser.parse_args()

    repo_root = args.repo_root or find_repo_root(args.crate_dir)
    if repo_root is None:
        print(
            f"[!] ERROR: could not find a .git above {args.crate_dir!r} -- pass --repo-root explicitly",
            file=sys.stderr,
        )
        return 1

    lcov_text = run_llvm_cov_lcov(args.crate_dir, release=not args.debug_build)
    if lcov_text is None:
        print(f"[!] ERROR: cargo llvm-cov failed for {args.crate_dir}", file=sys.stderr)
        return 1

    per_file = parse_lcov(lcov_text)
    per_file = to_repo_relative(per_file, args.crate_dir, repo_root)

    output_path = args.output or default_output_path()
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({"files": per_file}, f, indent=2, sort_keys=True)
        f.write("\n")

    total_lines = sum(len(d["executed_lines"]) + len(d["missing_lines"]) for d in per_file.values())
    total_executed = sum(len(d["executed_lines"]) for d in per_file.values())
    pct = (100.0 * total_executed / total_lines) if total_lines else 0.0
    print(
        f"[+] Coverage report written to {output_path} "
        f"({len(per_file)} files, {total_executed}/{total_lines} lines covered, {pct:.1f}%)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
