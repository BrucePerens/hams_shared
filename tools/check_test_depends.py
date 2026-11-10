#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Manifest `test_depends` Auditor
-------------------------------
A module's `__manifest__.py` may list, under `test_depends`, modules its TESTS need installed although its runtime
`depends` does not include them (a page measured, a model or field used by one test). Odoo ignores unknown manifest keys;
devbox_tools/asset_gate.py installs the entries next to the module in a test run and hams_shared/tools/test_impact.py treats
each entry as an extra reverse edge (a change to the companion selects the dependent's tests).

This is NOT `depends_cycle` (check_dependency_cycles.py), the runtime mechanism for genuine dependency cycles. The two keys
are independent and never substitute for each other.

Checks, across hams_com and hams_open (one registry in real deployments):
  1. every entry names an existing module in the graph (a typo makes the test run install nothing),
  2. the entry is not already in the module's real `depends` (redundant),
  3. the entry is not the module itself,
  4. the entry does not close a cycle of test_depends edges alone (A needs B for its tests, B needs A for its tests: neither
     can be the base of the other). A companion that DEPENDS on the module at runtime (ham_events' tests need
     ham_club_management, which depends on ham_events) is legitimate: the companion is installed on top of the module. Such an
     entry only means the module's tests that need it must run post_install.
"""

import ast
import os
import sys

import check_dependency_cycles as cdc
import check_target
from tree_files import tree_walk

SKIP = ("radae", "node_modules", "__pycache__", ".git", "daemons", "tools")


def _literal_list(value):
    if isinstance(value, (ast.List, ast.Tuple)):
        return [e.value for e in value.elts if isinstance(e, ast.Constant)]
    return []


def _find_sibling_repo(repo_root):
    """The other repository (hams_com <-> hams_open), by the same rule check_dependency_cycles.py uses."""
    return cdc._find_sibling_repo(repo_root)


def build_graphs(roots):
    """(depends, test_depends): {module: [names]} read from every manifest under `roots`."""
    depends, test_depends = {}, {}
    for repo_root in roots:
        for root, _dirs, files in tree_walk(repo_root, "__manifest__.py", SKIP, skip_hidden=True, untracked=True):
            if "__manifest__.py" not in files:
                continue
            mod = os.path.basename(root)
            path = os.path.join(root, "__manifest__.py")
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    tree = ast.parse(handle.read(), filename=path)
            except (SyntaxError, OSError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Dict):
                    continue
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and key.value == "depends":
                        depends[mod] = _literal_list(value)
                    elif isinstance(key, ast.Constant) and key.value == "test_depends":
                        test_depends[mod] = _literal_list(value)
                if mod in depends:
                    break
    return depends, test_depends


def check_entries(depends, test_depends):
    """Error strings for every bad `test_depends` entry."""
    combined = {mod: [e for e in entries if e != mod] for mod, entries in test_depends.items()}
    errors = []
    for mod, entries in sorted(test_depends.items()):
        for dep in entries:
            if dep == mod:
                errors.append(f"❌ ERROR: SELF-REFERENTIAL test_depends: {mod} lists itself.")
            elif dep in depends.get(mod, []):
                errors.append(
                    f"❌ ERROR: REDUNDANT test_depends: {mod}'s 'test_depends' lists '{dep}', which is ALSO in its real "
                    "'depends' -- remove it from test_depends."
                )
            elif dep not in depends:
                errors.append(
                    f"❌ ERROR: UNKNOWN test_depends target: {mod}'s 'test_depends' lists '{dep}', which is not a module in "
                    "this dependency graph (typo, or the module does not exist)."
                )
            elif cdc._reachable(combined, dep, mod):
                errors.append(
                    f"❌ ERROR: CYCLIC test_depends: {mod} lists '{dep}', which already reaches {mod} through test_depends edges alone."
                )
    return errors


def main():
    if len(sys.argv) < 2:
        print("Usage: check_test_depends.py <repo_root>")
        sys.exit(1)
    check_target.require_directory(sys.argv[1], "check_test_depends")
    repo_root = cdc._resolve_repo_root(sys.argv[1])
    roots = [repo_root]
    sibling = _find_sibling_repo(repo_root)
    if sibling:
        roots.append(sibling)
    depends, test_depends = build_graphs(roots)
    check_target.require_scanned(len(depends), ", ".join(roots), "check_test_depends", "Odoo modules (directories holding __manifest__.py)")
    errors = check_entries(depends, test_depends)
    for err in errors:
        print(err)
    if errors:
        print(f"Total Errors (test_depends): {len(errors)}")
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
