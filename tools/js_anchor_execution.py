#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
JS anchor execution report (ANCHOR_COVERAGE_AND_REMEDIATION_PLAN.md Stage 3, JS half)

Cross-references the `[@ANCHOR: ...]` declarations in JS source with real V8 coverage, at function
level (NIGHT_PLAN decision 79: file-and-function granularity is enough). Input is the document
`run_js_coverage.py --coverage-dir ...` prints (its `functions` key: `{relpath: {"functions": {name:
{"executed", "total"}}, "anonymous": {...}}}`), collected from real tour runs. For every anchor that
sits inside a named function (the same function units `check_js_function_test_anchors.py` scans,
through the same acorn scanner), the report says one of:

- `executed`: the function ran in at least one collected tour.
- `not_executed`: the file was loaded and V8 reported the function, but it never ran.
- `unattributed`, with a reason: the anchor is outside any named function; or its file is not in the
  coverage data at all (no collected run loaded it); or its file is covered but V8 reported no
  function of that name (minified output renames some functions, and a function that was never
  instantiated has no entry).

V8 names a class method by its bare method name, so `Class.method` is looked up as `method`. Two
functions of the same bare name in one file share a counter: the lookup reports executed when any of
them ran, and a name shared by several functions is flagged `ambiguous_name` so a reader knows the
answer is per name, not per function.

Usage:
    run_js_coverage.py --coverage-dir D --addons-path A --repo-root R > coverage.json
    js_anchor_execution.py --coverage coverage.json --repo-root R [--json]
"""

import argparse
import json
import os
import sys

import verify_anchors as va  # noqa: E402
from check_function_test_anchors import _is_base_anchor_declaration  # noqa: E402
from check_js_function_test_anchors import _extend_span_backward_over_comments, _run_js_scan  # noqa: E402


def anchors_in_file(filepath):
    """Returns `(function_anchors, loose_anchors)` for one JS file. `function_anchors` is a list of
    `(anchor, qualname, short_name)`; `loose_anchors` is the anchors not inside any named function."""
    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    scanned = _run_js_scan([filepath]).get(filepath)
    if scanned is None:
        return [], []
    spans = []
    for qualname, start, end in scanned:
        spans.append((_extend_span_backward_over_comments(start, lines), end, qualname))
    found = []
    for index, line in enumerate(lines):
        prev = lines[index - 1] if index else ""
        for match in va.ANCHOR_PATTERN.finditer(line):
            if _is_base_anchor_declaration(line, match, prev):
                found.append((index + 1, match.group(1)))
    function_anchors, loose = [], []
    for line_number, anchor in found:
        owner = next((q for s, e, q in spans if s <= line_number <= e), None)
        if owner is None:
            loose.append(anchor)
        else:
            function_anchors.append((anchor, owner, owner.rsplit(".", 1)[-1]))
    return function_anchors, loose


def build_report(coverage_doc, repo_root, scanned_files=None):
    """`coverage_doc` is the `run_js_coverage.py` output. Every JS file under `repo_root` that
    declares an anchor is checked (`scanned_files`, relative paths, restricts that for tests)."""
    covered = coverage_doc.get("functions", {})
    report = {"executed": [], "not_executed": [], "unattributed": []}
    for rel in scanned_files if scanned_files is not None else _js_files_with_anchors(repo_root):
        function_anchors, loose = anchors_in_file(os.path.join(repo_root, rel))
        for anchor in loose:
            report["unattributed"].append(
                {"anchor": anchor, "file": rel, "reason": "anchor is outside any named function"}
            )
        names = [short for _a, _q, short in function_anchors]
        for anchor, qualname, short in function_anchors:
            entry = {"anchor": anchor, "file": rel, "function": qualname}
            if names.count(short) > 1:
                entry["ambiguous_name"] = True
            file_cov = covered.get(rel)
            if file_cov is None:
                entry["reason"] = "file is not in the coverage data (no collected run loaded it)"
                report["unattributed"].append(entry)
                continue
            counts = file_cov.get("functions", {}).get(short)
            if counts is None:
                entry["reason"] = "V8 reported no function of this name (renamed by minification, or never instantiated)"
                report["unattributed"].append(entry)
            elif counts["executed"] > 0:
                report["executed"].append(entry)
            else:
                report["not_executed"].append(entry)
    for key in report:
        report[key].sort(key=lambda e: (e["file"], e["anchor"]))
    return report


def _js_files_with_anchors(repo_root):
    from check_js_function_test_anchors import _git_tracked_js_files

    result = []
    for path in _git_tracked_js_files(repo_root):
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError):
            continue
        if "[@ANCHOR" in text:
            result.append(os.path.relpath(path, repo_root))
    return sorted(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--coverage", required=True, help="JSON printed by run_js_coverage.py --coverage-dir")
    parser.add_argument("--repo-root", default=os.getcwd())
    parser.add_argument("--json", action="store_true", help="print the whole report as JSON")
    args = parser.parse_args()
    with open(args.coverage, "r", encoding="utf-8") as f:
        coverage_doc = json.load(f)
    report = build_report(coverage_doc, os.path.abspath(args.repo_root))
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    for key in ("executed", "not_executed", "unattributed"):
        print(f"{key}: {len(report[key])}")
    for entry in report["not_executed"]:
        print(f"  NOT EXECUTED  {entry['file']}::{entry['function']}  [@ANCHOR: {entry['anchor']}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
