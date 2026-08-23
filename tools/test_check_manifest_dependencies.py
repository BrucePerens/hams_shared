#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_manifest_dependencies.py.

Unlike check_dependency_cycles.py, this script's entire logic lives inside
main() with no extracted pure functions -- refactoring it just to make it
importable would risk changing behavior in a script every module's CI run
depends on. Instead these tests invoke it as a real subprocess against real
temp-directory module fixtures (manifest + JS files on disk) and assert on
exit code and stdout, the same black-box contract CI itself relies on.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "check_manifest_dependencies.py")


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _run(repo_root):
    result = subprocess.run(
        [sys.executable, _SCRIPT, repo_root],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.returncode, result.stdout + result.stderr


class CheckManifestDependenciesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _manifest(self, mod, depends=None, description="A real description.", extra=""):
        _write(
            os.path.join(self.tmp, mod, "__manifest__.py"),
            "{\n"
            f"    'depends': {depends or []!r},\n"
            f"    'description': {description!r},\n"
            f"{extra}"
            "}\n",
        )

    def test_clean_module_with_no_js_passes(self):
        self._manifest("mod_a")
        code, out = _run(self.tmp)
        self.assertEqual(code, 0, out)

    def test_js_import_from_a_declared_dependency_passes(self):
        self._manifest("mod_a", depends=["mod_b"])
        self._manifest("mod_b")
        _write(
            os.path.join(self.tmp, "mod_a", "static", "src", "js", "main.js"),
            "import { thing } from '@mod_b/js/thing';\n",
        )
        code, out = _run(self.tmp)
        self.assertEqual(code, 0, out)

    def test_js_import_from_an_undeclared_module_is_a_violation(self):
        self._manifest("mod_a", depends=[])
        self._manifest("mod_b")
        _write(
            os.path.join(self.tmp, "mod_a", "static", "src", "js", "main.js"),
            "import { thing } from '@mod_b/js/thing';\n",
        )
        code, out = _run(self.tmp)
        self.assertEqual(code, 1)
        self.assertIn("MANIFEST DEPENDENCY VIOLATION", out)

    def test_importing_own_module_never_needs_a_depends_entry(self):
        self._manifest("mod_a", depends=[])
        _write(
            os.path.join(self.tmp, "mod_a", "static", "src", "js", "main.js"),
            "import { thing } from '@mod_a/js/thing';\n",
        )
        code, out = _run(self.tmp)
        self.assertEqual(code, 0, out)

    def test_importing_core_odoo_aliases_never_needs_a_depends_entry(self):
        self._manifest("mod_a", depends=[])
        _write(
            os.path.join(self.tmp, "mod_a", "static", "src", "js", "main.js"),
            "import { Component } from '@odoo/owl';\n"
            "import { registry } from '@web/core/registry';\n",
        )
        code, out = _run(self.tmp)
        self.assertEqual(code, 0, out)

    def test_missing_description_is_a_violation(self):
        self._manifest("mod_a", description="")
        code, out = _run(self.tmp)
        self.assertEqual(code, 1)
        self.assertIn("MANIFEST DESCRIPTION VIOLATION", out)

    def test_invalid_knowledge_doc_category_is_a_violation(self):
        self._manifest(
            "mod_a",
            extra="    'knowledge_docs': [{'category': 'not_a_real_category'}],\n",
        )
        code, out = _run(self.tmp)
        self.assertEqual(code, 1)
        self.assertIn("KNOWLEDGE DOC CATEGORY VIOLATION", out)

    def test_a_recognized_knowledge_doc_category_is_not_a_violation(self):
        self._manifest(
            "mod_a", extra="    'knowledge_docs': [{'category': 'workspace'}],\n"
        )
        code, out = _run(self.tmp)
        self.assertEqual(code, 0, out)

    def test_a_syntax_error_in_a_manifest_is_reported_and_fails(self):
        _write(
            os.path.join(self.tmp, "broken_mod", "__manifest__.py"),
            "{ this is not valid python",
        )
        code, out = _run(self.tmp)
        self.assertEqual(code, 1)
        self.assertIn("ERROR parsing", out)

    def test_a_test_bundle_file_importing_a_backend_bundled_utility_is_fine(self):
        # web.assets_backend is itself in the allowed set for a test-bundle
        # import (test_bundles | prod_bundles) -- only an import of
        # something registered in neither counts as cross-contamination.
        self._manifest(
            "mod_a",
            extra=(
                "    'assets': {\n"
                "        'web.assets_tests': ['mod_a/static/src/js/my_test.js'],\n"
                "        'web.assets_backend': ['mod_a/static/src/js/prod_only.js'],\n"
                "    },\n"
            ),
        )
        _write(
            os.path.join(self.tmp, "mod_a", "static", "src", "js", "my_test.js"),
            "import { helper } from '@mod_a/js/prod_only';\n",
        )
        code, out = _run(self.tmp)
        self.assertEqual(code, 0, out)

    def test_a_test_bundle_file_importing_a_file_in_no_recognized_bundle_is_cross_contamination(self):
        # Registered in a bundle name outside both test_bundles and
        # prod_bundles (e.g. a typo, or a bundle this script doesn't know
        # about) -- this is the actual trigger condition, verified against
        # the real script rather than assumed from its docstring/messages.
        self._manifest(
            "mod_a",
            extra=(
                "    'assets': {\n"
                "        'web.assets_tests': ['mod_a/static/src/js/my_test.js'],\n"
                "        'web.assets_weird_other': ['mod_a/static/src/js/prod_only.js'],\n"
                "    },\n"
            ),
        )
        _write(
            os.path.join(self.tmp, "mod_a", "static", "src", "js", "my_test.js"),
            "import { helper } from '@mod_a/js/prod_only';\n",
        )
        code, out = _run(self.tmp)
        self.assertEqual(code, 1)
        self.assertIn("ASSET BUNDLE CROSS-CONTAMINATION", out)

    def test_a_prod_bundle_file_importing_a_test_only_asset_is_cross_contamination(self):
        self._manifest(
            "mod_a",
            extra=(
                "    'assets': {\n"
                "        'web.assets_backend': ['mod_a/static/src/js/main.js'],\n"
                "        'web.assets_tests': ['mod_a/static/src/js/test_only.js'],\n"
                "    },\n"
            ),
        )
        _write(
            os.path.join(self.tmp, "mod_a", "static", "src", "js", "main.js"),
            "import { helper } from '@mod_a/js/test_only';\n",
        )
        code, out = _run(self.tmp)
        self.assertEqual(code, 1)
        self.assertIn("ASSET BUNDLE CROSS-CONTAMINATION", out)

    def test_importing_a_file_registered_in_no_bundle_at_all_is_not_flagged(self):
        # Documents real, verified behavior, not a claim it's correct: an
        # imported file that never appears in file_to_bundles (not merely
        # in an unrecognized bundle, but absent from `assets` entirely)
        # produces imported_bundles == [], which is falsy, so the "if
        # imported_bundles and not any(...)" guard never fires. A
        # completely unregistered import -- arguably the most likely real
        # test-runner crash case -- currently passes silently. Flagged as
        # a tangential finding in night_shift_todo.md rather than changed
        # here, since narrowing a shared pre-commit gate's behavior is a
        # deliberate call, not a mechanical test-coverage add.
        self._manifest(
            "mod_a",
            extra="    'assets': {'web.assets_tests': ['mod_a/static/src/js/my_test.js']},\n",
        )
        _write(
            os.path.join(self.tmp, "mod_a", "static", "src", "js", "my_test.js"),
            "import { helper } from '@mod_a/js/totally_unregistered';\n",
        )
        code, out = _run(self.tmp)
        self.assertEqual(code, 0, out)

    def test_a_prod_bundle_file_importing_another_prod_bundle_file_is_fine(self):
        self._manifest(
            "mod_a",
            extra=(
                "    'assets': {\n"
                "        'web.assets_backend': [\n"
                "            'mod_a/static/src/js/main.js',\n"
                "            'mod_a/static/src/js/helper.js',\n"
                "        ],\n"
                "    },\n"
            ),
        )
        _write(
            os.path.join(self.tmp, "mod_a", "static", "src", "js", "main.js"),
            "import { helper } from '@mod_a/js/helper';\n",
        )
        code, out = _run(self.tmp)
        self.assertEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main()
