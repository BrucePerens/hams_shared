#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for check_migration_registry_deletes.py, against temp-directory migration fixtures."""

import os
import shutil
import tempfile
import textwrap
import unittest

import check_migration_registry_deletes as chk  # noqa: E402


class CheckFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def findings(self, body):
        path = os.path.join(self.tmp, "post-migration.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(textwrap.dedent(body))
        return chk.check_file(path)

    def test_the_ham_base_0_13_shape_is_flagged(self):
        # Model absent from the registry -> delete its chatter: the 2026-11-21 incident.
        found = self.findings(
            """
            def migrate(cr, version):
                env = api.Environment(cr, 1, {})
                for model in ("ham.chat",):
                    if model not in env:
                        cr.execute("DELETE FROM mail_message WHERE model = %s", (model,))
            """
        )
        self.assertEqual(len(found), 1)

    def test_env_get_and_registry_conditions_are_flagged(self):
        for cond in ("env.get('x.y') is None", "'x.y' not in env.registry", "not env['x.y']"):
            found = self.findings(
                f"""
                def migrate(cr, version):
                    if {cond}:
                        env['mail.message'].search([]).unlink()
                """
            )
            self.assertEqual(len(found), 1, cond)

    def test_table_existence_through_a_fetch_taints_the_condition(self):
        found = self.findings(
            """
            def migrate(cr, version):
                cr.execute("SELECT to_regclass('some_table')")
                exists = cr.fetchone()[0]
                if not exists:
                    cr.execute("DELETE FROM ir_attachment WHERE res_model = 'x'")
            """
        )
        self.assertEqual(len(found), 1)

    def test_ir_model_lookup_deciding_a_delete_is_flagged(self):
        found = self.findings(
            """
            def migrate(cr, version):
                cr.execute("SELECT id FROM ir_model WHERE model = 'x.y'")
                if not cr.fetchone():
                    cr.execute("DELETE FROM x_y")
            """
        )
        self.assertEqual(len(found), 1)

    def test_helper_named_exists_is_flagged(self):
        found = self.findings(
            """
            def migrate(cr, version):
                if _table_exists(cr, "t"):
                    cr.execute("DROP TABLE t")
            """
        )
        self.assertEqual(len(found), 1)

    def test_ignore_comment_on_the_delete_or_the_condition_allows_it(self):
        for delete_marker, cond_marker in (("  # migration-registry-ok: own table", ""), ("", "  # migration-registry-ok: own table")):
            found = self.findings(
                f"""
                def migrate(cr, version):
                    if _table_exists(cr, "t"):{cond_marker}
                        cr.execute("DROP TABLE t"){delete_marker}
                """
            )
            self.assertEqual(found, [])

    def test_early_return_guard_and_plain_deletes_are_not_flagged(self):
        self.assertEqual(
            self.findings(
                """
                def migrate(cr, version):
                    cr.execute("SELECT to_regclass('t')")
                    if not cr.fetchone()[0]:
                        return
                    cr.execute("DELETE FROM t WHERE id > 5")
                    cr.execute("DELETE FROM ir_model_data WHERE module = 'm' AND name = 'x'")
                """
            ),
            [],
        )

    def test_a_non_delete_under_a_registry_condition_is_not_flagged(self):
        self.assertEqual(
            self.findings(
                """
                def migrate(cr, version):
                    if "x.y" in env:
                        cr.execute("UPDATE t SET a = 1")
                """
            ),
            [],
        )

    def test_an_unrelated_condition_is_not_flagged(self):
        self.assertEqual(
            self.findings(
                """
                def migrate(cr, version):
                    if version:
                        cr.execute("DELETE FROM t WHERE stale")
                """
            ),
            [],
        )


class FileSelectionTests(unittest.TestCase):
    def test_only_files_under_migrations_are_scanned(self):
        tmp = tempfile.mkdtemp()
        try:
            for rel in ("mod/migrations/1.0/post.py", "mod/models/x.py"):
                os.makedirs(os.path.join(tmp, os.path.dirname(rel)), exist_ok=True)
                with open(os.path.join(tmp, rel), "w", encoding="utf-8") as f:
                    f.write("X = 1\n")
            found = [os.path.relpath(p, tmp) for p in chk.find_python_files(tmp)]
            self.assertEqual(found, [os.path.join("mod", "migrations", "1.0", "post.py")])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
