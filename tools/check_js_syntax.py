#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later
import os
import sys
import subprocess
import argparse
import multiprocessing
import re
import logging


def load_ignore_file(filepath):
    patterns = []
    if filepath and os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append(re.compile(line))
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
# of this exact mistake were found and fixed across 7 modules in both
# hams_com and hams_open on 2026-08-27 (see hams_com/night_shift_todo.md's
# Twenty-second through Twenty-fourth addenda), several of which had been
# silently masked by an unrelated asset-bundle bug and had never actually
# run before that session. This check exists so the same mistake fails
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


def check_file(file_path):
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            code = f.read()
    except (OSError, UnicodeDecodeError) as e:
        logging.warning("Failed to read %s: %s", file_path, e)
        return None

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
                if f".{bad_name}(" in line:
                    err_msg = (
                        f"🚨 [AUDIT] INVALID HOOT MATCHER: `.{bad_name}(` (line {lineno}) is not "
                        f"a real @odoo/hoot expect() method -- it's a Jest-ism. Use "
                        f"`.{real_equivalent}` instead.\n"
                        f"Code: `{line.strip()}`"
                    )
                    return file_path, err_msg

            # window.fetch/globalThis.fetch reassignment in *.test.js is
            # covered by the standalone check_window_fetch_reassignment.py
            # (wired into run_linters.py), not here -- kept as one rule in
            # one place rather than duplicating it.

            # Real bug pattern, found live 2026-09-09: hoot's mocked window
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
                return file_path, err_msg

    if "extends Interaction" in code and "mountComponent(" in code:
        err_msg = (
            "🚨 [AUDIT] ARCHITECTURE TRAP: Do not manually call `mountComponent(`\n"
            "inside an Interaction class. This causes an Owl Registry Collision with\n"
            "the NotificationContainer. Let Odoo handle mounting natively via\n"
            "`static components` and data props.\n"
            "See docs/LLM_EXPERIENCE.md (Item 38) for details."
        )
        return file_path, err_msg

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


def main():
    parser = argparse.ArgumentParser(description="Check JS syntax")
    parser.add_argument("--ignore-file", help="Path to ignore list")
    parser.add_argument("directories", nargs="+", help="Directories to scan")
    args = parser.parse_args()

    ignore_patterns = load_ignore_file(args.ignore_file)
    js_files = []

    for d in args.directories:
        if not os.path.isdir(d):
            continue
        for root, dirs, files in os.walk(d):
            if "radae" in dirs:
                dirs.remove("radae")
            # Prune hidden folders and standard library directories to save time
            dirs[:] = [
                dir_name
                for dir_name in dirs
                if not dir_name.startswith(".")
                and dir_name not in ("node_modules", "__pycache__")
                and not (os.path.basename(root) == "static" and dir_name == "lib")
            ]
            for f in files:
                if f.endswith(".js") and ".min." not in f:
                    full_path = os.path.join(root, f)
                    rel_path = os.path.relpath(full_path, os.path.dirname(d))
                    if not is_ignored(rel_path, ignore_patterns):
                        js_files.append(full_path)

    if not js_files:
        print("[+] JS Syntax Linter: No JS files found in target modules.")
        return 0

    print(f"[*] JS Syntax Linter: Checking {len(js_files)} JS files...")
    errors = []

    # Constrain pool size to avoid overwhelming the VM CPU scheduler
    pool_size = min(4, multiprocessing.cpu_count() or 1)
    with multiprocessing.Pool(pool_size) as pool:
        for res in pool.imap_unordered(check_file, js_files):
            if res:
                errors.append(res)

    if errors:
        print("🚨 JAVASCRIPT SYNTAX ERRORS DETECTED 🚨\n")
        for file_path, stderr in errors:
            print(f"File: {file_path}\n{stderr.strip()}\n" + "-" * 60)
        return 1

    print(f"[+] JS Syntax Linter: All {len(js_files)} files passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
