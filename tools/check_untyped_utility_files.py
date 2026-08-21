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

Deliberately excluded even though they don't touch Odoo models:
`daemons/hams_local_relay/radae/` (vendored ML/DSP research code with
extensive real mypy findings -- untangling third-party research code's
typing is its own project, not Phase 1 scope) and the individual files in
EXCLUDED_FILES below, each of which produced real mypy output on first
evaluation that needs a human to actually look at before this becomes a
hard gate on it. At least one of those (daemons/ham_dx_daemon/main.py)
was checked by hand and confirmed to be a FALSE POSITIVE, not a real bug:
telnetlib3.open_connection()'s return type is a bare
`Union[TelnetWriter, TelnetWriterUnicode]` with no static way to narrow
based on the `encoding=` argument's runtime value, so mypy assumes the
bytes-only variant even though the actual call (encoding='utf8', the
default) returns the str-accepting one. That's a reminder that a
third-party library's own imprecise type signature is a second real
source of Phase-1 noise beyond Odoo's dynamic typing -- excluded files
are "not yet reviewed," not "presumed buggy."
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

# Repo-relative files with real, not-yet-reviewed mypy findings as of this
# check's introduction. See the module docstring above.
EXCLUDED_FILES = {
    "ingest/build_dependency_graph.py",
    "ingest/composite_text.py",
    "ingest/parse_pdfs.py",
    "ingest/reset_pipeline_state.py",
    "ingest/visual_daemon.py",
}

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
