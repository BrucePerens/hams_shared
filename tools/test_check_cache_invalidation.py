#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_cache_invalidation.py.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_cache_invalidation as chk  # noqa: E402

_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "check_cache_invalidation.py")


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


class CheckFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _path(self, content):
        p = os.path.join(self.tmp, "models.py")
        _write(p, content)
        return p

    def test_a_mutating_raw_sql_with_no_invalidation_call_is_flagged(self):
        p = self._path(
            "def foo(self):\n"
            "    self.env.cr.execute('UPDATE ham_qso SET x = 1')\n"
        )
        errors = chk.check_file(p)
        self.assertEqual(len(errors), 1)
        self.assertIn("foo", errors[0])

    def test_a_mutating_raw_sql_with_notify_model_invalidation_passes(self):
        p = self._path(
            "def foo(self):\n"
            "    self.env.cr.execute('UPDATE ham_qso SET x = 1')\n"
            "    notify_model_invalidation('ham.qso')\n"
        )
        self.assertEqual(chk.check_file(p), [])

    def test_a_mutating_raw_sql_with_invalidate_model_cache_passes(self):
        p = self._path(
            "def foo(self):\n"
            "    self.env.cr.execute('DELETE FROM ham_qso WHERE id = 1')\n"
            "    invalidate_model_cache('ham.qso')\n"
        )
        self.assertEqual(chk.check_file(p), [])

    def test_a_select_query_is_not_treated_as_mutating(self):
        p = self._path(
            "def foo(self):\n"
            "    self.env.cr.execute('SELECT id FROM ham_qso')\n"
        )
        self.assertEqual(chk.check_file(p), [])

    def test_insert_update_delete_are_all_recognized_as_mutating(self):
        for verb in ("INSERT INTO x VALUES (1)", "UPDATE x SET y = 1", "DELETE FROM x"):
            with self.subTest(verb=verb):
                p = self._path(f"def foo(self):\n    self.env.cr.execute({verb!r})\n")
                self.assertEqual(len(chk.check_file(p)), 1)

    def test_an_upsert_stored_procedure_call_is_caught_by_the_heuristic(self):
        p = self._path(
            "def foo(self):\n"
            "    self.env.cr.execute('CALL ham_qso_upsert_batch(%s)', [data])\n"
        )
        self.assertEqual(len(chk.check_file(p)), 1)

    def test_a_create_or_replace_function_ddl_naming_upsert_is_not_flagged(self):
        # The exact documented false positive this script's own comment
        # describes: a CREATE FUNCTION definition containing "upsert" in
        # its own name trips the substring heuristic even though defining
        # the function mutates nothing -- only a later CALL/SELECT of it
        # would. Verified as the real, current, deliberate exclusion.
        p = self._path(
            "def init(self):\n"
            "    self.env.cr.execute('''\n"
            "        CREATE OR REPLACE FUNCTION ham_qso_upsert_batch(data jsonb)\n"
            "        RETURNS void AS $$ BEGIN END; $$ LANGUAGE plpgsql;\n"
            "    ''')\n"
        )
        self.assertEqual(chk.check_file(p), [])

    def test_a_plain_create_function_ddl_is_also_exempted(self):
        p = self._path(
            "def init(self):\n"
            "    self.env.cr.execute('CREATE FUNCTION foo_upsert() RETURNS void AS $$ $$ LANGUAGE sql')\n"
        )
        self.assertEqual(chk.check_file(p), [])

    def test_execute_called_on_something_other_than_env_cr_is_ignored(self):
        p = self._path(
            "def foo(self):\n"
            "    some_other_object.execute('UPDATE x SET y = 1')\n"
        )
        self.assertEqual(chk.check_file(p), [])

    def test_a_non_constant_query_argument_cannot_be_classified_and_is_not_flagged(self):
        # Documents real, verified current behavior (a known heuristic
        # limitation, not something this test claims is correct): the
        # mutating-query check only inspects a literal string constant
        # argument. A dynamically built query (an f-string, a variable, a
        # %-formatted string) can't be classified, so mutating_query stays
        # False and nothing is flagged even if the query does mutate data.
        p = self._path(
            "def foo(self, query):\n"
            "    self.env.cr.execute(query)\n"
        )
        self.assertEqual(chk.check_file(p), [])

    def test_a_read_only_function_with_no_sql_at_all_passes(self):
        p = self._path("def foo(self):\n    return self.name\n")
        self.assertEqual(chk.check_file(p), [])

    def test_an_invalidation_call_hidden_inside_an_unused_nested_helper_is_not_counted(self):
        # Real bug found 2026-09-10: `ast.walk(node)` recurses into EVERY
        # descendant, including a nested `def`'s own body -- so a
        # notify_model_invalidation() call that only lives inside a nested
        # helper function (defined but never actually invoked from `bar`'s
        # own control flow) was wrongly counted as if it ran on `bar`'s own
        # path, silently clearing `has_invalidation` to True. `bar` itself
        # never actually invalidates anything after its mutating UPDATE.
        p = self._path(
            "def bar(self):\n"
            "    self.env.cr.execute('UPDATE foo SET x = 1')\n"
            "\n"
            "    def _unused_helper():\n"
            "        notify_model_invalidation(self.env, 'foo')\n"
        )
        errors = chk.check_file(p)
        self.assertEqual(len(errors), 1)
        self.assertIn("bar", errors[0])

    def test_a_nested_helper_invoked_from_the_outer_function_is_still_flagged(self):
        # Documents the fix's own scope boundary, not a case the fix resolves: a nested helper
        # that IS actually called from the outer function's own body is a legitimate, if
        # unusual, way to structure the real invalidation call, but the outer function's own
        # scope only sees a call to `_do_invalidate` (an ast.Name the checker doesn't recognize
        # as an invalidation call) -- it does NOT trace into the helper's own body to see the
        # real notify_model_invalidation() call nested inside it. So `bar` is still flagged here,
        # a known, accepted limitation this fix does not attempt to close (tracing through an
        # arbitrary call graph is out of scope for this checker) -- this test exists so a future
        # change doesn't silently "fix" this into different, unverified behavior without a
        # deliberate decision to do so.
        p = self._path(
            "def bar(self):\n"
            "    self.env.cr.execute('UPDATE foo SET x = 1')\n"
            "\n"
            "    def _do_invalidate():\n"
            "        notify_model_invalidation(self.env, 'foo')\n"
            "    _do_invalidate()\n"
        )
        # `bar` itself still shows no direct invalidation call on its own
        # scope (only a call to the nested helper, which is an Attribute-
        # free ast.Name call to `_do_invalidate`, not to
        # notify_model_invalidation) -- documenting the real, current
        # limitation (an indirection the checker still can't see through)
        # rather than asserting a fix this change does not attempt.
        errors = chk.check_file(p)
        self.assertEqual(len(errors), 1)
        self.assertIn("bar", errors[0])

    def test_an_async_def_with_mutating_sql_and_no_invalidation_is_flagged(self):
        # Real bug found 2026-09-10, same review pass: the outer scan only
        # matched `ast.FunctionDef`, silently skipping every `async def`
        # entirely regardless of what it does -- an async model method
        # (or daemon-adjacent helper under a `models/` directory) doing a
        # raw mutating execute with no invalidation call was never checked
        # at all.
        p = self._path(
            "async def foo(self):\n"
            "    self.env.cr.execute('UPDATE ham_qso SET x = 1')\n"
        )
        errors = chk.check_file(p)
        self.assertEqual(len(errors), 1)
        self.assertIn("foo", errors[0])

    def test_each_function_is_evaluated_independently(self):
        p = self._path(
            "def good(self):\n"
            "    self.env.cr.execute('UPDATE x SET y = 1')\n"
            "    notify_model_invalidation('x')\n\n"
            "def bad(self):\n"
            "    self.env.cr.execute('UPDATE x SET y = 1')\n"
        )
        errors = chk.check_file(p)
        self.assertEqual(len(errors), 1)
        self.assertIn("bad", errors[0])


class MainIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *dirs):
        result = subprocess.run(
            [sys.executable, _SCRIPT, *dirs], capture_output=True, text=True, timeout=30
        )
        return result.returncode, result.stdout + result.stderr

    def test_a_clean_models_file_passes(self):
        _write(
            os.path.join(self.tmp, "mod_a", "models", "foo.py"),
            "def foo(self):\n    return self.name\n",
        )
        code, out = self._run(self.tmp)
        self.assertEqual(code, 0, out)
        self.assertIn("passed successfully", out)

    def test_a_violation_in_a_models_directory_fails(self):
        _write(
            os.path.join(self.tmp, "mod_a", "models", "foo.py"),
            "def foo(self):\n    self.env.cr.execute('UPDATE x SET y = 1')\n",
        )
        code, out = self._run(self.tmp)
        self.assertEqual(code, 1)
        self.assertIn("CRITICAL", out)

    def test_the_same_violation_outside_a_models_directory_is_not_scanned(self):
        _write(
            os.path.join(self.tmp, "mod_a", "tools", "foo.py"),
            "def foo(self):\n    self.env.cr.execute('UPDATE x SET y = 1')\n",
        )
        code, out = self._run(self.tmp)
        self.assertEqual(code, 0, out)

    def test_a_syntax_broken_file_is_skipped_without_crashing_the_run(self):
        _write(os.path.join(self.tmp, "mod_a", "models", "broken.py"), "def foo(: broken")
        code, out = self._run(self.tmp)
        self.assertEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main()
