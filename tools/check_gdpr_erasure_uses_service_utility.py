#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
GDPR Erasure Hand-Rolled unlink() Checker
-------------------------------------------------------------------------------------------
Every module's `_execute_gdpr_erasure()` override (the established, documented contract --
see compliance/models/res_users.py's base no-op definition) is expected to delete records via
`zero_sudo.security.utils._erase_via_service_account(model_name, domain, service_xml_id)`, not
a hand-rolled `self.env[...].with_user(svc_uid).search(...)` followed by `.unlink()`.

The hand-rolled shape has a real, already-hit failure mode: a service account's own group
memberships can pick up an UNRELATED, restrictive ir.rule scoped to one of its groups (e.g. the
GDPR service account deliberately holds base.group_user for one unrelated, documented reason,
which can incidentally match some other module's "your own records only" rule) -- the account's
own search() then silently sees fewer records than actually exist, and a hand-rolled
search()+unlink() silently deletes a partial (or empty) subset with no error anywhere. Found
live in ham_relay_bridge: this made GDPR erasure of relay nodes a permanent no-op until a test
happened to check the actual outcome. `_erase_via_service_account` exists specifically to catch
this loudly instead (see its own docstring for the mechanism) -- using it instead of hand-rolling
the same operation is how every future erasure implementation inherits that protection for free.

Detection: any `_execute_gdpr_erasure` method (the established name every one of the ~13 current
overrides uses) that calls `.unlink()` anywhere in its body without also calling
`_erase_via_service_account` anywhere in that same body. AST-based, not text/regex, so it isn't
fooled by formatting -- deliberately does NOT try to detect the with_user()/search() shape
precisely (real call sites vary: some assign to a variable first, some access a relational field
directly instead of calling .search()) since ANY .unlink() inside this specific, well-known
method that isn't routed through the blessed utility is the pattern to flag, regardless of its
exact shape.

Escape hatch: a `.unlink()` call line carrying `# audit-ignore-gdpr-hand-rolled-unlink` is
exempted -- for the one legitimate case found so far (user_websites' website.page/blog.post/
blog.blog erasure), which needs real production-scale batching, savepoint-protected retry on
concurrent-update errors, and mid-loop commits for datasets that can run into the thousands of
records across users. `_erase_via_service_account` deliberately doesn't replicate that machinery
(it's built for the common case: a single user's own small, personal-scale dataset), so forcing
that one caller onto it would be a regression, not a fix. Use the same marker (with a comment
explaining why) for any other genuinely-batched exception found later -- don't loosen the check
itself for a plain, unbatched hand-rolled unlink() that has no such reason.

Usage: check_gdpr_erasure_uses_service_utility.py <repo_root>
"""

import ast
import os
import sys

SKIP_DIRS = {"node_modules", "__pycache__", ".git", "daemons", "tools", "radae"}
IGNORE_MARKER = "audit-ignore-gdpr-hand-rolled-unlink"


def _resolve_repo_root(given_path):
    given_path = os.path.abspath(given_path)
    if os.path.basename(given_path) == "hams_shared":
        return os.path.dirname(given_path)
    return given_path


def _resolve_repo_roots(given_path):
    """_resolve_repo_root above only ever redirects to ONE repo (hams_open) -- but real
    _execute_gdpr_erasure() overrides exist in both repos (17 files in hams_com, 7 in hams_open,
    confirmed directly), so run_linters.py's own actual invocation was only ever checking the
    smaller half. Same sibling-repo shape as the other fixed checkers."""
    repo_root = _resolve_repo_root(given_path)
    roots = [repo_root]
    sibling_name = "hams_open" if os.path.basename(repo_root) != "hams_open" else "hams_com"
    sibling = os.path.abspath(os.path.join(repo_root, "..", sibling_name))
    if os.path.isdir(sibling) and any(
        os.path.isfile(os.path.join(sibling, d, "__manifest__.py"))
        for d in os.listdir(sibling)
        if os.path.isdir(os.path.join(sibling, d))
    ):
        roots.append(sibling)
    return roots


def _calls_method_named(node, name):
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr == name
        ):
            return True
    return False


def _unignored_unlink_calls(node, source_lines):
    found = []
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr == "unlink"
        ):
            line = source_lines[child.lineno - 1] if child.lineno <= len(source_lines) else ""
            if IGNORE_MARKER not in line:
                found.append(child)
    return found


def _check_file(path, repo_root):
    violations = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
    except UnicodeDecodeError as e:
        print(f"Warning: UnicodeDecodeError reading {path}: {e}")
        return violations

    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError:
        return violations

    source_lines = source.splitlines()

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name != "_execute_gdpr_erasure":
            continue
        unignored = _unignored_unlink_calls(node, source_lines)
        if unignored and not _calls_method_named(node, "_erase_via_service_account"):
            for call in unignored:
                violations.append(
                    f"{os.path.relpath(path, repo_root)}:{call.lineno} "
                    f"_execute_gdpr_erasure() calls .unlink() directly instead of "
                    f"zero_sudo.security.utils._erase_via_service_account(model_name, "
                    f"domain, service_xml_id) -- a hand-rolled search()+unlink() silently "
                    f"deletes a partial/empty subset if the service account's groups "
                    f"incidentally match an unrelated restrictive ir.rule (found live in "
                    f"ham_relay_bridge). Use the utility instead, or add "
                    f"'# {IGNORE_MARKER}' with a comment explaining why if this genuinely "
                    f"needs its own batching the utility doesn't provide."
                )
    return violations


def check_gdpr_erasure_uses_service_utility(repo_root):
    violations = []
    for root, dirs, files in os.walk(repo_root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for filename in files:
            if not filename.endswith(".py"):
                continue
            path = os.path.join(root, filename)
            # The utility's own implementation is exempt -- it IS the blessed
            # unlink() call every other _execute_gdpr_erasure should route through.
            if os.path.basename(path) == "security_utils.py":
                continue
            violations.extend(_check_file(path, repo_root))
    return violations


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: check_gdpr_erasure_uses_service_utility.py <repo_root>")
        sys.exit(1)

    violations = []
    for repo_root in _resolve_repo_roots(sys.argv[1]):
        violations.extend(check_gdpr_erasure_uses_service_utility(repo_root))

    if violations:
        print("❌ GDPR erasure hand-rolled unlink() violations:")
        for v in violations:
            print(f"  - {v}")
        sys.exit(1)

    print("✅ Every _execute_gdpr_erasure() routes unlink() through the shared service utility.")
    sys.exit(0)
