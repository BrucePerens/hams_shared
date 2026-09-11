#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
JS Test-Only Hook Gating Check (ADR 0094)

Both real Service Workers in this codebase used to carry `TEST_*`-prefixed `postMessage` branches
reachable in every production deployment -- any same-origin script (a browser extension, a
compromised dependency, an XSS) could set a forced-failure flag on a live production Service
Worker with zero attacker sophistication required. ADR 0094 mandates that any such branch be
gated behind a `TEST_HOOKS_ENABLED` guard-and-return, substituted server-side from a real,
deliberate, per-deployment `ir.config_parameter` -- never Odoo's own `test_enable` (a banned
pattern after an LLM coding agent previously exploited automatic test/prod behavior branching to
fake passing tests, see `ham_repeater_dir/models/ham_repeater_import.py`'s own comment).

This check uses a real AST parse (via `acorn`, through `js_test_hook_gating_scan.cjs`), not a
regex approximation -- the exact same split-responsibility pattern
`check_js_function_test_anchors.py`/`js_function_scan.cjs` already established for ADR 0090's JS
sub-track. No baseline/ratchet: this is a brand-new rule with exactly two known real instances at
introduction (`sw.js`, `shack_sw.js`), both already fixed and gated per ADR 0094 before this
checker was written -- any violation found is real, new, and must be fixed immediately, not
grandfathered.
"""

import json
import os
import subprocess
import sys

EXCLUDE_DIRS = {
    ".git",
    "node_modules",
    "tools",
    "scripts",
    "migrations",
    "lib",  # vendored third-party JS, matching eslint.config.js's own static/lib/ ignore
    "hams_community",
    "hams_com",
    "cloudflared",
}

_JS_SCAN_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "js_test_hook_gating_scan.cjs")


def _git_tracked_js_files(repo_root):
    """Only git-tracked .js files -- same real-world-safety reason every sibling checker in this
    tool family uses `git ls-files` over a raw `os.walk`."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "*.js"],
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
        if basename.endswith(".test.js") or basename.endswith(".min.js"):
            continue
        files.append(os.path.join(repo_root, rel))
    return files


def _run_js_scan(filepaths):
    """Batches every file into one real Node/acorn invocation, matching
    check_js_function_test_anchors.py's own `_run_js_scan` precedent. Returns
    {filepath: [{"line": int, "literal": str}, ...]}, silently omitting any file acorn itself
    failed to parse (a real syntax error is check_js_syntax.py's job to catch, not this one's)."""
    if not filepaths:
        return {}
    proc = subprocess.run(
        ["node", _JS_SCAN_SCRIPT],
        input=json.dumps(filepaths),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        return {}
    try:
        results = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}
    return {entry["file"]: entry["violations"] for entry in results}


def scan_tree(repo_root):
    """Returns {relpath: [{"line": int, "literal": str}, ...]} for every real violation found
    across the whole repo."""
    all_js_files = _git_tracked_js_files(repo_root)
    violations_by_file = _run_js_scan(all_js_files)
    return {
        os.path.relpath(filepath, repo_root): violations
        for filepath, violations in violations_by_file.items()
        if violations
    }


def main():
    if len(sys.argv) < 2:
        print("usage: check_js_test_hook_gating.py <repo_root>", file=sys.stderr)
        return 2

    repo_root = os.path.abspath(sys.argv[1])
    violations_by_file = scan_tree(repo_root)

    if violations_by_file:
        print("[!] CI/CD FAILURE: Unguarded TEST_* postMessage/event hook(s) detected (ADR 0094):")
        for relpath, violations in sorted(violations_by_file.items()):
            for v in violations:
                line = v.get("line")
                literal = v.get("literal")
                print(f"    - {relpath}:{line}: branch on '{literal}' with no preceding "
                      f"`if (!TEST_HOOKS_ENABLED) return;` guard in the same block.")
        print(
            "      [!] DIAGNOSTIC FOR AI: A TEST_*-prefixed message/event branch is reachable by "
            "any same-origin script in every production deployment unless it is gated. Add a "
            "`const TEST_HOOKS_ENABLED = __TEST_HOOKS_ENABLED__;` declaration (substituted "
            "server-side from a real ir.config_parameter, e.g. caching.enable_sw_test_hooks -- "
            "see ADR 0094), and place `if (!TEST_HOOKS_ENABLED) return;` before the first TEST_* "
            "branch in the same block. Never gate this on Odoo's own test_enable flag."
        )
        return 1

    print("[+] SUCCESS: No unguarded TEST_* postMessage/event hooks found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
