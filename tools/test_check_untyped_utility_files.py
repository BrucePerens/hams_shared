#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_untyped_utility_files.py (ODOO_AWARE_TYPE_CHECKING.md Phase 1).

collect_candidates() only ever scans the hardcoded SCAN_ROOTS (a fixed list
of repo-relative paths this repo actually has), so temp-directory fixtures
here mirror those exact names (e.g. "daemons/...", "ingest/...") rather than
arbitrary directory names, the same way the script itself only ever sees
real hams_com/hams_open paths.
"""

import ast
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_untyped_utility_files as chk

_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "check_untyped_utility_files.py")


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _class_node(src):
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            return node
    raise AssertionError("no class in fixture")


class IsModelClassTests(unittest.TestCase):
    def test_recognizes_models_dot_model(self):
        self.assertTrue(chk._is_model_class(_class_node("class Foo(models.Model):\n    pass\n")))

    def test_recognizes_a_bare_model_import_form(self):
        self.assertTrue(chk._is_model_class(_class_node("class Foo(Model):\n    pass\n")))

    def test_recognizes_abstract_and_transient_model(self):
        self.assertTrue(chk._is_model_class(_class_node("class Foo(models.AbstractModel):\n    pass\n")))
        self.assertTrue(chk._is_model_class(_class_node("class Foo(models.TransientModel):\n    pass\n")))

    def test_a_plain_class_is_not_a_model_class(self):
        self.assertFalse(chk._is_model_class(_class_node("class Foo:\n    pass\n")))


class DefinesOdooModelClassTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _path(self, content):
        p = os.path.join(self.tmp, "f.py")
        _write(p, content)
        return p

    def test_true_for_a_file_that_defines_a_model_class(self):
        p = self._path("class Foo(models.Model):\n    _name = 'x'\n")
        self.assertTrue(chk.defines_odoo_model_class(p))

    def test_false_for_a_plain_utility_file(self):
        p = self._path("def add(a, b):\n    return a + b\n")
        self.assertFalse(chk.defines_odoo_model_class(p))

    def test_false_for_a_runtime_isinstance_check_with_no_class_def(self):
        # The real, documented false-negative this script's own docstring
        # fixed: distributed_redis_cache/redis_cache.py referenced
        # `models.Model` only in an isinstance() check, defined no class,
        # and an earlier textual "imports models from odoo" version of
        # this check wrongly exempted it. The real AST-based check must
        # NOT exempt this file (no class defined here at all -- False is
        # actually correct: it should be SCANNED, i.e. not treated as an
        # Odoo model file to skip).
        p = self._path(
            "def notify_model_invalidation(obj):\n"
            "    if isinstance(obj, models.Model):\n"
            "        pass\n"
        )
        self.assertFalse(chk.defines_odoo_model_class(p))

    def test_false_and_no_crash_for_a_syntax_broken_file(self):
        p = self._path("class Foo(models.Model: broken")
        self.assertFalse(chk.defines_odoo_model_class(p))

    def test_false_for_a_nonexistent_file(self):
        self.assertFalse(chk.defines_odoo_model_class(os.path.join(self.tmp, "does_not_exist.py")))


class CollectCandidatesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_single_file_scan_root_is_collected_when_present(self):
        _write(
            os.path.join(self.tmp, "distributed_redis_cache", "redis_cache.py"),
            "def f():\n    pass\n",
        )
        candidates = chk.collect_candidates(self.tmp)
        self.assertIn(os.path.join(self.tmp, "distributed_redis_cache", "redis_cache.py"), candidates)

    def test_a_missing_scan_root_is_skipped_without_crashing(self):
        # None of SCAN_ROOTS exist under this empty temp dir at all.
        self.assertEqual(chk.collect_candidates(self.tmp), [])

    def test_a_directory_scan_root_is_walked_recursively(self):
        _write(os.path.join(self.tmp, "daemons", "a", "b.py"), "def f():\n    pass\n")
        candidates = chk.collect_candidates(self.tmp)
        self.assertIn(os.path.join(self.tmp, "daemons", "a", "b.py"), candidates)

    def test_a_file_defining_an_odoo_model_class_is_excluded(self):
        _write(
            os.path.join(self.tmp, "daemons", "models_file.py"),
            "class Foo(models.Model):\n    _name = 'x'\n",
        )
        candidates = chk.collect_candidates(self.tmp)
        self.assertEqual(candidates, [])

    def test_a_file_under_an_excluded_dir_prefix_is_never_collected(self):
        _write(
            os.path.join(self.tmp, "daemons", "hams_local_relay", "radae", "vendored.py"),
            "def f():\n    pass\n",
        )
        candidates = chk.collect_candidates(self.tmp)
        self.assertEqual(candidates, [])

    def test_a_file_under_an_ignored_dir_name_is_never_collected(self):
        _write(os.path.join(self.tmp, "daemons", "node_modules", "x.py"), "def f():\n    pass\n")
        candidates = chk.collect_candidates(self.tmp)
        self.assertEqual(candidates, [])

    def test_a_non_py_file_is_never_collected(self):
        _write(os.path.join(self.tmp, "daemons", "readme.txt"), "not python")
        candidates = chk.collect_candidates(self.tmp)
        self.assertEqual(candidates, [])

    def test_an_explicitly_excluded_file_is_skipped(self):
        _write(os.path.join(self.tmp, "daemons", "known_bad.py"), "def f():\n    pass\n")
        original = chk.EXCLUDED_FILES
        chk.EXCLUDED_FILES = {os.path.join("daemons", "known_bad.py")}
        try:
            candidates = chk.collect_candidates(self.tmp)
        finally:
            chk.EXCLUDED_FILES = original
        self.assertEqual(candidates, [])

    def test_candidates_are_deduplicated_and_sorted(self):
        _write(os.path.join(self.tmp, "daemons", "b.py"), "def f():\n    pass\n")
        _write(os.path.join(self.tmp, "daemons", "a.py"), "def f():\n    pass\n")
        candidates = chk.collect_candidates(self.tmp)
        self.assertEqual(candidates, sorted(set(candidates)))


class MainIntegrationTests(unittest.TestCase):
    """Real subprocess runs against real mypy -- this environment has mypy
    installed, and main()'s actual value is entirely in that wiring."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self):
        result = subprocess.run(
            [sys.executable, _SCRIPT, self.tmp], capture_output=True, text=True, timeout=60
        )
        return result.returncode, result.stdout + result.stderr

    def test_a_clean_utility_file_passes(self):
        _write(
            os.path.join(self.tmp, "daemons", "clean.py"),
            "def add(a: int, b: int) -> int:\n    return a + b\n",
        )
        code, out = self._run()
        self.assertEqual(code, 0, out)

    def test_a_real_wrong_arg_count_call_is_caught(self):
        # Mirrors the proposal doc's own motivating bug class.
        _write(
            os.path.join(self.tmp, "daemons", "bug.py"),
            "def notify(a, b):\n"
            "    pass\n\n"
            "def caller():\n"
            "    notify(1, 2, 3)\n",
        )
        code, out = self._run()
        self.assertEqual(code, 1)
        self.assertIn("mypy findings", out)

    def test_a_file_defining_an_odoo_model_class_is_never_scanned_even_with_a_real_bug(self):
        _write(
            os.path.join(self.tmp, "daemons", "model_with_bug.py"),
            "class Foo(models.Model):\n"
            "    _name = 'x'\n\n"
            "    def notify(self, a, b):\n"
            "        pass\n\n"
            "    def caller(self):\n"
            "        self.notify(1, 2, 3)\n",
        )
        code, out = self._run()
        self.assertEqual(code, 0, out)

    def test_no_candidates_at_all_passes_quietly(self):
        code, out = self._run()
        self.assertEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main()
