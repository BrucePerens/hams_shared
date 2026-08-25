#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
ir.model.access.csv Group Definition Order Checker
----------------------------------------------------
Catches the exact bug class that silently broke this whole codebase's test suite: a module's
own manifest lists `security/ir.model.access.csv` in its `data` list BEFORE the XML file that
defines a `res.groups` record the CSV references by external ID -- Odoo loads `data` files in
the listed order, so the access rule fails with "No matching record found for external id
'<module>.<group>' in field 'Group'" the moment that module installs, and (with no
`--continue-on-error` in this codebase's test runner) that failure aborts the ENTIRE Odoo boot,
not just that one module -- every other module's tests, whatever they are, never run at all.

Found and fixed once by hand (ham_aprs/__manifest__.py: security/ir.model.access.csv listed
before security/security_data.xml, which defines ham_aprs.group_aprs_service) while chasing an
unrelated test-coverage task -- this check exists so a recurrence is caught by CI, not by
whoever next happens to run the full test suite and watches it die 116 modules in.

Only checks SAME-MODULE group references (`<this_module>.<group_id>`) -- a reference to another
module's group (e.g. `base.group_system`) is always safe regardless of this module's own `data`
order, since Odoo fully installs every dependency (and everything it defines) before this module's
own `data` list runs at all.

Usage: check_access_csv_group_order.py <repo_root>
Always scans the full manifest graph (both hams_com and hams_open), like
check_model_extension_collisions.py -- a module's own data-list ordering is a property of that
module regardless of which repo invoked the check from.
"""

import ast
import csv
import io
import os
import re
import sys

SKIP_DIRS = {"node_modules", "__pycache__", ".git", "daemons", "tools", "radae"}

_GROUP_RECORD_RE = re.compile(
    r'<record\s+id="([^"]+)"\s+model="res\.groups"', re.IGNORECASE
)


def _resolve_repo_root(given_path):
    """run_linters.py's own `dir_path` (computed from its __file__, which lives inside
    hams_shared/tools/) resolves to the hams_shared directory itself, not a real repo root --
    confirmed directly, not assumed, while debugging why this checker was silently finding zero
    modules (and printing a false "OK") when actually invoked from run_linters.py, despite
    working correctly in every direct/manual invocation this session used to build and verify
    it. Passing hams_shared itself to _find_modules/_find_sibling_repo finds nothing (hams_shared
    contains no Odoo modules, and neither "hams_open" nor "hams_com" exists as ITS OWN sibling).
    Detect that case by name and redirect to hams_shared's real parent repo instead."""
    given_path = os.path.abspath(given_path)
    if os.path.basename(given_path) == "hams_shared":
        return os.path.dirname(given_path)
    return given_path


def _find_sibling_repo(repo_root):
    """Mirrors check_model_extension_collisions.py's own sibling-repo resolution."""
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
    """Yields (module_name, module_dir, manifest_path) for every real Odoo module under roots."""
    for root in roots:
        for entry in sorted(os.listdir(root)):
            if entry in SKIP_DIRS or entry.startswith("."):
                continue
            module_dir = os.path.join(root, entry)
            manifest_path = os.path.join(module_dir, "__manifest__.py")
            if os.path.isdir(module_dir) and os.path.isfile(manifest_path):
                yield entry, module_dir, manifest_path


def _extract_data_list(manifest_path):
    """AST-only extraction of the manifest's own 'data' list, in file order. Returns [] if the
    manifest doesn't parse or has no 'data' key -- never executes the manifest."""
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=manifest_path)
    except (SyntaxError, UnicodeDecodeError, OSError):
        return []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key_node, value_node in zip(node.keys, node.values):
            if (
                isinstance(key_node, ast.Constant)
                and key_node.value == "data"
                and isinstance(value_node, ast.List)
            ):
                return [
                    elt.value
                    for elt in value_node.elts
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                ]
    return []


def _same_module_group_refs(csv_path, module_name):
    """Returns the set of same-module group external IDs (bare, without the module prefix) an
    ir.model.access.csv row references, or an empty set if this isn't an access CSV at all
    (no group_id:id column) or it doesn't parse."""
    try:
        with open(csv_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return set()

    reader = csv.DictReader(io.StringIO(content))
    if not reader.fieldnames or "group_id:id" not in reader.fieldnames:
        return set()

    refs = set()
    prefix = f"{module_name}."
    for row in reader:
        value = (row.get("group_id:id") or "").strip()
        if value.startswith(prefix):
            refs.add(value[len(prefix):])
    return refs


def _defined_group_ids(xml_path):
    try:
        with open(xml_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return set()
    return set(_GROUP_RECORD_RE.findall(content))


def _check_module(module_name, module_dir, manifest_path):
    """Returns a list of error strings for this one module, or an empty list if it's clean."""
    data_files = _extract_data_list(manifest_path)
    errors = []

    for i, rel_path in enumerate(data_files):
        if not rel_path.endswith(".csv"):
            continue
        csv_path = os.path.join(module_dir, rel_path)
        if not os.path.isfile(csv_path):
            continue
        needed_groups = _same_module_group_refs(csv_path, module_name)
        if not needed_groups:
            continue

        defined_before = set()
        for earlier_rel_path in data_files[:i]:
            if not earlier_rel_path.endswith(".xml"):
                continue
            earlier_path = os.path.join(module_dir, earlier_rel_path)
            if os.path.isfile(earlier_path):
                defined_before |= _defined_group_ids(earlier_path)

        missing = needed_groups - defined_before
        if missing:
            # Distinguish "defined later in data (wrong order)" from "never
            # defined anywhere in this module's own data list" for a more
            # actionable message -- the fix differs (reorder vs. add it).
            defined_anywhere = set()
            for any_rel_path in data_files:
                if any_rel_path.endswith(".xml"):
                    any_path = os.path.join(module_dir, any_rel_path)
                    if os.path.isfile(any_path):
                        defined_anywhere |= _defined_group_ids(any_path)

            for group_id in sorted(missing):
                if group_id in defined_anywhere:
                    errors.append(
                        f"{module_name}: '{rel_path}' (position {i}) references "
                        f"'{module_name}.{group_id}', which IS defined in this module but in an "
                        f"XML file listed LATER in 'data' -- move the group-defining XML file "
                        f"earlier than '{rel_path}' in {module_name}/__manifest__.py."
                    )
                else:
                    errors.append(
                        f"{module_name}: '{rel_path}' (position {i}) references "
                        f"'{module_name}.{group_id}', which is not defined by any XML file in "
                        f"this module's own 'data' list at all -- check for a typo, or add the "
                        f"missing <record model=\"res.groups\"> definition."
                    )
    return errors


def main():
    if len(sys.argv) < 2:
        print("Usage: check_access_csv_group_order.py <repo_root>", file=sys.stderr)
        return 1

    repo_root = _resolve_repo_root(sys.argv[1])
    roots = [repo_root]
    sibling = _find_sibling_repo(repo_root)
    if sibling:
        roots.append(sibling)

    all_errors = []
    for module_name, module_dir, manifest_path in _find_modules(roots):
        all_errors.extend(_check_module(module_name, module_dir, manifest_path))

    if all_errors:
        print(
            "\n[!] CI/CD FAILURE: ir.model.access.csv Group Definition Order Violation:"
        )
        for err in all_errors:
            print(f"    - {err}")
        print(
            "      [!] DIAGNOSTIC FOR AI: Odoo loads a module's 'data' files in the listed "
            "order. A CSV access rule referencing a group this same module defines must come "
            "AFTER the XML file defining that group, or the whole Odoo boot fails at that "
            "module -- not just that module's own tests."
        )
        return 1

    print("[*] ir.model.access.csv group definition order: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
