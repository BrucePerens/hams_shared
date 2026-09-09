#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Anchor/Coverage Cross-Reference (ADR 0090 decision 4, Stage 3 of
ANCHOR_COVERAGE_AND_REMEDIATION_PLAN.md)

Stage 1 (`check_function_test_anchors.py`) only asks "does this function cite an anchor at all" --
a static, declarative claim. It cannot see a function whose anchor cites a real test that only
exercises one branch, or never actually calls into it along the path the citation implies. Stage 2
(`hams_shared/tools/test.py --coverage`) produces real per-line execution data. This script is
Stage 3: join the two, and surface anchored functions whose own line span was never actually
executed by any real test run -- the gap neither mechanism alone can see.

ADR 0090 decision 4, verbatim: "binary ('was this span touched at all by any test') for the first
real gate, with branch coverage as the real, named follow-on once Stage 2 produces actual data to
check it against." This script implements exactly that binary gate -- not a percentage threshold,
which the ADR explicitly considered and rejected as penalizing normal defensive/fail-fast code.

Scope note, real and load-bearing: a `coverage.json` produced by a partial test run (e.g. `-u
some_module`) only contains real per-line data for files actually imported during that run. A file
absent from `coverage.json["files"]` was outside that run's own scope, not proven uncovered by any
test anywhere -- reporting it as a gap would be noise, not signal. This script only reports on files
`coverage.json` actually has data for, silently skipping everything else. A full-tree, no-`-u` run
is what makes this report meaningful across the whole repo; a scoped run makes it meaningful only
for the modules that run actually exercised, and the report says so.

Python only, matching `check_function_test_anchors.py`'s own scope exactly (same
`_git_tracked_python_files`/`_direct_functions`/`_function_span` helpers, imported directly rather
than re-implemented, so the two stages can never silently drift out of agreement on what a function
even is). Rust/JS coverage cross-referencing is real, separate, not-yet-started follow-on work, per
ADR 0090 decision 2 and the plan's own Stage 2 notes on those sub-tracks.

This is a reporting tool, not (yet) a CI gate -- Stage 4 (remediation) hasn't run yet, so gating on
this today would fail CI on every pre-existing gap with no way to grandfather them in the way
Stage 1's own baseline does. Wiring a baseline/ratchet for this stage is real, sequenced follow-on
work for whenever Stage 4 actually starts closing these gaps, not assumed done here.
"""

import argparse
import ast
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_function_test_anchors as cfta  # noqa: E402
import verify_anchors as va  # noqa: E402


def load_coverage_files(coverage_json_path):
    """Returns coverage.py's own {relpath: {"executed_lines": [...],
    "missing_lines": [...]}} map, straight from its JSON report -- the
    file already keys by repo-relative path when `relative_files = True`
    was set in the rcfile that produced it (Stage 2's own convention,
    confirmed by direct inspection to match `_git_tracked_python_files`'s
    own `os.path.relpath` keys exactly)."""
    with open(coverage_json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("files", {})


def check_anchor_coverage(repo_root, coverage_files):
    """Returns a list of (identity, start, end) for every anchored
    function whose own line span coverage.json has real statement data
    for (i.e. the file was actually in this run's own scope), but none of
    those statement lines executed -- ADR 0090 decision 4's binary
    "touched at all" gate. A function with no statement lines in scope at
    all (an abstract stub, a docstring-only body, or a file outside this
    run's own coverage scope) is silently skipped -- there's nothing this
    run could have told us about it either way."""
    gaps = []
    for filepath in cfta._git_tracked_python_files(repo_root):
        rel_path = os.path.relpath(filepath, repo_root)
        file_cov = coverage_files.get(rel_path)
        if file_cov is None:
            continue
        executed = set(file_cov.get("executed_lines", []))
        missing = set(file_cov.get("missing_lines", []))
        statement_lines = executed | missing
        if not statement_lines:
            continue
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
        except (OSError, UnicodeDecodeError):
            continue
        try:
            tree = ast.parse(content, filename=filepath)
        except SyntaxError:
            continue
        lines = content.splitlines()
        for qualname, node in cfta._direct_functions(tree.body, []):
            start, end = cfta._function_span(node, lines)
            span_lines = lines[start - 1 : min(end, len(lines))]
            # A bare `ANCHOR_PATTERN.search` over the joined span would count a mere citation --
            # `# Verified by [@ANCHOR: name]`, `# Tests [@ANCHOR: name]` -- as if it were a real
            # base anchor declaration, wrongly pulling an actually-unanchored function into this
            # stage's "anchored but unexecuted" report instead of leaving it to Stage 1.
            has_anchor = any(
                cfta._is_base_anchor_declaration(line, m)
                for line in span_lines
                for m in va.ANCHOR_PATTERN.finditer(line)
            )
            if not has_anchor:
                continue  # Stage 1's own job, not this stage's
            span_statements = {
                ln for ln in range(start, end + 1) if ln in statement_lines
            }
            if not span_statements:
                continue
            if not (span_statements & executed):
                identity = f"{rel_path}::{qualname}"
                gaps.append((identity, start, end))
    return gaps


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--coverage-json",
        required=True,
        help="Path to a coverage.json report produced by "
        "`hams_shared/tools/test.py --coverage` (ADR 0090 Stage 2).",
    )
    parser.add_argument(
        "--repo-root",
        default=os.getcwd(),
        help="Repo root to scan (default: current directory).",
    )
    args = parser.parse_args()

    coverage_files = load_coverage_files(args.coverage_json)
    if not coverage_files:
        print("[!] ERROR: coverage.json has no per-file data -- nothing to cross-reference.")
        return 1

    gaps = check_anchor_coverage(args.repo_root, coverage_files)
    scoped_file_count = len(coverage_files)
    print(
        f"[i] Cross-referenced anchors against {scoped_file_count} file(s) this "
        f"coverage run actually exercised."
    )
    if not gaps:
        print("[+] No anchored function's span was left fully unexecuted. Clean.")
        return 0

    print(
        f"\n[!] {len(gaps)} anchored function(s) claim a test but were never "
        f"executed at all during this run (ADR 0090 decision 4, binary threshold):\n"
    )
    for identity, start, end in sorted(gaps):
        print(f"  {identity} (lines {start}-{end})")
    return 1


if __name__ == "__main__":
    sys.exit(main())
