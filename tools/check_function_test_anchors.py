#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Function-Level Test-Anchor Ratchet (ADR 0090)

ANCHOR_COVERAGE_AND_REMEDIATION_PLAN.md's Stage 1 real open question 1, resolved by Bruce
directly, 2026-09-04: "I think we should test every function, we can afford to have an AI write
such tests." Real open question 5, resolved the same session: "institute the CI requirement for
new code now, we will do the sweep when we get to that proposal." This script is that CI
requirement -- a ratchet, not a sweep: every currently-unanchored function (as of the baseline
snapshot committed alongside this script) is grandfathered in, allowed to stay unanchored until
the real sweep closes it; any function added or modified to lose its anchor AFTER the baseline was
taken is a real, new CI failure.

Deliberately narrower than `verify_anchors.py`'s own full traceability check: this only asks "does
this function have a base anchor at all" (any of `[@ANCHOR: name]` / `[@ANCHOR-BEGIN: name]`
inside its own line span, including a leading comment or docstring), not whether that anchor
resolves to a real, verified test the way `verify_anchors.py`'s bidirectional check does -- that
deeper verification already runs separately, on whatever anchors DO exist. This check's only job
is "don't let the countable population of real gaps grow while nobody's watching."

Python only, for now -- `verify_anchors.py`'s own anchor-recognition already covers .py/.js/.xml/
.html; Rust (.rs) functions are not scanned by any anchor mechanism yet, a real, named, NOT YET
DONE follow-on (ANCHOR_COVERAGE_AND_REMEDIATION_PLAN.md's own real open question 2 chose to
pursue Python/Rust/JS coverage instrumentation together in Stage 2, but that is a different stage
from this one -- this script's own Rust support is unstarted, not silently assumed included).

Scope, matching the plan's own Stage 1 text: every module-level function and class method in a
git-tracked, non-test, non-tools/scripts .py file. Nested functions (closures defined inside
another function) are deliberately NOT walked -- they are not independently testable units the
way a module-level function or a class method is, and anchoring every closure would be noise, not
signal. `test_*.py` files themselves are not scanned for their own functions needing anchors --
the ANCHOR mandate is about the code under test, not the tests, which have their own `# Tests
[@ANCHOR: ...]`/`# [@ANCHOR: test_name]` convention `verify_anchors.py` already governs.
"""

import argparse
import ast
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import verify_anchors as va  # noqa: E402

EXCLUDE_DIRS = {
    ".git",
    "__pycache__",
    "tools",
    "scripts",
    "migrations",
    "node_modules",
    "hams_community",
    "hams_com",
}

DEFAULT_BASELINE_FILENAME = "function_test_anchor_baseline.json"


def _git_tracked_python_files(repo_root):
    """Only git-tracked .py files -- excludes build artifacts, venvs, and
    anything else sitting in the working tree that was never actually
    committed, the same real-world-safety reason every git-status-aware
    operation elsewhere in this codebase's own tooling uses `git
    ls-files` over a raw `os.walk`."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "*.py"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    files = []
    for rel in out.splitlines():
        parts = rel.split("/")
        if any(p in EXCLUDE_DIRS for p in parts):
            continue
        basename = os.path.basename(rel)
        if basename.startswith("test_") or basename == "__init__.py":
            continue
        files.append(os.path.join(repo_root, rel))
    return files


def _direct_functions(body, class_stack):
    """Yields (qualname, node) for every FunctionDef/AsyncFunctionDef
    directly in `body` (a module or class body) -- recurses into nested
    ClassDefs (real, if rare, Odoo mixin nesting), but deliberately does
    NOT descend into a FunctionDef's own body to find closures nested
    inside it, per this module's own doc comment on scope."""
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield ".".join(class_stack + [node.name]), node
        elif isinstance(node, ast.ClassDef):
            yield from _direct_functions(node.body, class_stack + [node.name])


def scan_file(filepath, repo_root):
    """Returns a list of (identity, has_anchor) for every in-scope
    function in `filepath`. `identity` is a stable string (relative path
    + qualified name) usable as a baseline dictionary key across runs."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
    except (OSError, UnicodeDecodeError):
        return []
    try:
        tree = ast.parse(content, filename=filepath)
    except SyntaxError:
        return []

    lines = content.splitlines()
    rel_path = os.path.relpath(filepath, repo_root)
    results = []
    for qualname, node in _direct_functions(tree.body, []):
        # `node.lineno` is the `def` line itself -- Python's AST does NOT
        # include decorator lines in it (confirmed directly: a real
        # @http.route-decorated method's own FunctionDef.lineno pointed
        # one line past its decorator). The anchor comment for a decorated
        # function idiomatically sits above the decorator, not between it
        # and `def` -- this codebase's own established convention, used
        # throughout this session -- so start from the EARLIEST decorator
        # (if any), then walk back further past any run of comment/blank
        # lines immediately preceding that, the same real structural
        # lookback check_burn_list.py's own _xml_audit_lookback_start uses
        # for an analogous reason. Without this, every decorated function
        # whose anchor sits above its decorator (the common case: @api.model,
        # @http.route, @api.depends, etc.) reads as a false-positive gap.
        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
        while start > 1:
            prev = lines[start - 2].strip()
            # Comment lines only -- deliberately NOT crossing a blank line,
            # which would risk absorbing an unrelated trailing comment
            # (e.g. a "# Verified by [@ANCHOR: ...]" belonging to the
            # PREVIOUS function/method, separated from this one by the
            # ordinary blank line between two defs) as if it were this
            # function's own anchor -- a false credit, not just a missed one.
            if prev.startswith("#"):
                start -= 1
            else:
                break
        end = getattr(node, "end_lineno", node.lineno)
        span = "\n".join(lines[start - 1 : min(end, len(lines))])
        has_anchor = bool(va.ANCHOR_PATTERN.search(span))
        identity = f"{rel_path}::{qualname}"
        results.append((identity, has_anchor))
    return results


def scan_tree(repo_root):
    """Returns {identity: True} for every in-scope function currently
    lacking a base anchor, across the whole real repo."""
    gaps = {}
    for filepath in _git_tracked_python_files(repo_root):
        for identity, has_anchor in scan_file(filepath, repo_root):
            if not has_anchor:
                gaps[identity] = True
    return gaps


def load_baseline(path):
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_baseline(path, gaps):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(gaps.keys()), f, indent=2)
        f.write("\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", nargs="?", default=".")
    parser.add_argument(
        "--baseline",
        default=None,
        help="Path to the baseline JSON file (default: alongside this script)",
    )
    parser.add_argument(
        "--generate-baseline",
        action="store_true",
        help="Regenerate the baseline from the current tree's real state instead of checking it",
    )
    args = parser.parse_args()

    repo_root = os.path.abspath(args.directory)
    baseline_path = args.baseline or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), DEFAULT_BASELINE_FILENAME
    )

    current_gaps = scan_tree(repo_root)

    if args.generate_baseline:
        save_baseline(baseline_path, current_gaps)
        print(f"[*] Baseline written: {len(current_gaps)} pre-existing unanchored functions recorded at {baseline_path}")
        return 0

    baseline = load_baseline(baseline_path)
    new_gaps = sorted(k for k in current_gaps if k not in baseline)

    if new_gaps:
        print("[!] CI/CD FAILURE: New Unanchored Functions Detected (ADR 0090):")
        for identity in new_gaps:
            print(f"    - {identity}")
        print(
            "      [!] DIAGNOSTIC FOR AI: This function is new (or was modified to lose its "
            "anchor) since the baseline was taken -- it is not grandfathered in. Add a real "
            "`# [@ANCHOR: name]` (or `[@ANCHOR-BEGIN:]`/`[@ANCHOR-END:]`) and a real test citing "
            "it with `# Tests [@ANCHOR: name]`, matching verify_anchors.py's own rules."
        )
        return 1

    print(f"[+] SUCCESS: No new unanchored functions ({len(current_gaps)} pre-existing, grandfathered, tracked in the baseline for the real sweep).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
