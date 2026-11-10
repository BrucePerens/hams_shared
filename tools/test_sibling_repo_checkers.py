#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every checker that scans both repositories takes the OTHER repository by identity (lint_exec.sibling_repo_root),
not by directory name.

Tests [@ANCHOR: lint_exec:has_odoo_module] and [@ANCHOR: lint_exec:has_agents_md]. 2026-11-05/06: from a hams_open
worktree at ~/workspace/tmp/<topic>, `../hams_open` is a symlink to the main hams_open checkout. About twenty
checkers took it as the "sibling", scanned main hams_open beside the worktree and never scanned hams_com.
"""

import importlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lint_exec  # noqa: E402

# (module, kind): "roots" = _resolve_repo_roots(path) -> [root, sibling]; "find" = _find_sibling_repo(root) -> sibling.
CHECKERS = [
    ("check_dead_code", "roots"),
    ("check_registry_test_cr_usage", "roots"),
    ("check_xml_attribute_position_comment", "roots"),
    ("check_xml_comment_double_hyphen", "roots"),
    ("check_external_library_locality", "roots"),
    ("scan_ui_vulnerabilities", "roots"),
    ("check_self_writeable_field_tests", "roots"),
    ("check_pip_audit", "roots"),
    ("check_gdpr_erasure_uses_service_utility", "roots"),
    ("check_untyped_utility_files", "roots"),
    ("check_doc_images", "roots"),
    ("check_access_csv_group_order", "find"),
    ("check_dependency_cycles", "find"),
    ("check_test_depends", "find"),
    ("check_res_config_settings_access", "find"),
    ("check_module_subpackage_imports", "find"),
    ("check_minified_js_nested_templates", "find"),
    ("check_minified_js_syntax_errors", "find"),
    ("check_model_extension_collisions", "find"),
    ("odoo_registry_builder", "find"),
]


def _git(cwd, *args):
    subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True, text=True)


class SiblingByIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ws = os.path.realpath(os.path.join(self.tmp, "workspace"))
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for name in ("HAMS_COM_TREE", "HAMS_OPEN_TREE"):
            os.environ.pop(name, None)
        self.open_main = self._repo("hams_open", "zero_sudo")
        self.com_main = self._repo("hams_com", "ham_base")
        os.makedirs(os.path.join(self.ws, "tmp"))
        self.open_wt = os.path.join(self.ws, "tmp", "wt-topic")
        _git(self.open_main, "worktree", "add", "-q", "-b", "topic", self.open_wt)
        os.symlink(self.open_main, os.path.join(self.ws, "tmp", "hams_open"))

    def _repo(self, name, module):
        repo = os.path.join(self.ws, name)
        os.makedirs(os.path.join(repo, module))
        for path in (os.path.join(repo, module, "__manifest__.py"), os.path.join(repo, "AGENTS.md")):
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{}\n")
        _git(repo, "init", "-q")
        _git(repo, "remote", "add", "origin", f"https://github.com/BrucePerens/{name}.git")
        _git(repo, "add", module, "AGENTS.md")
        _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "init")
        return repo

    def _sibling(self, module, kind, path):
        try:
            mod = importlib.import_module(module)
        except ImportError as error:  # a checker needing a library this interpreter lacks (odoo, lxml)
            self.skipTest(f"{module} cannot be imported here: {error}")
        if kind == "roots":
            roots = mod._resolve_repo_roots(path)
            return roots[1] if len(roots) > 1 else None
        return mod._find_sibling_repo(path)

    def test_a_worktree_beside_a_symlink_to_its_own_main_checkout_gets_the_other_repository(self):
        for module, kind in CHECKERS:
            with self.subTest(module=module):
                self.assertEqual(self._sibling(module, kind, self.open_wt), self.com_main)

    def test_the_main_checkouts_still_pair_up(self):
        for module, kind in CHECKERS:
            with self.subTest(module=module):
                self.assertEqual(self._sibling(module, kind, self.open_main), self.com_main)
                self.assertEqual(self._sibling(module, kind, self.com_main), self.open_main)

    def test_without_the_other_repository_there_is_no_sibling(self):
        shutil.rmtree(self.com_main)
        for module, kind in CHECKERS:
            with self.subTest(module=module):
                self.assertIsNone(self._sibling(module, kind, self.open_wt))

    def test_the_accept_tests_look_for_a_module_and_for_agents_md(self):
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        self.assertFalse(lint_exec.has_odoo_module(empty))
        self.assertFalse(lint_exec.has_odoo_module(os.path.join(self.tmp, "missing")))
        self.assertTrue(lint_exec.has_odoo_module(self.open_main))
        self.assertFalse(lint_exec.has_agents_md(empty))
        self.assertTrue(lint_exec.has_agents_md(self.com_main))


if __name__ == "__main__":
    unittest.main()
