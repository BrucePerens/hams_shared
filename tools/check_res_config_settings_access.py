#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
res.config.settings Access Grant Checker
-----------------------------------------
Mechanical guard for ADR-0096 ("res.config.settings Authorization Is All-or-Nothing, By Design"):
no module's own ir.model.access.csv may grant model-level access to res.config.settings to any
group other than base.group_system.

Born from a real, live security hole: night_shift_todo.md's "saving ANY Settings page can crash
with an AccessError" investigation found user_websites/security/ir.model.access.csv granted
group_user_websites_administrator -- a content-moderation-tier role that does not imply
base.group_system -- full read/write/create/unlink on the ENTIRE shared res.config.settings
TransientModel. ir.model.access.csv grants are per (model, group), never per field, so that row
was never actually scoped to user_websites' own 3 settings fields; it handed that role read/write
on every field ANY installed module merges onto the one shared model (confirmed concretely:
distributed_redis_cache's redis_password, cloudflare's cloudflare_api_token). base's own
ir.model.access.csv already grants res.config.settings to base.group_system alone -- every module
that needs a Settings field should rely on that inherited grant, exactly like every OTHER
Settings-contributing module in both repos already does (ham_dns, ham_training, web_map, caching,
cloudflare, distributed_redis_cache, hams_base, hams_s3, advertising all add zero access-csv rows
of their own for this model). A module adding its own row naming a different group is the same
mistake recurring, not a legitimate narrower delegation -- there is no such thing as "let this
role edit just its own Settings fields," because the grant is never actually that narrow.

Usage: check_res_config_settings_access.py <repo_root>
Always scans the full manifest graph (both hams_com and hams_open), like
check_access_csv_group_order.py / check_model_extension_collisions.py -- a module's own access
grant is a property of that module regardless of which repo invoked the check from.
"""

import csv
import io
import os
import sys

SKIP_DIRS = {"node_modules", "__pycache__", ".git", "daemons", "tools", "radae"}

ALLOWED_GROUP = "base.group_system"
TARGET_MODEL_IDS = {"model_res_config_settings", "base.model_res_config_settings"}


def _resolve_repo_root(given_path):
    """Mirrors check_access_csv_group_order.py's own hams_shared-invocation redirect: run_linters.py
    passes hams_shared's own directory, which contains no Odoo modules and has no
    "hams_open"/"hams_com" sibling of its own -- redirect to its real parent repo instead."""
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


def _find_access_csvs(roots):
    """Yields (module_name, csv_path) for every security/ir.model.access.csv under roots -- walks
    the whole module tree rather than trusting each manifest's own 'data' list, since a stray
    access-csv row is a real access grant the moment Odoo loads any CSV naming it, regardless of
    whether this checker can prove the manifest lists it (deliberately more paranoid than
    check_access_csv_group_order.py, which only cares about load ORDER of files already known to
    be listed)."""
    for root in roots:
        for entry in sorted(os.listdir(root)):
            if entry in SKIP_DIRS or entry.startswith("."):
                continue
            module_dir = os.path.join(root, entry)
            if not os.path.isdir(module_dir):
                continue
            if not os.path.isfile(os.path.join(module_dir, "__manifest__.py")):
                continue
            for dirpath, dirnames, filenames in os.walk(module_dir):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
                for filename in filenames:
                    if filename == "ir.model.access.csv":
                        yield entry, os.path.join(dirpath, filename)


def _check_csv(module_name, csv_path):
    """Returns a list of error strings for this one file, or an empty list if it's clean."""
    try:
        with open(csv_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return []

    reader = csv.DictReader(io.StringIO(content))
    if not reader.fieldnames or "model_id:id" not in reader.fieldnames:
        return []

    errors = []
    for row in reader:
        model_id = (row.get("model_id:id") or "").strip()
        if model_id not in TARGET_MODEL_IDS:
            continue
        group = (row.get("group_id:id") or "").strip()
        if group != ALLOWED_GROUP:
            errors.append(
                f"{module_name}: '{csv_path}' row '{row.get('id', '?')}' grants "
                f"res.config.settings access to '{group}', not '{ALLOWED_GROUP}' -- per "
                f"ADR-0096, no module may grant this shared TransientModel to any group other "
                f"than base.group_system, since the grant is never actually scoped to that "
                f"module's own fields (ir.model.access.csv is per model, not per field)."
            )
    return errors


def main():
    if len(sys.argv) < 2:
        print("Usage: check_res_config_settings_access.py <repo_root>", file=sys.stderr)
        return 1

    repo_root = _resolve_repo_root(sys.argv[1])
    roots = [repo_root]
    sibling = _find_sibling_repo(repo_root)
    if sibling:
        roots.append(sibling)

    all_errors = []
    for module_name, csv_path in _find_access_csvs(roots):
        all_errors.extend(_check_csv(module_name, csv_path))

    if all_errors:
        print("\n[!] CI/CD FAILURE: res.config.settings Over-Broad Access Grant:")
        for err in all_errors:
            print(f"    - {err}")
        print(
            "      [!] DIAGNOSTIC FOR AI: read hams_shared/docs/adrs/"
            "0096_res_config_settings_authorization_model.md before touching this. "
            "res.config.settings write access is base.group_system only; a narrower role that "
            "needs to manage one of its own settings values should not be granted access to this "
            "shared model at all -- use Odoo's own Users & Groups mechanism (res.groups' own form "
            "view) for delegating a role, not a bespoke field on Settings."
        )
        return 1

    print("[*] res.config.settings access grants: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
