#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Hoot Runner Coverage Linter
----------------------------
A module can register a *.test.js file in its __manifest__.py's
"web.assets_unit_tests" bundle -- making it syntactically valid, bundled,
and loadable by hoot -- without anything in the Python test suite ever
actually executing it via browser_js(). That gap let real, broken hoot
suites (a whole class failing on a read-only `window.fetch` reassignment,
found 2026-09-09) sit unnoticed for a long time: nothing in CI ever ran
them, so nothing ever reported red. This check closes the gap at the
opposite end from check_test_tags.py -- it doesn't check that existing
test classes are tagged correctly, it checks that a hoot suite REGISTERED
in a manifest has at least one Python test file that actually runs it.

The check is module-level, not per-file: it flags a module that has ANY
*.test.js file in web.assets_unit_tests but NO tests/test_*.py file
containing a browser_js() call with a "[HOOT]" success_signal anywhere in
the module. It does not verify per-suite tag coverage (a module with ten
hoot test files and one runner that only covers one tag currently passes)
-- that finer-grained gap is real but needs a JS-side tag cross-reference
this check doesn't attempt.
"""

import ast
import os
import sys


def find_hoot_test_files(manifest_dict):
    assets = manifest_dict.get("assets", {})
    unit_test_bundle = assets.get("web.assets_unit_tests", [])
    return [f for f in unit_test_bundle if isinstance(f, str) and f.endswith(".test.js")]


def module_has_hoot_runner(module_path):
    tests_dir = os.path.join(module_path, "tests")
    if not os.path.isdir(tests_dir):
        return False
    for root, dirs, files in os.walk(tests_dir):
        for f in files:
            if not f.endswith(".py"):
                continue
            filepath = os.path.join(root, f)
            try:
                with open(filepath, "r", encoding="utf-8") as fh:
                    content = fh.read()
            except (OSError, UnicodeDecodeError):
                continue
            if "browser_js(" in content and "[HOOT]" in content:
                return True
    return False


def main():
    if len(sys.argv) < 2:
        print("Usage: check_hoot_runner_coverage.py <repository_root> [more_roots...]")
        sys.exit(1)

    ignore_dirs = {
        ".git",
        "node_modules",
        "venv",
        "env",
        ".venv",
        "__pycache__",
        ".agents",
        "agents",
        "target",
        "radae",
        "site-packages",
        ".claude",
    }

    violations = []

    for repo_root in sys.argv[1:]:
        for root, dirs, files in os.walk(repo_root):
            dirs[:] = [d for d in dirs if d not in ignore_dirs]
            if "__manifest__.py" not in files:
                continue

            manifest_path = os.path.join(root, "__manifest__.py")
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    manifest_source = f.read()
                    tree = ast.parse(manifest_source, filename=manifest_path)
            except (OSError, SyntaxError):
                continue

            # Guardrail Preservation Mandate, matching check_test_tags.py's
            # own precedent: an explicit, reviewed exception for a module
            # that's already known to be missing a runner (tracked as a
            # real follow-up, not silently swept under the rug) shouldn't
            # be a hard failure every time run_linters.py runs.
            if "# burn-ignore-hoot-runner-coverage" in manifest_source:
                continue

            manifest_dict = None
            for node in tree.body:
                if isinstance(node, ast.Expr) and isinstance(node.value, ast.Dict):
                    try:
                        manifest_dict = ast.literal_eval(node.value)
                    except ValueError:
                        continue
                    break
            if manifest_dict is None:
                continue

            hoot_test_files = find_hoot_test_files(manifest_dict)
            if not hoot_test_files:
                continue

            if not module_has_hoot_runner(root):
                mod_name = os.path.basename(root)
                violations.append(
                    f"🚨 HOOT RUNNER COVERAGE VIOLATION: module '{mod_name}' registers "
                    f"{len(hoot_test_files)} *.test.js file(s) in web.assets_unit_tests "
                    f"({', '.join(hoot_test_files)}) but has no tests/test_*.py file that "
                    f"calls browser_js(...) with a \"[HOOT]\" success_signal -- these hoot "
                    f"suites are never actually executed by the Python test suite. Add a "
                    f"runner following ham_dx_cluster/tests/test_dx_cluster_widget_hoot.py's "
                    f"pattern."
                )

    if violations:
        for v in violations:
            print(v)
        sys.exit(1)

    print("[+] Hoot Runner Coverage Linter: all modules with hoot unit tests have a runner.")


if __name__ == "__main__":
    main()
