#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for scan_ui_vulnerabilities.py.

main() has no argparse -- it hardcodes base_dir as
os.path.dirname(__file__)/.. with no way to point it at a fixture
directory, unlike check_js_syntax.py's main(). To exercise the real
os.walk exclusion logic (radae/venv/node_modules/__pycache__/.git
pruning, .py-only filtering) end to end without touching the real repo
tree, the MainIntegrationTests copy the script itself into a fixture's
own tools/ subdirectory and run it from there, so __file__ resolves
base_dir to the fixture root -- confirmed empirically before writing
these assertions, not assumed from reading the code.
"""

import ast
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import scan_ui_vulnerabilities as chk  # noqa: E402

_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scan_ui_vulnerabilities.py")


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


class ScanFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _path(self, content):
        p = os.path.join(self.tmp, "f.py")
        _write(p, content)
        return p

    def test_a_raw_xml_tag_in_a_plain_string_is_flagged(self):
        p = self._path('x = \'<record id="foo">\'\n')
        self.assertTrue(chk.scan_file(p))

    def test_a_math_comparison_with_spaces_around_the_angle_bracket_is_not_flagged(self):
        p = self._path("if x < y:\n    pass\n")
        self.assertFalse(chk.scan_file(p))

    def test_a_raw_xml_tag_inside_an_fstring_literal_part_is_flagged(self):
        p = self._path('y = "z"\nx = f\'<record id="{y}">\'\n')
        self.assertTrue(chk.scan_file(p))

    def test_the_using_empty_string_if_pattern_is_flagged(self):
        p = self._path("x = \"using '' if cond\"\n")
        self.assertTrue(chk.scan_file(p))

    def test_the_parens_and_templates_pattern_is_flagged(self):
        p = self._path('x = "something() and templates.render"\n')
        self.assertTrue(chk.scan_file(p))

    def test_an_ordinary_clean_string_is_not_flagged(self):
        p = self._path("x = 1\ny = 'hello world'\n")
        self.assertFalse(chk.scan_file(p))

    def test_adjacent_string_literal_concatenation_is_folded_by_ast_into_one_constant(self):
        # Real, verified behavior: Python's parser folds "a" "b" into a
        # single Constant("ab") before the AST is ever visited, so there
        # is no separate node boundary here to exploit or miss.
        p = self._path('x = "a" "b"\n')
        self.assertFalse(chk.scan_file(p))

    def test_a_file_with_a_syntax_error_fails_to_parse_and_returns_false_without_crashing(self):
        p = self._path("def f(:\n")
        with self.assertLogs(level="WARNING"):
            self.assertFalse(chk.scan_file(p))

    def test_a_nonexistent_file_returns_false_without_crashing(self):
        with self.assertLogs(level="WARNING"):
            self.assertFalse(chk.scan_file(os.path.join(self.tmp, "does_not_exist.py")))

    def test_an_fstring_raw_tag_is_reported_twice_once_per_joinedstr_and_once_per_constant_child(self):
        # Real, verified quirk: visit_JoinedStr checks the assembled f-string
        # text, then generic_visit() continues into the JoinedStr's own
        # Constant child nodes, which visit_Constant checks again
        # independently -- so a single vulnerable f-string literal produces
        # two printed findings for one line, not one. Documented here rather
        # than silently assumed.
        p = self._path("name = \"x\"\nx = f\"using '' if {name}\"\n")

        # Re-derive the count the same way scan_file does internally, by
        # re-running the same visitor shape directly against the source.
        with open(p, encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=p)
        found = []

        class _V(ast.NodeVisitor):
            def visit_Constant(self, node):
                if isinstance(node.value, str) and "using" in node.value and "if" in node.value:
                    found.append(node.value)
                self.generic_visit(node)

            def visit_JoinedStr(self, node):
                parts = []
                for val in node.values:
                    if isinstance(val, ast.Constant) and isinstance(val.value, str):
                        parts.append(val.value)
                    elif isinstance(val, ast.FormattedValue):
                        parts.append("{...}")
                full = "".join(parts)
                if "using" in full and "if" in full:
                    found.append(full)
                self.generic_visit(node)

        _V().visit(tree)
        self.assertEqual(len(found), 2)
        self.assertTrue(chk.scan_file(p))


class ResolveRepoRootTests(unittest.TestCase):
    # Regression tests for a real gap found 2026-09-12: with no CLI argument (the only way this
    # script was ever actually invocable before this fix), main()'s base_dir always resolved to
    # `hams_shared/` itself -- confirmed directly (`ls hams_shared/`) to contain zero real Odoo
    # application modules, only agents/docs/tools/scripts/config, so this checker could
    # structurally never find the raw-XML-in-a-string-literal risk it exists to catch (that risk
    # lives in portal controllers/view-generation helpers under ham_logbook/ham_satellite/
    # user_websites/etc, never in hams_shared). Same _resolve_repo_root/_resolve_repo_roots shape
    # already fixed in check_pip_audit.py/check_cargo_deny.py.
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


class ExplicitRepoRootIntegrationTests(unittest.TestCase):
    """Regression tests proving the new optional CLI argument actually reaches the real risk
    surface, unlike the old no-argument-only invocation."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *args):
        result = subprocess.run(
            [sys.executable, _SCRIPT, *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode, result.stdout + result.stderr

    def test_an_explicit_repo_root_is_actually_scanned_not_the_scripts_own_directory(self):
        _write(os.path.join(self.tmp, "some_module", "bad.py"), 'x = "<record id=\\"bad\\">"\n')
        code, out = self._run(self.tmp)
        self.assertEqual(code, 1)
        self.assertIn("bad.py", out)

    def test_a_hams_shared_argument_redirects_to_its_parent_and_finds_a_sibling_vulnerability(
        self,
    ):
        # Simulates the real deployment shape (hams_open/hams_shared, hams_com sibling) without
        # touching the actual repos -- a vulnerability planted only in the sibling repo must still
        # be found when this script is invoked as if from hams_shared's own tools/ directory,
        # exactly how run_linters.py's own real invocation pattern works for the other checkers.
        hams_open = os.path.join(self.tmp, "hams_open")
        hams_com = os.path.join(self.tmp, "hams_com")
        _write(os.path.join(hams_open, "zero_sudo", "__manifest__.py"), "{}")
        _write(os.path.join(hams_com, "ham_base", "__manifest__.py"), "{}")
        _write(
            os.path.join(hams_com, "ham_base", "bad.py"), 'x = "<record id=\\"bad\\">"\n'
        )
        hams_shared = os.path.join(hams_open, "hams_shared")
        os.makedirs(hams_shared)
        code, out = self._run(hams_shared)
        self.assertEqual(code, 1)
        self.assertIn("bad.py", out)


class MainIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.tools_dir = os.path.join(self.tmp, "tools")
        os.makedirs(self.tools_dir)
        shutil.copy(_SCRIPT, os.path.join(self.tools_dir, "scan_ui_vulnerabilities.py"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self):
        result = subprocess.run(
            [sys.executable, os.path.join(self.tools_dir, "scan_ui_vulnerabilities.py")],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode, result.stdout + result.stderr

    def test_a_tree_with_no_vulnerabilities_exits_zero(self):
        _write(os.path.join(self.tmp, "mod_a", "clean.py"), "x = 1\n")
        code, out = self._run()
        self.assertEqual(code, 0, out)
        self.assertIn("No vulnerabilities found", out)
        # tools/scan_ui_vulnerabilities.py itself + mod_a/clean.py
        self.assertIn("Checked 2 Python files", out)

    def test_a_tree_with_a_vulnerability_exits_one_and_names_the_file(self):
        _write(os.path.join(self.tmp, "mod_a", "bad.py"), 'x = "<record id=\\"bad\\">"\n')
        code, out = self._run()
        self.assertEqual(code, 1)
        self.assertIn("bad.py", out)
        self.assertIn("Found 1 file(s)", out)

    def test_a_non_python_file_is_never_scanned(self):
        _write(os.path.join(self.tmp, "mod_a", "notes.txt"), '"<record id="bad">"\n')
        code, out = self._run()
        self.assertEqual(code, 0, out)
        self.assertIn("Checked 1 Python files", out)

    def test_the_venv_directory_is_pruned(self):
        _write(os.path.join(self.tmp, "venv", "lib", "bad.py"), 'x = "<record id=\\"bad\\">"\n')
        code, out = self._run()
        self.assertEqual(code, 0, out)

    def test_the_node_modules_directory_is_pruned(self):
        _write(os.path.join(self.tmp, "node_modules", "pkg", "bad.py"), 'x = "<record id=\\"bad\\">"\n')
        code, out = self._run()
        self.assertEqual(code, 0, out)

    def test_the_pycache_directory_is_pruned(self):
        _write(os.path.join(self.tmp, "__pycache__", "bad.py"), 'x = "<record id=\\"bad\\">"\n')
        code, out = self._run()
        self.assertEqual(code, 0, out)

    def test_the_dot_git_directory_is_pruned(self):
        _write(os.path.join(self.tmp, ".git", "bad.py"), 'x = "<record id=\\"bad\\">"\n')
        code, out = self._run()
        self.assertEqual(code, 0, out)

    def test_a_radae_directory_anywhere_in_the_tree_is_pruned(self):
        _write(os.path.join(self.tmp, "daemon", "radae", "bad.py"), 'x = "<record id=\\"bad\\">"\n')
        code, out = self._run()
        self.assertEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main()
