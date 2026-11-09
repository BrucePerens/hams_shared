# SPDX-License-Identifier: AGPL-3.0-or-later

"""The source checkers that select their files with `tree_files` (group: Python, shell and module-layout checkers) never
scan a worktree of another checkout nested under the one being linted, and still scan the files that are the tree's own.

Each checker is run as a command on a git checkout that has a real second worktree at `tmp/other` (same files, so a raw
`os.walk` would read and report them twice). The test records every file the checker opens and everything it prints:
the tree's own file must show up, nothing under `tmp/other` may. A new file that is not yet `git add`ed must be seen too."""

import os
import shutil
import tempfile
import unittest

import check_cargo_deny
import check_pip_audit
import check_res_config_settings_access
import tree_files_testkit as kit

MODEL = "from odoo import models\n\n\nclass A(models.Model):\n    _name = 'x.y'\n"

# script -> (files the checkout tracks, the one of them the checker must see). The checker gets the checkout as its argument.
CHECKERS = {
    "check_absolute_paths.py": ({"docs/a.md": "nothing\n"}, "docs/a.md"),
    "check_shebang.py": ({"s.sh": "echo hi\n"}, "s.sh"),
    "check_init_imports.py": ({"mod/__init__.py": "", "mod/b.py": "x = 1\n"}, "mod/__init__.py"),
    "check_cache_invalidation.py": ({"m/models/a.py": "x = 1\n"}, "m/models/a.py"),
    "check_external_library_locality.py": ({"m/static/lib/x.js": "x\n"}, "m/static/lib/x.js"),
    "check_model_extension_collisions.py": ({"m/__manifest__.py": "{}\n", "m/models/a.py": MODEL}, "m/models/a.py"),
    "check_self_writeable_field_tests.py": ({"m/__manifest__.py": "{}\n", "m/models/a.py": "x = 1\n"}, "m/models/a.py"),
    "check_manifest_dependencies.py": ({"m/__manifest__.py": "{'depends': []}\n", "m/static/src/a.js": "x\n"}, "m/static/src/a.js"),
    "check_rabbitmq_pool.py": ({"a.py": "x = 1\n"}, "a.py"),
    "check_registry_test_cr_usage.py": ({"a.py": "x = 1\n"}, "a.py"),
    "check_test_tags.py": ({"m/tests/test_a.py": "x = 1\n"}, "m/tests/test_a.py"),
    "check_dependency_cycles.py": ({"m/__manifest__.py": "{'depends': []}\n"}, "m/__manifest__.py"),
    "check_gdpr_erasure_uses_service_utility.py": ({"m/a.py": "x = 1\n"}, "m/a.py"),
    "check_untyped_utility_files.py": ({"ingest/u.py": "x = 1\n"}, "ingest/u.py"),
    "check_dead_code.py": ({"m/__manifest__.py": "{}\n", "m/views/v.xml": "<odoo/>\n"}, "m/views/v.xml"),
}


class NestedForeignWorktreeTests(unittest.TestCase):
    def test_a_nested_foreign_worktree_is_not_scanned_and_a_new_file_is(self):
        kit.check_nested_worktree_table(self, CHECKERS)


class FindFunctionTests(unittest.TestCase):
    """The checkers whose command shells out to another tool (`pip_audit`, `cargo deny`) are tested at the function that lists
    what they audit."""

    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_pip_audit_lists_the_trees_requirements_files_and_not_a_nested_worktrees(self):
        kit.checkout_files_with_nested_worktree(self.root, {"d/requirements.txt": "x\n", "d/notes.txt": "x\n"})
        kit.write(self.root, "new/requirements-dev.txt", "x\n")
        got = [os.path.relpath(p, self.root) for p in check_pip_audit.find_requirements_files(self.root)]
        self.assertEqual(got, [os.path.join("d", "requirements.txt"), os.path.join("new", "requirements-dev.txt")])

    def test_cargo_deny_lists_the_trees_crates_and_not_a_nested_worktrees(self):
        kit.checkout_files_with_nested_worktree(self.root, {"c/Cargo.toml": "x\n", "c/deny.toml": "x\n", "o/Cargo.toml": "x\n"})
        kit.write(self.root, "n/Cargo.toml", "x\n")
        kit.write(self.root, "n/deny.toml", "x\n")
        got = [os.path.relpath(p, self.root) for p in check_cargo_deny.find_deny_crates(self.root)]
        self.assertEqual(got, ["c", "n"])

    def test_res_config_access_csvs_of_a_module_include_a_new_file_and_skip_a_nested_worktree(self):
        kit.checkout_files_with_nested_worktree(self.root, {"m/__manifest__.py": "{}\n", "m/security/ir.model.access.csv": "x\n"})
        kit.write(self.root, "m/other/ir.model.access.csv", "x\n")
        got = [os.path.relpath(p, self.root) for _m, p in check_res_config_settings_access._find_access_csvs([self.root])]
        self.assertEqual(got, [os.path.join("m", "other", "ir.model.access.csv"), os.path.join("m", "security", "ir.model.access.csv")])


if __name__ == "__main__":
    unittest.main()
