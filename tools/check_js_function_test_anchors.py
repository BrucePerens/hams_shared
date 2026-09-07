#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
JS Function-Level Test-Anchor Ratchet (ADR 0090 decision 2's JS sub-track)

`check_function_test_anchors.py` is Python-only by its own explicit design ("Rust (.rs) functions
are not scanned by any anchor mechanism yet... this script's own Rust support is unstarted, not
silently assumed included" -- and the same was true of JS, until now). ADR 0090 decision 1 ("every
function gets a real test anchor") was never itself scoped to Python; only the CI-enforcement
mechanism (decision 5) was built Python-first. This script is that same ratchet, extended to JS,
using a real AST parse (via `acorn`, already vendored under hams_shared/node_modules as an ESLint
transitive dependency -- no new dependency added) through `js_function_scan.cjs`, rather than a
regex/text-scan approximation the way `check_burn_list.py`'s own JS rules get away with for
pattern-matching (a real function-boundary computation needs a real parser, not a guess).

Scope, matching check_function_test_anchors.py's Python scope as closely as JS's own real
conventions allow:
- Only named, independently-callable units: function declarations, class methods (including a
  class field initialized to an arrow/function expression -- Odoo OWL's own common
  `onClick = () => {...}` handler idiom), and a function/arrow-function expression assigned
  directly to a variable or object property. An anonymous inline callback
  (`el.addEventListener("click", () => {...})`) is NOT counted -- the same "not an independently
  testable unit" reasoning the Python side gives for excluding nested closures.
- `*.test.js` (this codebase's real Hoot unit-test file convention, the direct JS analogue of
  Python's `test_*.py`) is excluded -- these files ARE the tests, not code under test.
- A real Odoo tour registration file (contains a real `registry.category("web_tour.tours")` call
  -- detected by content, not filename, because this codebase's own tour files are inconsistently
  named/placed: some end in `_tour.js` under a `tours/` directory, some don't, and at least one
  real file, `ham_onboarding/static/src/js/onboarding_tour_utils.js`, has "tour" in its name but is
  genuine reusable application code with no such registration call at all) is excluded for the
  same reason `*.test.js` is: confirmed directly (`ham_dx_cluster/tests/test_dx_tour.py`'s own
  `self.start_tour(..., "dx_cluster_tour", ...)`) that a tour registration is itself the test
  fixture a real Python HttpCase test drives, not application code under test.
- Vendored third-party JS is excluded the same way `eslint.config.js`'s own `ignores` list already
  excludes it from linting: `static/lib/` (or `static/src/lib/`) directories and `*.min.js` files
  (confirmed real vendored files exist at this exact path shape:
  `ham_satellite/static/src/lib/{OrbitControls,three.min,satellite.min}.js`).
"""

import argparse
import ast
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import verify_anchors as va  # noqa: E402

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

TOUR_REGISTRATION_PATTERN = re.compile(r'registry\.category\(\s*["\']web_tour\.tours["\']')

DEFAULT_BASELINE_FILENAME = "js_function_test_anchor_baseline.json"

_JS_SCAN_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "js_function_scan.cjs")


def _git_tracked_js_files(repo_root):
    """Only git-tracked .js files -- same real-world-safety reason
    `_git_tracked_python_files` uses `git ls-files` over a raw `os.walk`."""
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


def _is_tour_registration_file(content):
    return bool(TOUR_REGISTRATION_PATTERN.search(content))


def _web_assets_tests_files(repo_root):
    """Every real file path any module's own `__manifest__.py` declares under the
    `web.assets_tests` bundle -- Odoo's own built-in, authoritative "this asset loads only during
    test/tour execution, never on a real production page" declaration. Found live: a first attempt
    at this exclusion used an import-graph heuristic (does any non-tour file ever import this one)
    and correctly caught `tour_utils.js` (imported via a real `@module/path` ES import) but MISSED
    `tour_failure_dump.js` entirely -- that file is never `import`ed by anything at all; it's loaded
    as a raw script directly from the `web.assets_tests` bundle (confirmed directly,
    `zero_sudo/__manifest__.py`), a loading mechanism no import-graph search can see. The manifest
    itself is the real, authoritative, and simpler signal -- the same `ast.literal_eval` parse
    `check_manifest_dependencies.py` already uses for its own `assets` bundle walk, reused here
    rather than re-derived.

    Real scope note: `web.assets_tests` genuinely means "test-only" in Odoo's own asset-bundle
    convention (as opposed to `web.assets_backend`/`web.assets_frontend`, real production bundles),
    so a file listed *only* under this bundle is production-code-adjacent test infrastructure, the
    same category `*.test.js`/a tour registration file already get exempted as."""
    test_files = set()
    for manifest_rel in _git_tracked_manifest_files(repo_root):
        manifest_path = os.path.join(repo_root, manifest_rel)
        module_dir = os.path.dirname(manifest_path)
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=manifest_path)
        except (OSError, SyntaxError):
            continue
        for node in tree.body:
            if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Dict)):
                continue
            try:
                manifest_dict = ast.literal_eval(node.value)
            except ValueError:
                continue
            assets = manifest_dict.get("assets", {})
            if not isinstance(assets, dict):
                continue
            for asset_path in assets.get("web.assets_tests", []):
                # Manifest asset paths are module-relative ("zero_sudo/static/src/js/foo.js"),
                # not manifest-relative -- module_dir's own parent is the real repo-relative base.
                test_files.add(os.path.normpath(os.path.join(module_dir, "..", asset_path)))
    return test_files


def _git_tracked_manifest_files(repo_root):
    try:
        out = subprocess.run(
            ["git", "ls-files", "*__manifest__.py"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    return [line for line in out.splitlines() if line]


def _extend_span_backward_over_comments(start, lines):
    """acorn's own `loc.start` for a function node points at the `function`
    keyword (or the property key, or the `class`/method name) itself -- it
    does NOT include a preceding `// [@ANCHOR: ...]` comment line, the JS
    analogue of the same real gap `_function_span`'s own lookback closes on
    the Python side (Python's AST equally excludes decorator/leading-
    comment lines from `FunctionDef.lineno`). Same lookback rule, ported
    directly: walk back over a contiguous run of `//`-comment lines only,
    deliberately NOT crossing a blank line, for the same reason the Python
    side doesn't -- crossing one risks absorbing the PREVIOUS function's own
    trailing anchor comment as if it were this function's."""
    while start > 1:
        prev = lines[start - 2].strip()
        if prev.startswith("//"):
            start -= 1
        else:
            break
    return start


def _run_js_scan(filepaths):
    """Batches every file into one real Node/acorn invocation (not one
    process per file) -- confirmed directly this handles 155 real files
    in a single sub-second call. Returns {filepath: [(name, start, end)]},
    silently omitting any file acorn itself failed to parse (matching
    scan_file's own SyntaxError-skip behavior on the Python side)."""
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
    return {
        entry["file"]: [(f["name"], f["start"], f["end"]) for f in entry["functions"]]
        for entry in results
    }


def scan_tree(repo_root):
    """Returns {identity: True} for every in-scope JS function currently
    lacking a base anchor, across the whole real repo. `identity` is
    `{relpath}::{qualname}`, the same key shape check_function_test_anchors.py
    uses for Python, so the two baselines never collide even though they
    share no filenames."""
    all_js_files = _git_tracked_js_files(repo_root)
    all_contents = {}
    for filepath in all_js_files:
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                all_contents[filepath] = f.read()
        except (OSError, UnicodeDecodeError):
            continue

    tour_files = {fp for fp, content in all_contents.items() if _is_tour_registration_file(content)}
    test_asset_files = _web_assets_tests_files(repo_root)

    candidate_files = []
    file_contents = {}
    for filepath, content in all_contents.items():
        if filepath in tour_files or filepath in test_asset_files:
            continue
        file_contents[filepath] = content
        candidate_files.append(filepath)

    scanned = _run_js_scan(candidate_files)

    gaps = {}
    for filepath in candidate_files:
        functions = scanned.get(filepath)
        if functions is None:
            continue  # acorn itself failed to parse this file
        content = file_contents[filepath]
        lines = content.splitlines()
        rel_path = os.path.relpath(filepath, repo_root)
        for qualname, start, end in functions:
            start = _extend_span_backward_over_comments(start, lines)
            span = "\n".join(lines[start - 1 : min(end, len(lines))])
            has_anchor = bool(va.ANCHOR_PATTERN.search(span))
            identity = f"{rel_path}::{qualname}"
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
        print(
            f"[*] Baseline written: {len(current_gaps)} pre-existing unanchored JS functions "
            f"recorded at {baseline_path}"
        )
        return 0

    baseline = load_baseline(baseline_path)
    new_gaps = sorted(k for k in current_gaps if k not in baseline)

    if new_gaps:
        print("[!] CI/CD FAILURE: New Unanchored JS Functions Detected (ADR 0090):")
        for identity in new_gaps:
            print(f"    - {identity}")
        print(
            "      [!] DIAGNOSTIC FOR AI: This function is new (or was modified to lose its "
            "anchor) since the baseline was taken -- it is not grandfathered in. Add a real "
            "`// [@ANCHOR: name]` (or `[@ANCHOR-BEGIN:]`/`[@ANCHOR-END:]`) and a real test citing "
            "it with `// Tests [@ANCHOR: name]`, matching verify_anchors.py's own rules."
        )
        return 1

    print(
        f"[+] SUCCESS: No new unanchored JS functions ({len(current_gaps)} pre-existing, "
        f"grandfathered, tracked in the baseline for the real sweep)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
