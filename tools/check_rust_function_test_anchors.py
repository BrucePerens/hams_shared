#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Rust Function-Level Test-Anchor Ratchet (ADR 0090 decision 2's Rust sub-track)

`check_function_test_anchors.py`'s own doc comment names this directly: "Rust (.rs) functions are
not scanned by any anchor mechanism yet -- a real, named, NOT YET DONE follow-on." This script is
that follow-on -- the same ratchet as the Python and JS scripts, extended to Rust via a real `syn`
AST parse (`rust_function_scan`, a small standalone Rust binary under this same directory) rather
than a regex/text-scan approximation, matching the JS sub-track's own "a real function-boundary
computation needs a real parser, not a guess" reasoning.

Scope, matching `check_function_test_anchors.py`'s Python scope and `check_js_function_test_
anchors.py`'s JS scope as closely as Rust's own real conventions allow -- see
`rust_function_scan/src/main.rs`'s own module doc comment for the exact walked-unit rules (free
functions, impl methods qualified as `Type::method`, trait default-method bodies qualified as
`Trait::method`; `#[test]`-attributed functions and anything inside a `#[cfg(test)]` module
excluded; nested/closure functions not independently walked).

File-level scope, mirroring the other two scanners' own vendored/tooling exclusions: git-tracked
`.rs` files only, excluding `vendor/` (this codebase's own real vendored-C-and-Rust convention --
confirmed real directories exist at this exact path shape, `daemons/*/vendor/`), `target/` (build
artifacts, though git wouldn't track these anyway), `examples/` and `benches/` (cargo's own
reserved demo/benchmark directories -- the same "not core product code" reasoning
`check_function_test_anchors.py` gives for excluding `tools`/`scripts`), and `tests/` (a crate's
own top-level integration-test directory -- these ARE the tests, the Rust analogue of Python's
`test_*.py` and JS's `*.test.js` exclusions, applied by directory name since Rust's own convention
places integration tests in a dedicated directory rather than a filename pattern).
"""

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import verify_anchors as va  # noqa: E402

EXCLUDE_DIRS = {
    ".git",
    "target",
    "vendor",
    "examples",
    "benches",
    "tests",
    "node_modules",
    "hams_community",
    "hams_com",
}

DEFAULT_BASELINE_FILENAME = "rust_function_test_anchor_baseline.json"

_SCAN_CRATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rust_function_scan")
_SCAN_BINARY = os.path.join(_SCAN_CRATE_DIR, "target", "release", "rust_function_scan")


def _git_tracked_rust_files(repo_root):
    """Only git-tracked .rs files -- same real-world-safety reason
    `_git_tracked_python_files`/`_git_tracked_js_files` use `git ls-files`
    over a raw `os.walk`."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "*.rs"],
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
        files.append(os.path.join(repo_root, rel))
    return files


def _ensure_scan_binary_built():
    """Builds `rust_function_scan` in release mode if its own binary
    doesn't already exist -- subsequent calls are near-free (cargo's own
    incremental build cache), the same per-invocation cargo overhead
    `check_cargo_clippy.py`/`check_cargo_deny.py` already accept as the
    cost of using the real toolchain rather than a mock. Returns True on
    success (binary present after this call), False otherwise."""
    if os.path.exists(_SCAN_BINARY):
        return True
    proc = subprocess.run(
        ["cargo", "build", "--release", "--quiet"],
        cwd=_SCAN_CRATE_DIR,
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0 and os.path.exists(_SCAN_BINARY)


def _run_rust_scan(filepaths):
    """Batches every file into one real `rust_function_scan` invocation
    (not one process per file), the same batching `_run_js_scan` already
    does for Node/acorn. Returns {filepath: [(name, start, end)]},
    silently omitting any file the real `syn` parser itself failed to
    parse (matching `_run_js_scan`'s own SyntaxError-skip behavior)."""
    if not filepaths or not _ensure_scan_binary_built():
        return {}
    proc = subprocess.run(
        [_SCAN_BINARY],
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
    """Returns {identity: True} for every in-scope Rust function/method
    currently lacking a base anchor, across the whole real repo.
    `identity` is `{relpath}::{qualname}`, the same key shape the other
    two scanners use, so all three baselines can coexist without
    collision even where a Python/JS/Rust file happen to share a
    relative path stem."""
    candidate_files = []
    file_contents = {}
    for filepath in _git_tracked_rust_files(repo_root):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
        except (OSError, UnicodeDecodeError):
            continue
        file_contents[filepath] = content
        candidate_files.append(filepath)

    scanned = _run_rust_scan(candidate_files)

    gaps = {}
    for filepath in candidate_files:
        functions = scanned.get(filepath)
        if functions is None:
            continue  # syn itself failed to parse this file
        content = file_contents[filepath]
        lines = content.splitlines()
        rel_path = os.path.relpath(filepath, repo_root)
        for qualname, start, end in functions:
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
            f"[*] Baseline written: {len(current_gaps)} pre-existing unanchored Rust functions "
            f"recorded at {baseline_path}"
        )
        return 0

    baseline = load_baseline(baseline_path)
    new_gaps = sorted(k for k in current_gaps if k not in baseline)

    if new_gaps:
        print("[!] CI/CD FAILURE: New Unanchored Rust Functions Detected (ADR 0090):")
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
        f"[+] SUCCESS: No new unanchored Rust functions ({len(current_gaps)} pre-existing, "
        f"grandfathered, tracked in the baseline for the real sweep)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
