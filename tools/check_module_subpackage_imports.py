#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Module Subpackage Import Checker
-----------------------------------
Catches a real bug found by hand while building a new feature: `ham_propagation/models/` was
added (a new `__init__.py` plus a real model extension file), but `ham_propagation/__init__.py`
itself only ever did `from . import controllers` -- this module never had a `models/` directory
before, so nothing imported it. Odoo's own module loader only runs whatever a module's top-level
`__init__.py` actually imports; the new model extension was silently never registered at all, and
the new view (a real, valid `<button name="action_suggest_band">`) failed real Odoo view
validation with "action_suggest_band is not a valid action on ham.sked" -- the method genuinely
didn't exist on the merged model from Odoo's point of view, despite being real, correct Python
sitting right there on disk.

This is the same shape of bug as check_access_csv_group_order.py's own motivating case
(ham_aprs's group-order bug): a file that's completely correct in isolation, silently never
takes effect because nothing actually loads it, with a failure mode (view validation, an
external-id lookup) that can look unrelated to the real cause.

Checks the three subpackage directory names actually used across this codebase's own modules
(confirmed by survey, not guessed: `models`, `controllers`, `wizard` -- `report`/`data`/`i18n`
either don't appear or aren't meant to be Python-imported). For each Odoo module that has one of
these as a real subdirectory with its own `__init__.py`, the module's own top-level `__init__.py`
must import it (`from . import <sub>` or `from .<sub> import ...`) -- directly or, more commonly
in `models/__init__.py` files that re-export deeper packages, at least once somewhere.

Usage: check_module_subpackage_imports.py <repo_root>
Always scans the full manifest graph (both hams_com and hams_open), like
check_access_csv_group_order.py -- a module's own missing import is a property of that module
regardless of which repo invoked the check from.
"""

import ast
import os
import sys

SKIP_DIRS = {"node_modules", "__pycache__", ".git", "daemons", "tools", "radae"}
CHECKED_SUBPACKAGES = ("models", "controllers", "wizard")


def _resolve_repo_root(given_path):
    """run_linters.py's own `dir_path` resolves to the hams_shared directory itself, not a real
    repo root (confirmed directly while debugging check_access_csv_group_order.py's identical
    issue -- same root cause, same fix, applied here too since this checker is invoked the same
    way). Detect that case by name and redirect to hams_shared's real parent repo instead."""
    given_path = os.path.abspath(given_path)
    if os.path.basename(given_path) == "hams_shared":
        return os.path.dirname(given_path)
    return given_path


def _find_sibling_repo(repo_root):
    """Mirrors check_access_csv_group_order.py's own sibling-repo resolution."""
    repo_root = os.path.abspath(repo_root)
    for sibling_name in ("hams_open", "hams_com"):
        if os.path.basename(repo_root) == sibling_name:
            continue
        candidate = os.path.abspath(os.path.join(repo_root, "..", sibling_name))
        if not os.path.isdir(candidate):
            continue
        has_a_module = any(
            os.path.isfile(os.path.join(candidate, d, "__manifest__.py"))
            for d in os.listdir(candidate)
            if os.path.isdir(os.path.join(candidate, d))
        )
        if has_a_module:
            return candidate
    return None


def _find_modules(roots):
    for root in roots:
        for entry in sorted(os.listdir(root)):
            if entry in SKIP_DIRS or entry.startswith("."):
                continue
            module_dir = os.path.join(root, entry)
            manifest_path = os.path.join(module_dir, "__manifest__.py")
            if os.path.isdir(module_dir) and os.path.isfile(manifest_path):
                yield entry, module_dir


def _imported_subpackage_names(init_path):
    """Returns the set of subpackage names this __init__.py imports at its own top level, via
    either `from . import X` or `from .X import Y`. AST-only, never executes the file."""
    try:
        with open(init_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=init_path)
    except (SyntaxError, UnicodeDecodeError, OSError):
        # A real syntax error here is a real problem, but it's a different
        # checker's job to catch that -- this one only cares about imports
        # it can actually parse.
        return set()

    imported = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level != 1:
            continue
        if node.module:
            # from .models import Something -- node.module == "models"
            imported.add(node.module.split(".")[0])
        else:
            # from . import models
            for alias in node.names:
                imported.add(alias.name)
    return imported


def _check_module(module_name, module_dir):
    """Returns a list of error strings for this one module, or [] if it's clean."""
    init_path = os.path.join(module_dir, "__init__.py")
    if not os.path.isfile(init_path):
        # A real Odoo module always has one; a missing one is a different,
        # more fundamental problem this checker doesn't try to diagnose.
        return []

    imported = _imported_subpackage_names(init_path)
    errors = []
    for sub in CHECKED_SUBPACKAGES:
        sub_init = os.path.join(module_dir, sub, "__init__.py")
        if os.path.isfile(sub_init) and sub not in imported:
            errors.append(
                f"{module_name}: has a real '{sub}/__init__.py' but "
                f"{module_name}/__init__.py never imports it (no 'from . import {sub}') -- "
                f"everything in {sub}/ is silently dead code as far as Odoo's module loader "
                f"is concerned."
            )
    return errors


def main():
    if len(sys.argv) < 2:
        print("Usage: check_module_subpackage_imports.py <repo_root>", file=sys.stderr)
        return 1

    repo_root = _resolve_repo_root(sys.argv[1])
    roots = [repo_root]
    sibling = _find_sibling_repo(repo_root)
    if sibling:
        roots.append(sibling)

    all_errors = []
    for module_name, module_dir in _find_modules(roots):
        all_errors.extend(_check_module(module_name, module_dir))

    if all_errors:
        print("\n[!] CI/CD FAILURE: Module Subpackage Import Violation:")
        for err in all_errors:
            print(f"    - {err}")
        print(
            "      [!] DIAGNOSTIC FOR AI: Odoo only registers what a module's own "
            "__init__.py actually imports. Add the missing 'from . import <subpackage>' "
            "line -- this is not optional boilerplate, code in an unimported subpackage "
            "never runs at all, however correct it is."
        )
        return 1

    print("[*] Module subpackage imports: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
