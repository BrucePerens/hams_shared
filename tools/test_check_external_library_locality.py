#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_external_library_locality.py.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_external_library_locality as chk  # noqa: E402

_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "check_external_library_locality.py")


def _write(path, content=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


class FindViolationsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_js_file_under_static_lib_in_a_non_external_module_is_flagged(self):
        _write(os.path.join(self.tmp, "ham_shack", "static", "src", "lib", "foo.js"))
        violations = chk.find_violations(self.tmp)
        self.assertEqual(len(violations), 1)
        self.assertIn("foo.js", violations[0])

    def test_a_js_file_under_static_node_modules_is_flagged(self):
        _write(os.path.join(self.tmp, "ham_shack", "static", "node_modules", "d3", "d3.js"))
        violations = chk.find_violations(self.tmp)
        self.assertEqual(len(violations), 1)

    def test_the_same_layout_inside_the_external_module_is_never_flagged(self):
        _write(os.path.join(self.tmp, "external", "static", "src", "lib", "foo.js"))
        self.assertEqual(chk.find_violations(self.tmp), [])

    def test_a_non_js_css_file_in_lib_is_not_a_library_and_is_not_flagged(self):
        _write(os.path.join(self.tmp, "ham_shack", "static", "src", "lib", "data.json"))
        self.assertEqual(chk.find_violations(self.tmp), [])

    def test_a_css_file_under_lib_is_also_flagged(self):
        _write(os.path.join(self.tmp, "ham_shack", "static", "src", "lib", "foo.css"))
        violations = chk.find_violations(self.tmp)
        self.assertEqual(len(violations), 1)

    def test_a_js_file_outside_static_entirely_is_never_flagged(self):
        _write(os.path.join(self.tmp, "ham_shack", "lib", "foo.js"))
        self.assertEqual(chk.find_violations(self.tmp), [])

    def test_first_party_js_under_static_but_not_lib_or_node_modules_is_never_flagged(self):
        _write(os.path.join(self.tmp, "ham_shack", "static", "src", "js", "web_shack.js"))
        self.assertEqual(chk.find_violations(self.tmp), [])

    def test_an_explicitly_excluded_file_is_never_flagged(self):
        _write(
            os.path.join(self.tmp, "ham_satellite", "static", "src", "lib", "three.min.js")
        )
        self.assertEqual(chk.find_violations(self.tmp), [])

    def test_the_radae_directory_is_never_walked(self):
        _write(
            os.path.join(
                self.tmp, "some_daemon", "radae", "static", "src", "lib", "vendored.js"
            )
        )
        self.assertEqual(chk.find_violations(self.tmp), [])

    def test_multiple_violations_across_modules_are_all_reported(self):
        _write(os.path.join(self.tmp, "mod_a", "static", "src", "lib", "a.js"))
        _write(os.path.join(self.tmp, "mod_b", "static", "src", "lib", "b.js"))
        violations = chk.find_violations(self.tmp)
        self.assertEqual(len(violations), 2)


class MainIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self):
        result = subprocess.run(
            [sys.executable, _SCRIPT, self.tmp], capture_output=True, text=True, timeout=30
        )
        return result.returncode, result.stdout + result.stderr

    def test_a_clean_repo_passes(self):
        _write(os.path.join(self.tmp, "external", "static", "src", "lib", "foo.js"))
        code, out = self._run()
        self.assertEqual(code, 0, out)
        self.assertIn("no vendored libraries outside", out)

    def test_a_violation_fails_with_a_fix_suggestion(self):
        _write(os.path.join(self.tmp, "ham_shack", "static", "src", "lib", "foo.js"))
        code, out = self._run()
        self.assertEqual(code, 1)
        self.assertIn("VENDORED LIBRARY OUTSIDE", out)
        self.assertIn("external/static/src/node_modules", out)


class ResolveRepoRootTests(unittest.TestCase):
    # Regression test for a real bug found the same night check_model_extension_collisions.py's
    # own identical bug was found and fixed: run_linters.py's own dir_path resolves to
    # .../hams_shared, not a real repo root. This checker's own zero-violations result happened
    # to be correct by coincidence either way (hams_shared genuinely has no vendored-library
    # files), but it was still scanning the wrong, much smaller tree.
    def test_a_hams_shared_path_redirects_to_its_parent_repo(self):
        fake_repo = os.path.join(os.sep, "some", "workspace", "some_repo")
        self.assertEqual(
            chk._resolve_repo_root(os.path.join(fake_repo, "hams_shared")),
            fake_repo,
        )

    def test_a_real_repo_root_passes_through_unchanged(self):
        fake_repo = os.path.join(os.sep, "some", "workspace", "some_repo")
        self.assertEqual(chk._resolve_repo_root(fake_repo), fake_repo)


class ResolveRepoRootsTests(unittest.TestCase):
    # Regression test for the second half of the same bug class, found 2026-08-27:
    # _resolve_repo_root's redirect only ever lands on ONE repo (hams_shared's literal parent) --
    # but EXCLUDED_FILES above names real hams_com paths (ham_satellite/...), meaning this
    # checker's real scope always spanned both repos and the single-repo redirect left hams_com
    # entirely unscanned via run_linters.py's actual invocation.
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.workspace = os.path.join(self.tmp, "workspace")
        os.makedirs(self.workspace)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_repo(self, name, module_names=()):
        repo = os.path.join(self.workspace, name)
        for module_name in module_names:
            _write(os.path.join(repo, module_name, "__manifest__.py"), "{}")
        return repo

    def test_hams_shared_input_appends_the_real_hams_com_sibling(self):
        self._make_repo("hams_open", module_names=["zero_sudo"])
        hams_com = self._make_repo("hams_com", module_names=["ham_base"])
        hams_shared = os.path.join(self.workspace, "hams_open", "hams_shared")
        os.makedirs(hams_shared)
        roots = chk._resolve_repo_roots(hams_shared)
        self.assertEqual(roots, [os.path.join(self.workspace, "hams_open"), hams_com])

    def test_a_real_repo_root_with_no_odoo_sibling_present_scans_alone(self):
        repo = self._make_repo("hams_open", module_names=["zero_sudo"])
        self.assertEqual(chk._resolve_repo_roots(repo), [repo])

    def test_a_sibling_directory_with_no_manifest_py_anywhere_is_not_treated_as_a_repo(self):
        repo = self._make_repo("hams_open", module_names=["zero_sudo"])
        os.makedirs(os.path.join(self.workspace, "hams_com", "not_a_module"))
        self.assertEqual(chk._resolve_repo_roots(repo), [repo])


if __name__ == "__main__":
    unittest.main()
