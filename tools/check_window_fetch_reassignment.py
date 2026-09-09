#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Flags direct `window.fetch = ...`/`globalThis.fetch = ...` reassignment in *.test.js files.

Origin: found live during the 2026-09-08 patent-disclosure re-verification of disclosure 26
(`ham_world_map_view.test.js`) -- two tests reassigned `window.fetch` directly to mock a network
call, which throws `TypeError: Cannot assign to read only property 'fetch'` under the real Odoo
`@odoo/hoot` test harness, since hoot's mocked `window` makes `window.fetch` read-only. Both tests
had silently never been run for real; the codebase's own sanctioned pattern is the `mockFetch()`
helper (established in `ham_shack/static/tests/web_transceiver.test.js`), which this check
recommends in its violation message. A follow-up sweep the same night found the identical pattern
in 17 more tests across four other modules (ham_logbook, ham_satellite, theme_hams), none of which
ship a hoot test runner at all -- meaning this specific defect can go undetected indefinitely
without a mechanical check, since nothing forces these tests to actually run. `globalThis.fetch =`
is the exact same failure under the exact same read-only property (module code and ES module scope
both resolve `globalThis` to the same mocked `window` object hoot patches), so it's covered by the
same regex rather than a second check.

This check is deliberately narrow (a single, low-noise regex) rather than a general JS static
analyzer: `window.fetch =`/`globalThis.fetch =` (or the bracket-notation equivalents) assigned to
anything, excluding `==`/`===`/`!=`/`!==` comparisons, inside a `*.test.js` file.
"""

import os
import re
import sys

_ASSIGNMENT_RE = re.compile(
    r"""(?:window|globalThis)(?:\.fetch|\[['"]fetch['"]\])\s*=(?!=)"""
)

_IGNORE_DIRS = {
    ".git",
    "node_modules",
    "venv",
    "env",
    ".venv",
    "__pycache__",
    ".agents",
    "target",
    "radae",
    # Stale `git worktree add` checkouts under .claude/worktrees/<hash>/ --
    # frozen, historical copies from past agent sessions, not live source.
    # Found live wiring this check into run_linters.py: several already-
    # fixed test.js files still had old, broken worktree copies lingering
    # here, which this check flagged even though the real source was
    # clean. Same reasoning as check_absolute_paths.py's own archive/
    # exclusion and check_hoot_runner_coverage.py's own ignore_dirs.
    ".claude",
}


def check_window_fetch_reassignment(repo_dir):
    violations = []

    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d not in _IGNORE_DIRS]

        for file in files:
            if not file.endswith(".test.js"):
                continue

            file_path = os.path.join(root, file)
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    for i, line in enumerate(f, 1):
                        if _ASSIGNMENT_RE.search(line):
                            violations.append(
                                f"{os.path.relpath(file_path, repo_dir)}:{i} Direct `window.fetch = "
                                "...`/`globalThis.fetch = ...` reassignment -- this throws under the "
                                "real @odoo/hoot test harness (hoot's mocked window makes fetch "
                                "read-only). Use this repo's own mockFetch() helper instead (see "
                                "ham_shack/static/tests/web_transceiver.test.js for the established "
                                "pattern)."
                            )
            except UnicodeDecodeError as e:
                print(f"Warning: UnicodeDecodeError reading {file_path}: {e}")

    return violations


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: check_window_fetch_reassignment.py <repo_dir>")
        sys.exit(1)

    repo_dir = sys.argv[1]
    violations = check_window_fetch_reassignment(repo_dir)

    if violations:
        print("❌ window.fetch Reassignment Violations:")
        for v in violations:
            print(f"  - {v}")
        sys.exit(1)

    sys.exit(0)
