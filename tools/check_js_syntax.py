#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
import os
import sys
import subprocess
import argparse
import multiprocessing
import re
import json
import logging
import check_target
from tree_files import tree_walk
from source_text import read_source


def load_ignore_file(filepath):
    patterns = []
    if filepath and os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if line and not line.startswith("#"):
                    try:
                        patterns.append(re.compile(line))
                    except re.error as e:
                        # One typo'd regex anywhere in the ignore file (an unbalanced '(' is
                        # the classic case) must not crash the whole checker before it checks a
                        # single JS file -- report it and keep the other, valid patterns.
                        logging.warning(
                            "%s:%d: invalid regex %r skipped: %s", filepath, lineno, line, e
                        )
    return patterns


def is_ignored(path, patterns):
    for pat in patterns:
        if re.search(pat, path):
            return True
    return False


# Jest/Chai-style matcher names an LLM (or a human who's used those
# frameworks) commonly reaches for out of habit, but @odoo/hoot's real
# expect() API (odoo/addons/web/static/lib/hoot/core/expect.js) doesn't
# implement -- confirmed by reading that file's complete list of `to*`
# methods directly, not assumed. Calling one of these throws
# "expect(...).toXxx is not a function" at test-run time; 10 real instances
# of this exact mistake existed across 7 modules in both hams_com and
# hams_open, several of which had been silently masked by an unrelated
# asset-bundle bug and had never actually run. This check exists so the same mistake fails
# fast (a syntax-adjacent lint error) instead of silently shipping a test
# that can never pass.
_INVALID_HOOT_MATCHERS = {
    "toBeUndefined": "toBe(undefined)",
    "toBeNull": "toBe(null)",
    "toBeDefined": "not.toBe(undefined)",
    "toBeTruthy": "toBe(true), or a more specific real matcher",
    "toBeFalsy": "toBe(false), or a more specific real matcher",
    "toBeNaN": "no direct hoot equivalent -- check with a plain if/throw",
    "toContain": "toInclude(...)",
    "toContainEqual": "toInclude(...)",
    "toStrictEqual": "toEqual(...)",
    "toBeGreaterThanOrEqual": "not.toBeLessThan(...)",
    "toBeLessThanOrEqual": "not.toBeGreaterThan(...)",
    "toHaveBeenCalled": "no hoot equivalent -- track calls in a counter/flag and assert with toBe()",
    "toHaveBeenCalledWith": "no hoot equivalent -- capture call args manually and assert with toEqual()",
    "toHaveBeenCalledTimes": "no hoot equivalent -- track a counter manually and assert with toBe()",
    "toMatchSnapshot": "no hoot equivalent -- hoot has no snapshot testing",
    "toThrowError": "toThrow(...)",
}


def audit_file(file_path):
    """The Odoo-architecture rules that run before the syntax check. Returns (code, failure): `failure` is a
    (path, message) pair or None; `code` is the source text, None when the file could not be read."""
    code, problem = read_source(file_path)
    if problem:
        return None, (file_path, problem)

    # --- Custom Odoo Architecture Checks ---
    if file_path.endswith(".test.js"):
        code_lines = [
            (lineno, line)
            for lineno, line in enumerate(code.splitlines(), start=1)
            if not line.strip().startswith("//")
        ]
        # The UNTESTABLE URL MOCK check below only fires when a file
        # combines window.history.pushState/replaceState(...) with a
        # document.location/window.location reference -- that pairing is
        # what's actually untestable (hoot mocks history but not
        # location), not pushState alone. A file that only reads back via
        # hoot's own mockLocation (the correct fix) never touches
        # document.location/window.location at all, so it must stay
        # clean of this rule rather than being forbidden from using the
        # very fix it recommends.
        references_real_location = any(
            re.search(r"\b(document|window)\.location\b", line) for _, line in code_lines
        )

        # A test that imports its `expect` from a sibling `./harness.js` runs on that project's own
        # runner (daemons/shack_console/console/tests/harness.js, which provides toContain), not on
        # @odoo/hoot, so the hoot matcher list does not describe it.
        uses_hoot_expect = not re.search(r"""from\s+['"]\./harness\.js['"]""", code)

        for lineno, line in enumerate(code.splitlines(), start=1):
            # Skip full-line comments -- this codebase's block comments are
            # consistently written as consecutive `// ...` lines (not
            # `/* */` blocks), so this is enough to keep the checks below
            # from tripping on prose that quotes the very patterns they
            # forbid (e.g. a comment explaining *why* `window.fetch =` or
            # `window.history.pushState(` is wrong).
            if line.strip().startswith("//"):
                continue

            for bad_name, real_equivalent in _INVALID_HOOT_MATCHERS.items():
                if not uses_hoot_expect:
                    break
                if f".{bad_name}(" in line:
                    err_msg = (
                        f"🚨 [AUDIT] INVALID HOOT MATCHER: `.{bad_name}(` (line {lineno}) is not "
                        f"a real @odoo/hoot expect() method -- it's a Jest-ism. Use "
                        f"`.{real_equivalent}` instead.\n"
                        f"Code: `{line.strip()}`"
                    )
                    return code, (file_path, err_msg)

            # window.fetch/globalThis.fetch reassignment in *.test.js is
            # covered by the standalone check_window_fetch_reassignment.py
            # (wired into run_linters.py), not here -- kept as one rule in
            # one place rather than duplicating it.

            # Real bug pattern: hoot's mocked window
            # only redirects `window.history` to a MockHistory/MockLocation
            # pair (odoo/addons/web/static/lib/hoot/mock/window.js's
            # WINDOW_MOCK_DESCRIPTORS -- `history: { value: mockHistory }`);
            # `window.location`/`document.location` are never patched, so
            # they stay the real, live location of the hoot test runner's
            # own page. A test that calls `window.history.pushState(...)`
            # to fake a URL query string, then asserts against
            # `document.location`, is asserting against two disconnected
            # objects -- verified to fail 100% of the time (confirmed
            # `Object.defineProperty(location, "search", ...)` also can't
            # patch around it: `configurable: false` in this Chrome).
            # Use hoot's own `mockLocation`/`mockHistory` (from
            # "@odoo/hoot") to read back what pushState/replaceState wrote,
            # or -- if the code under test hardcodes real document.location
            # with no injectable seam -- rely on a real Python browser tour
            # instead (see user_websites/static/tests/
            # toast_notifications.test.js's own precedent and comment).
            if references_real_location and re.search(
                r"window\.history\.(pushState|replaceState)\s*\(", line
            ):
                err_msg = (
                    f"🚨 [AUDIT] UNTESTABLE URL MOCK: `window.history.pushState/replaceState(` "
                    f"(line {lineno}) is combined with a document.location/window.location "
                    f"reference elsewhere in this file -- hoot only mocks `window.history`, "
                    f"never `document.location`/`window.location`, so pushState/replaceState "
                    f"writes and any real-location read are two disconnected objects. Read "
                    f"back via hoot's own `mockLocation` (from \"@odoo/hoot\") instead, or "
                    f"test through a real Python browser tour if the code under test "
                    f"hardcodes document.location with no injectable seam.\n"
                    f"Code: `{line.strip()}`"
                )
                return code, (file_path, err_msg)

    if "extends Interaction" in code and "mountComponent(" in code:
        err_msg = (
            "🚨 [AUDIT] ARCHITECTURE TRAP: Do not manually call `mountComponent(`\n"
            "inside an Interaction class. This causes an Owl Registry Collision with\n"
            "the NotificationContainer. Let Odoo handle mounting natively via\n"
            "`static components` and data props.\n"
            "See docs/LLM_EXPERIENCE.md (Item 38) for details."
        )
        return code, (file_path, err_msg)

    return code, None


def node_check(file_path, code):
    """The syntax check of one file: `node --input-type=module --check` on its source. Returns
    (path, message) when it does not parse, else None."""
    # Using --input-type=module forces Node to natively parse ES6 Imports/Exports
    # without needing experimental VM modules or package.json overrides.
    res = subprocess.run(
        ["node", "--input-type=module", "--check"],
        input=code,
        capture_output=True,
        text=True,
    )

    if res.returncode != 0:
        # Node reports stdin errors as '[stdin]:line'. Inject the real filename.
        err_msg = res.stderr.replace("[stdin]", os.path.basename(file_path))
        return file_path, err_msg
    return None


def check_file(file_path):
    """Audit rules, then the syntax check, for one file (one node process)."""
    code, failure = audit_file(file_path)
    if failure or code is None:
        return failure
    return node_check(file_path, code)


# One node process for many files. `node --check` on stdin builds a ModuleWrap from the source; so does
# vm.SourceTextModule's constructor (parsing only: no linking, no evaluation), so both accept exactly the
# same sources, but the batch pays node's ~40 ms start-up once instead of once per file (37 minutes for
# the JavaScript of a console PR under load, 2026-11-02). The batch only says WHICH files do not parse; each
# of those is checked again on its own so the message is the one `node --check` prints.
_BATCH_SCRIPT = r"""
const vm = require('vm');
let data = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => { data += chunk; });
process.stdin.on('end', () => {
  const bad = [];
  for (const [index, code] of JSON.parse(data)) {
    try { new vm.SourceTextModule(code, { identifier: String(index) }); } catch (error) { bad.push(index); }
  }
  process.stdout.write(JSON.stringify(bad));
});
"""
BATCH_SIZE = 400


def unparsable_in_batch(items):
    """Indexes of `items` ((path, code) pairs) that do not parse as an ES module, from one node process
    per BATCH_SIZE files. None when the batch could not be run (old node without
    --experimental-vm-modules, a crash): the caller then checks every file on its own."""
    bad = []
    for start in range(0, len(items), BATCH_SIZE):
        chunk = [[start + i, code] for i, (_path, code) in enumerate(items[start:start + BATCH_SIZE])]
        try:
            res = subprocess.run(
                ["node", "--experimental-vm-modules", "--no-warnings", "-e", _BATCH_SCRIPT],
                input=json.dumps(chunk),
                capture_output=True,
                text=True,
                timeout=600,
            )
            found = json.loads(res.stdout) if res.returncode == 0 else None
        except (OSError, ValueError, subprocess.TimeoutExpired):
            found = None
        if not isinstance(found, list):
            return None
        bad += [int(i) for i in found]
    return bad


def check_files(file_paths, pool_size):
    """Failures [(path, message)] for the files, same as check_file on each, in batches."""
    errors = []
    sources = []
    if pool_size > 1 and len(file_paths) > 8:
        with multiprocessing.Pool(pool_size) as pool:
            audited = pool.map(audit_file, file_paths, chunksize=16)
    else:
        audited = [audit_file(p) for p in file_paths]
    for path, (code, failure) in zip(file_paths, audited):
        if failure:
            errors.append(failure)
        elif code is not None:
            sources.append((path, code))
    bad = unparsable_in_batch(sources)
    if bad is None:
        suspects = sources
    else:
        suspects = [sources[i] for i in bad]
    for path, code in suspects:
        result = node_check(path, code)
        if result:
            errors.append(result)
    return errors


def main():
    parser = argparse.ArgumentParser(description="Check JS syntax")
    parser.add_argument("--ignore-file", help="Path to ignore list")
    parser.add_argument("directories", nargs="+", help="Directories to scan")
    args = parser.parse_args()

    ignore_patterns = load_ignore_file(args.ignore_file)
    js_files = []
    seen = 0

    for d in args.directories:
        check_target.require_directory(d, "check_js_syntax")
        # Hidden folders and standard library directories are left out; so is `static/lib` (vendored libraries), where
        # the `static` may be the directory given itself.
        def not_in_static_lib(rel, d=d):
            parts = [os.path.basename(os.path.abspath(d))] + rel.split("/")[:-1]
            return not any(a == "static" and b == "lib" for a, b in zip(parts, parts[1:]))

        for root, _dirs, files in tree_walk(
            d, ".js", ("radae", "node_modules", "__pycache__"), skip_hidden=True, accept=not_in_static_lib, untracked=True
        ):
            for f in files:
                if f.endswith(".js") and ".min." not in f:
                    seen += 1
                    full_path = os.path.join(root, f)
                    rel_path = os.path.relpath(full_path, os.path.dirname(d))
                    if not is_ignored(rel_path, ignore_patterns):
                        js_files.append(full_path)

    # `seen` counts the files examined before the ignore list: a tree whose files are all ignored on purpose is a scan, an empty one is not.
    check_target.require_scanned(seen, " ".join(args.directories), "check_js_syntax", "non-minified .js files")
    if not js_files:
        print("[+] JS Syntax Linter: No JS files found in target modules.")
        return 0

    print(f"[*] JS Syntax Linter: Checking {len(js_files)} JS files...")
    errors = []

    # Constrain pool size to avoid overwhelming the VM CPU scheduler
    pool_size = min(4, multiprocessing.cpu_count() or 1)
    errors = check_files(js_files, pool_size)

    if errors:
        print("🚨 JAVASCRIPT SYNTAX ERRORS DETECTED 🚨\n")
        for file_path, stderr in errors:
            print(f"File: {file_path}\n{stderr.strip()}\n" + "-" * 60)
        return 1

    print(f"[+] JS Syntax Linter: All {len(js_files)} files passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
