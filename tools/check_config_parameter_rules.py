#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
ir.config_parameter Access Checker
-----------------------------------
A module's ir.model.access.csv row on ir.config_parameter gives its group the whole table (every secret and setting:
stripe.secret_key, web.base.url, auth_signup.*). Such a row is only allowed when the same module also has an ir.rule on
ir.config_parameter for that group, naming the keys the group may use. The two config service groups (the gatekeepers behind
ham_base's get_param/set_param allow-lists) are the one exception.

Usage: check_config_parameter_rules.py <repo_root>   (scans hams_com and hams_open both)
"""

import csv
import io
import os
import re
import sys
import xml.etree.ElementTree as ET

import check_target
import lint_exec
from tree_files import tree_walk
from check_res_config_settings_access import _find_access_csvs, _resolve_repo_root

TARGET_MODEL_IDS = {"model_ir_config_parameter", "base.model_ir_config_parameter"}
GATEKEEPER_GROUPS = {"ham_base.group_config_service", "zero_sudo.group_config_service"}
REF_RE = re.compile(r"""ref\(\s*['"]([\w.]+)['"]\s*\)""")
SKIP_DIRS = {"node_modules", "__pycache__", ".git", "tests", "static"}


def _qualify(xml_id, module):
    return xml_id if "." in xml_id else f"{module}.{xml_id}"


def _ruled_groups(module_dir, module):
    """Groups named by an ir.rule on ir.config_parameter anywhere in the module's XML."""
    groups = set()
    for dirpath, _dirs, filenames in tree_walk(module_dir, ".xml", SKIP_DIRS, skip_hidden=True, untracked=True):
        for name in filenames:
            if not name.endswith(".xml"):
                continue
            try:
                tree = ET.parse(os.path.join(dirpath, name))
            except (ET.ParseError, OSError):
                continue
            for rec in tree.iter("record"):
                if rec.get("model") != "ir.rule":
                    continue
                fields = {f.get("name"): f for f in rec.findall("field")}
                model = fields.get("model_id")
                if model is None or (model.get("ref") or "") not in TARGET_MODEL_IDS:
                    continue
                if "groups" in fields:
                    groups.update(_qualify(ref, module) for ref in REF_RE.findall(fields["groups"].get("eval") or ""))
    return groups


def _check_csv(module_name, csv_path):
    """Error strings for the rows of this csv that grant ir.config_parameter to a group without a key rule."""
    try:
        with open(csv_path, "r", encoding="utf-8") as handle:
            content = handle.read()
    except (OSError, UnicodeDecodeError) as error:
        return [f"{module_name}: '{csv_path}' could not be read ({error}), so its ir.config_parameter grants are unchecked"]
    reader = csv.DictReader(io.StringIO(content))
    model_col = next((c for c in (reader.fieldnames or ()) if c in ("model_id:id", "model_id/id")), None)
    if model_col is None:
        return [f"{module_name}: '{csv_path}' has no model_id:id (or model_id/id) column, so its ir.config_parameter grants are unchecked"]
    group_col = next((c for c in reader.fieldnames if c in ("group_id:id", "group_id/id")), "group_id:id")
    rows = [r for r in reader if (r.get(model_col) or "").strip() in TARGET_MODEL_IDS]
    if not rows:
        return []
    module_dir = os.path.dirname(os.path.dirname(csv_path))
    ruled = _ruled_groups(module_dir, module_name)
    errors = []
    for row in rows:
        group = _qualify((row.get(group_col) or "").strip(), module_name)
        if group in GATEKEEPER_GROUPS or group in ruled:
            continue
        errors.append(
            f"{module_name}: '{csv_path}' row '{row.get('id', '?')}' gives '{group}' the whole ir.config_parameter table; "
            f"add an ir.rule on base.model_ir_config_parameter for that group that names its keys"
        )
    return errors


def main():
    if len(sys.argv) < 2:
        print("Usage: check_config_parameter_rules.py <repo_root>", file=sys.stderr)
        return 1
    check_target.require_directory(sys.argv[1], "check_config_parameter_rules")
    repo_root = _resolve_repo_root(sys.argv[1])
    roots = [repo_root]
    sibling = lint_exec.sibling_repo_root(repo_root, lint_exec.has_odoo_module)
    if sibling:
        roots.append(sibling)
    csvs = list(_find_access_csvs(roots))
    check_target.require_scanned(len(csvs), ", ".join(roots), "check_config_parameter_rules", "module ir.model.access.csv files")
    errors = []
    for module_name, csv_path in csvs:
        errors.extend(_check_csv(module_name, csv_path))
    if errors:
        print("\n[!] CI/CD FAILURE: ir.config_parameter table-wide grant without a key rule:")
        for err in errors:
            print(f"    - {err}")
        return 1
    print("[*] ir.config_parameter grants all have a key rule: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
