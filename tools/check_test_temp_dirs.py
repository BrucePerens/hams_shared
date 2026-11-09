#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Rust test code must not build its own scratch path under the temporary directory.

A test that writes `std::env::temp_dir().join(format!("name_{pid}"))` by hand and creates it
leaves that directory behind unless it also removes it, and it never does on a failing test
(the removal line after the failing assertion is not reached). On a machine whose temporary
directory is memory, a suite run once leaked about eighty directories. The fix is one shared
scratch-directory helper per crate whose value removes the tree when it is dropped (including
while a failing test unwinds), and tests that use it.

This gate fails on `temp_dir().join(` in Rust test code: a file under a `tests/` directory, a
file named `tests.rs`, `testkit.rs`, `*_tests.rs` or `*_test.rs`, or anything after the first
`#[cfg(test)]` line of another file. The helper itself carries a same-line or immediately
preceding comment `// temp-dir-helper: <how it is removed>`; any other deliberate exception
carries `// temp-dir-ignore: <reason>`. Code outside tests is not judged (a product default
output directory is not a test scratch directory). Examples, benches, fuzz targets, vendored
and reference code are skipped.

Existing uses that were not converted yet are listed in `test_temp_dir_baseline.json` next to
this script as `{"<path relative to its repository>": <count>}`; a file may not exceed its listed count and a file
that is not listed may not have any. `--generate-baseline` rewrites it from the current tree.

Heuristic text scan, not a parser, like the other Rust gates here.
"""

import argparse
import json
import os
import re
import sys

import check_target
from tree_files import tree_files

SKIP_DIRS = {
    ".git", "target", "vendor", "reference", "examples", "benches", "fuzz",
    "node_modules", "__pycache__", "worktrees", "site-packages", ".venv", "venv",
}
BASELINE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_temp_dir_baseline.json")
_CALL = re.compile(r"temp_dir\(\)\s*\.join\(")
_CFG_TEST = re.compile(r"^\s*#\[cfg\(test\)\]")
_MARK = re.compile(r"temp-dir-(?:helper|ignore)\s*:\s*\S")


def find_rust_files(root):
    yield from tree_files(root, ".rs", SKIP_DIRS)


def is_test_file(path):
    parts = path.split(os.sep)
    name = parts[-1]
    return (
        "tests" in parts[:-1]
        or name in ("tests.rs", "testkit.rs")
        or name.endswith("_tests.rs")
        or name.endswith("_test.rs")
    )


def check_text(text, whole_file_is_test):
    """The line numbers (1-based) of unmarked `temp_dir().join(` in test code of `text`."""
    lines = text.split("\n")
    test_from = 0 if whole_file_is_test else None
    if test_from is None:
        for i, line in enumerate(lines):
            if _CFG_TEST.match(line):
                following = next((l for l in lines[i + 1:] if l.strip() and not l.strip().startswith("#[")), "")
                # `#[cfg(test)] mod name;` only declares a test file; the file it names is judged by its own name.
                if re.match(r"\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+\w+\s*;", following):
                    continue
                test_from = i
                break
    if test_from is None:
        return []
    found = []
    offsets = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1
    for m in _CALL.finditer(text):
        ln = max(i for i, o in enumerate(offsets) if o <= m.start())
        if ln < test_from:
            continue
        window = lines[max(0, ln - 2): ln + 1]
        if any(_MARK.search(w) for w in window):
            continue
        found.append(ln + 1)
    return found


def scan(roots):
    """{"<path relative to its repository>": [line, ...]} for every file with findings."""
    out = {}
    for root in roots:
        for path in find_rust_files(root):
            try:
                text = open(path, encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                continue
            hits = check_text(text, is_test_file(os.path.relpath(path, root)))
            if hits:
                key = os.path.relpath(path, root)
                out[key] = out.get(key, []) + hits
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("roots", nargs="+")
    ap.add_argument("--baseline", default=BASELINE)
    ap.add_argument("--generate-baseline", action="store_true")
    args = ap.parse_args(argv)
    roots = []
    for given in args.roots:
        r = check_target.require_directory(given, "check_test_temp_dirs")
        if os.path.basename(r) == "hams_shared":
            r = os.path.dirname(r)
        if r not in roots:
            roots.append(r)
    check_target.require_scanned(
        sum(len(list(find_rust_files(r))) for r in roots), ", ".join(roots), "check_test_temp_dirs", "Rust files"
    )
    found = scan(roots)
    if args.generate_baseline:
        with open(args.baseline, "w", encoding="utf-8") as f:
            json.dump({k: len(v) for k, v in sorted(found.items())}, f, indent=1, sort_keys=True)
            f.write("\n")
        print(f"baseline of {len(found)} files written to {args.baseline}")
        return 0
    try:
        with open(args.baseline, encoding="utf-8") as f:
            allowed = json.load(f)
    except FileNotFoundError:
        allowed = {}
    bad = False
    for path, lines in sorted(found.items()):
        if len(lines) > allowed.get(path, 0):
            bad = True
            where = ", ".join(str(n) for n in lines)
            print(
                f"❌ {path}: `temp_dir().join(` in test code at line {where}. A scratch directory built by hand is never "
                f"removed when the test fails. Use the crate's scratch-directory helper (a value that removes the tree "
                f"when dropped; for bench_mcp, crate::testdir). A helper itself carries `// temp-dir-helper: <how it is "
                f"removed>`; a deliberate exception carries `// temp-dir-ignore: <reason>`."
            )
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
