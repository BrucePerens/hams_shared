#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_noupdate_edits.py, against throwaway git repositories.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

import check_noupdate_edits as chk  # noqa: E402


def _wrap(body, outer="1"):
    return f'<odoo>\n<data noupdate="{outer}">\n{body}\n</data>\n</odoo>\n'


PLAIN = '<odoo>\n{}\n</odoo>\n'


class NoupdateEditTests(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.test")
        self.git("config", "user.name", "t")

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def git(self, *args):
        subprocess.run(["git", "-C", self.repo, *args], check=True, capture_output=True)

    def write(self, path, text):
        full = os.path.join(self.repo, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(text)

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def warnings(self):
        return [(path, record_id) for path, record_id, _m in chk.check_repo(self.repo, "base", "HEAD")]

    def start(self, path, text):
        self.write(path, text)
        self.commit("base")
        self.git("tag", "base")

    RECORD = '<record id="r1" model="ir.rule"><field name="name">{}</field></record>'
    TEMPLATE = '<template id="t1"><p>{}</p></template>'

    def test_edited_record_in_a_noupdate_block_is_reported(self):
        self.start("mod/data/a.xml", _wrap(self.RECORD.format("old")))
        self.write("mod/data/a.xml", _wrap(self.RECORD.format("new")))
        self.commit("edit")
        self.assertEqual(self.warnings(), [("mod/data/a.xml", "r1")])

    def test_edited_template_in_a_noupdate_block_is_reported(self):
        self.start("mod/views/a.xml", _wrap(self.TEMPLATE.format("old")))
        self.write("mod/views/a.xml", _wrap(self.TEMPLATE.format("new")))
        self.commit("edit")
        self.assertEqual(self.warnings(), [("mod/views/a.xml", "t1")])

    def test_odoo_root_noupdate_attribute_counts(self):
        self.start("mod/data/a.xml", f'<odoo noupdate="1">{self.RECORD.format("old")}</odoo>')
        self.write("mod/data/a.xml", f'<odoo noupdate="1">{self.RECORD.format("new")}</odoo>')
        self.commit("edit")
        self.assertEqual(self.warnings(), [("mod/data/a.xml", "r1")])

    def test_nested_noupdate_zero_resets(self):
        inner = lambda text: f'<data noupdate="0">{self.RECORD.format(text)}</data>'
        self.start("mod/data/a.xml", _wrap(inner("old")))
        self.write("mod/data/a.xml", _wrap(inner("new")))
        self.commit("edit")
        self.assertEqual(self.warnings(), [])

    def test_edit_outside_a_noupdate_block_is_fine(self):
        self.start("mod/data/a.xml", PLAIN.format(self.RECORD.format("old")))
        self.write("mod/data/a.xml", PLAIN.format(self.RECORD.format("new")))
        self.commit("edit")
        self.assertEqual(self.warnings(), [])

    def test_record_moved_out_of_noupdate_is_fine(self):
        self.start("mod/data/a.xml", _wrap(self.RECORD.format("old")))
        self.write("mod/data/a.xml", PLAIN.format(self.RECORD.format("new")))
        self.commit("edit")
        self.assertEqual(self.warnings(), [])

    def test_record_moved_into_noupdate_with_a_change_is_reported(self):
        self.start("mod/data/a.xml", PLAIN.format(self.RECORD.format("old")))
        self.write("mod/data/a.xml", _wrap(self.RECORD.format("new")))
        self.commit("edit")
        self.assertEqual(self.warnings(), [("mod/data/a.xml", "r1")])

    def test_comment_and_whitespace_only_changes_are_ignored(self):
        self.start("mod/data/a.xml", _wrap(self.RECORD.format("same")))
        self.write(
            "mod/data/a.xml",
            _wrap("<!-- a new comment -->\n   " + self.RECORD.format("same").replace("><", ">\n  <")),
        )
        self.commit("edit")
        self.assertEqual(self.warnings(), [])

    def test_attribute_order_is_ignored(self):
        self.start("mod/data/a.xml", _wrap('<record id="r1" model="ir.rule"/>'))
        self.write("mod/data/a.xml", _wrap('<record model="ir.rule" id="r1"/>'))
        self.commit("edit")
        self.assertEqual(self.warnings(), [])

    def test_added_and_removed_records_are_not_reported(self):
        self.start("mod/data/a.xml", _wrap(self.RECORD.format("old")))
        self.write(
            "mod/data/a.xml",
            _wrap('<record id="r2" model="ir.rule"><field name="name">x</field></record>'),
        )
        self.commit("edit")
        self.assertEqual(self.warnings(), [])

    def test_only_the_edited_record_of_two_is_reported(self):
        two = lambda text: _wrap(self.RECORD.format(text) + self.TEMPLATE.format("fixed"))
        self.start("mod/data/a.xml", two("old"))
        self.write("mod/data/a.xml", two("new"))
        self.commit("edit")
        self.assertEqual(self.warnings(), [("mod/data/a.xml", "r1")])

    def test_a_migration_script_in_the_same_change_covers_the_record(self):
        self.start("mod/data/a.xml", _wrap(self.RECORD.format("old")))
        self.write("mod/data/a.xml", _wrap(self.RECORD.format("new")))
        self.write("mod/migrations/1.1/post-migration.py", "def migrate(cr, version):\n    cr.execute(\"-- r1\")\n")
        self.commit("edit with migration")
        self.assertEqual(self.warnings(), [])

    def test_a_migration_of_another_module_or_record_does_not_cover_it(self):
        self.start("mod/data/a.xml", _wrap(self.RECORD.format("old")))
        self.write("mod/data/a.xml", _wrap(self.RECORD.format("new")))
        self.write("other/migrations/1.1/post-migration.py", "# r1\n")
        self.write("mod/migrations/1.1/post-migration.py", "# r10 and xr1 only\n")
        self.commit("edit")
        self.assertEqual(self.warnings(), [("mod/data/a.xml", "r1")])

    def test_demo_tests_and_malformed_files_are_skipped(self):
        self.start("mod/demo/a.xml", _wrap(self.RECORD.format("old")))
        self.write("mod/demo/a.xml", _wrap(self.RECORD.format("new")))
        self.write("mod/data/b.xml", "<odoo><data noupdate='1'><record id='r1'")
        self.commit("edit")
        self.assertEqual(self.warnings(), [])

    def test_a_renamed_file_is_followed(self):
        padding = "".join(
            f'<record id="pad{i}" model="ir.rule"><field name="name">padding {i}</field></record>\n'
            for i in range(30)
        )
        self.start("mod/data/a.xml", _wrap(padding + self.RECORD.format("old")))
        os.remove(os.path.join(self.repo, "mod/data/a.xml"))
        self.write("mod/data/b.xml", _wrap(padding + self.RECORD.format("new")))
        self.commit("rename and edit")
        self.assertEqual(self.warnings(), [("mod/data/b.xml", "r1")])

    def test_main_prints_and_exits_zero_unless_strict(self):
        self.start("mod/data/a.xml", _wrap(self.RECORD.format("old")))
        self.write("mod/data/a.xml", _wrap(self.RECORD.format("new")))
        self.commit("edit")
        args = [self.repo, "--base", "base", "--head", "HEAD"]
        self.assertEqual(chk.main(args), 0)
        self.assertEqual(chk.main(args + ["--strict"]), 1)

    def test_main_with_no_findings_is_quiet_and_zero_even_when_strict(self):
        self.start("mod/data/a.xml", PLAIN.format(self.RECORD.format("old")))
        self.write("mod/data/a.xml", PLAIN.format(self.RECORD.format("new")))
        self.commit("edit")
        self.assertEqual(chk.main([self.repo, "--base", "base", "--strict"]), 0)


if __name__ == "__main__":
    unittest.main()
