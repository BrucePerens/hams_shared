#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Odoo-Aware Type Checking, Phase 1 (see docs/proposals/ODOO_AWARE_TYPE_CHECKING.md)
------------------------------------------------------------------------------------
Runs `mypy --check-untyped-defs` over plain, non-Odoo-model utility files --
pure functions and daemon/ingestion scripts with no `_inherit`/`_name`
model class involved. This catches real "wrong method, wrong arg count"
bugs (the proposal's own motivating examples: notify_model_invalidation()
called with the wrong arg count, record.with_delay() when queue_job was
never installed) without needing the registry-aware mypy plugin Phase 2
would require to handle Odoo's `_inherit` merging correctly.

Any file that imports `models` from the `odoo` package is automatically
skipped -- Odoo Model classes are exactly the class of file vanilla mypy
cannot check correctly yet (see the proposal's "cross-module _inherit"
section), so silently skipping them here is a safety filter, not a gap
Phase 2 hasn't gotten to.

Deliberately excluded even though it doesn't touch Odoo models:
`daemons/hams_local_relay/radae/` (vendored ML/DSP research code with
extensive real mypy findings -- untangling third-party research code's
typing is its own project, not Phase 1 scope).

EXCLUDED_FILES started as 33 files with real, not-yet-reviewed mypy
findings on this check's introduction. As of 2026-08-20 that backlog has
been fully worked through -- every file now passes cleanly, with real
bugs fixed (e.g. a NoneType slice crash on a malformed NOAA payload, a
websockets 15.x API mismatch that would TypeError the instant a test
ran, a live-broken binary-discovery function that could never find
Debian's own `pat` package) and false positives resolved honestly rather
than blanket-suppressed (heterogeneous dict/list literals given real
type annotations instead of mypy's overly-broad inferred join type,
Python's function-wide variable typing conflicting across unrelated
code paths reusing a name like `f` or `key`, and confirmed third-party
stub gaps like telnetlib3.open_connection()'s bare
`Union[TelnetWriter, TelnetWriterUnicode]` return type with no static
way to narrow on the `encoding=` argument's runtime value). See git
history for the individual fix commits. This is now a hard, unconditional
gate (run_linters.py step 23 already fails the whole lint pass on any
finding here, for any file this check reaches) -- EXCLUDED_FILES stays
in the code, empty, as where a newly reviewed and deliberately accepted
exclusion would go if one is ever needed again, not repopulated
reflexively just because a new file trips this check.
"""

import os
import subprocess
import sys

# Repo-relative paths (files or directories) to scan.
SCAN_ROOTS = [
    "ham_com/models/callsign_validation.py",
    "ham_base/models/geo_utils.py",
    "daemons",
    "ingest",
]

# Repo-relative directory prefixes to never scan, even under SCAN_ROOTS.
EXCLUDED_DIR_PREFIXES = [
    "daemons/hams_local_relay/radae",
]

# As of 2026-08-20, every file this check can reach passes
# mypy --check-untyped-defs cleanly -- the backlog that used to live
# here (33 files) has been fully worked through, fixing real bugs and
# false positives along the way (see git history for the individual
# commits). This set stays here, empty, as where a *newly reviewed and
# accepted* exclusion would go if one is ever needed again -- not
# repopulated reflexively just because a new file trips this check.
EXCLUDED_FILES: set = set()

IGNORE_DIR_NAMES = {"__pycache__", "node_modules", ".venv", "venv", "target", ".git"}


def imports_odoo_models(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith("from odoo import") and "models" in stripped:
                    return True
                if stripped.startswith("import odoo.models"):
                    return True
                if stripped.startswith("from odoo.models import"):
                    return True
    except OSError:
        return False
    return False


def collect_candidates(repo_root):
    candidates = []
    for rel_root in SCAN_ROOTS:
        abs_root = os.path.join(repo_root, rel_root)
        if os.path.isfile(abs_root):
            files = [abs_root]
        elif os.path.isdir(abs_root):
            files = []
            for root, dirs, filenames in os.walk(abs_root):
                dirs[:] = [d for d in dirs if d not in IGNORE_DIR_NAMES]
                for name in filenames:
                    if name.endswith(".py"):
                        files.append(os.path.join(root, name))
        else:
            continue

        for abs_path in files:
            rel_path = os.path.relpath(abs_path, repo_root)
            if any(
                rel_path == excluded or rel_path.startswith(excluded + os.sep)
                for excluded in EXCLUDED_DIR_PREFIXES
            ):
                continue
            if rel_path in EXCLUDED_FILES:
                continue
            if imports_odoo_models(abs_path):
                continue
            candidates.append(abs_path)
    return sorted(set(candidates))


def main():
    if len(sys.argv) < 2:
        print("Usage: check_untyped_utility_files.py <repo_root>")
        sys.exit(1)

    repo_root = sys.argv[1]
    candidates = collect_candidates(repo_root)
    if not candidates:
        sys.exit(0)

    # Run mypy one file at a time rather than as one batched invocation:
    # these are independent, unrelated CLI scripts (many literally named
    # main.py in different directories), not one coherent package, and
    # mypy's default module-name inference collides same-basename files
    # across different directories ("Duplicate module named main") when
    # given as a single batch with no __init__.py markers to disambiguate.
    any_failed = False
    for candidate in candidates:
        res = subprocess.run(
            ["mypy", "--check-untyped-defs", "--ignore-missing-imports", candidate],
            capture_output=True,
            text=True,
            cwd=repo_root,
        )
        if res.returncode != 0:
            if not any_failed:
                print("❌ mypy findings in non-Odoo-model utility files:")
            any_failed = True
            if res.stdout:
                print(res.stdout, end="")
            if res.stderr:
                print(res.stderr, end="")

    sys.exit(1 if any_failed else 0)


if __name__ == "__main__":
    main()
