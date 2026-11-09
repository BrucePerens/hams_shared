# SPDX-License-Identifier: AGPL-3.0-or-later

"""check_burn_list (the main scan and the tour-class scan), check_cargo_clippy's crate finder and
check_external_programs_documented's relay source finder take their files from `tree_files`: a worktree of another
checkout nested under the one being linted is not scanned, and a new file that is not `git add`ed yet is. See
`tree_files_testkit.check_nested_worktree_table`."""

import os
import shutil
import tempfile
import unittest

import check_cargo_clippy
import check_external_programs_documented as ext
import tree_files_testkit as kit

CHECKERS = {
    "check_burn_list.py": (
        {"m/__manifest__.py": "{}\n", "m/models/a.py": "x = 1\n", "m/views/v.xml": "<odoo/>\n", "m/static/src/t.js": "x\n"},
        "m/models/a.py",
    ),
}


class NestedForeignWorktreeTests(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_burn_list_scans_this_tree_only(self):
        kit.check_nested_worktree_table(self, CHECKERS)

    def test_burn_list_tour_scan_reads_only_this_trees_xml_and_js(self):
        kit.checkout_files_with_nested_worktree(
            self.root, {"m/__manifest__.py": "{}\n", "m/views/v.xml": '<odoo class="o_tour_x"/>\n', "m/static/src/t.js": "x\n"}
        )
        opened, out = kit.run_checker("check_burn_list.py", self.root, os.path.join(self.root, "cache"))
        seen = "\n".join(opened) + out
        self.assertIn(os.path.join("m", "views", "v.xml"), seen)
        self.assertNotIn(os.path.join("tmp", "other"), seen)

    def test_cargo_crates_of_this_tree_include_a_new_one_and_skip_a_nested_worktree(self):
        kit.checkout_files_with_nested_worktree(self.root, {"c/Cargo.toml": "[package]\nname='c'\n"})
        kit.write(self.root, "n/Cargo.toml", "[package]\nname='n'\n")
        got = [os.path.relpath(p, self.root) for p in check_cargo_clippy.find_cargo_crates(self.root)]
        self.assertEqual(got, ["c", "n"])

    def test_relay_rust_sources_of_this_tree_include_a_new_file_and_skip_a_nested_worktree(self):
        kit.checkout_files_with_nested_worktree(self.root, {"relay/src/a.rs": "fn a() {}\n", "relay/src/tests/t.rs": "\n"})
        kit.write(self.root, "relay/src/new.rs", "fn n() {}\n")
        got = sorted(rel for _path, rel in ext._relay_rust_files(os.path.join(self.root, "relay")))
        self.assertEqual(got, [os.path.join("src", "a.rs"), os.path.join("src", "new.rs")])


if __name__ == "__main__":
    unittest.main()
