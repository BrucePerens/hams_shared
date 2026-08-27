#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
registry.test_cr Dead-Code Checker
-------------------------------------------------------------------------------------------
Catches `vars(self.env.registry).get("test_cr")` (and equivalent spellings) used as an
"am I running inside a test?" guard, most commonly to skip an `env.cr.commit()` that Odoo's
TransactionCase forbids inside tests. `registry.test_cr` is not a real attribute anywhere in
this installed Odoo version -- confirmed by grepping the entire installed odoo package: every
"test_cr" hit there is an unrelated local variable name in stock Odoo's own account-module
tests, never a registry attribute. `.get("test_cr")` on a real registry's `vars()` always
returns None, so any `is_test = ... .get("test_cr") is not None` idiom is always False --
dead code that silently never activates its own guard.

Found real, live consequences of this exact pattern twice in one night (2026-08-25): (1)
ham_relay_bridge's own copy of the idiom, whose corresponding env.cr.commit() had literally
never been exercised end-to-end by any test before that session, because an earlier,
independent bug (a missing ir.rule) always made the surrounding search() return zero records
and short-circuit the loop before ever reaching commit(); once that first bug was fixed, the
dead is_test guard let the AssertionError through immediately. (2) user_websites' own five
occurrences of the identical idiom (blog_post.py, res_users.py x5, user_websites_groups.py,
website_page.py) are the same latent bug, just not (yet) triggering a live failure there
because their own GDPR/erasure tests are HttpCase-based -- a real HTTP request gets its own
separate, unpatched cursor, so the forbidden-commit path is never actually reached that way.

There is no legitimate use of this idiom (unlike some other burn-listed patterns, which have
rare, deliberate exceptions) -- registry.test_cr has never existed as a real attribute, so
there is no escape-hatch comment for this check. The fix is always the same: find a real way to
detect test mode (or, more simply, ask whether the commit()/batching this guard was protecting
is even necessary for this model's realistic data scale -- see ham_relay_bridge's own fix,
which just removed the unneeded batching/commit machinery entirely).

Usage: check_registry_test_cr_usage.py <repo_root>
"""

import os
import re
import sys

SKIP_DIRS = {"node_modules", "__pycache__", ".git", "daemons", "tools", "radae"}

_PATTERN = re.compile(r"registry\b.*\.get\(\s*['\"]test_cr['\"]\s*\)")


def _resolve_repo_root(given_path):
    given_path = os.path.abspath(given_path)
    if os.path.basename(given_path) == "hams_shared":
        return os.path.dirname(given_path)
    return given_path


def _resolve_repo_roots(given_path):
    """_resolve_repo_root above only ever redirects to ONE repo (hams_open) -- but real Odoo
    Python source spans both hams_open and hams_com. Same sibling-repo shape as the other fixed
    checkers (check_untyped_utility_files.py, check_self_writeable_field_tests.py, ...)."""
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


def check_registry_test_cr_usage(repo_root):
    violations = []
    for root, dirs, files in os.walk(repo_root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for filename in files:
            if not filename.endswith(".py"):
                continue
            path = os.path.join(root, filename)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for lineno, line in enumerate(f, 1):
                        if _PATTERN.search(line):
                            violations.append(
                                f"{os.path.relpath(path, repo_root)}:{lineno} "
                                f"registry.test_cr is not a real Odoo attribute in this "
                                f"installed version -- this is_test check is always False. "
                                f"Find a real test-mode detection, or (more often the right "
                                f"answer) remove the commit()/batching it was guarding."
                            )
            except UnicodeDecodeError as e:
                print(f"Warning: UnicodeDecodeError reading {path}: {e}")
    return violations


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: check_registry_test_cr_usage.py <repo_root>")
        sys.exit(1)

    violations = []
    for repo_root in _resolve_repo_roots(sys.argv[1]):
        violations.extend(check_registry_test_cr_usage(repo_root))

    if violations:
        print("❌ registry.test_cr dead-code violations:")
        for v in violations:
            print(f"  - {v}")
        sys.exit(1)

    print("✅ No registry.test_cr dead-code usage found.")
    sys.exit(0)
