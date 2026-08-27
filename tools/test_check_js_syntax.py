#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_js_syntax.py.

check_file() shells out to a real `node --input-type=module --check`
subprocess -- a fast, local, CPU-only, network-free operation (node is
installed in this environment), so these tests let it run for real rather
than mocking it, the same way check_untyped_utility_files.py's tests run
real mypy.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_js_syntax as chk  # noqa: E402

_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "check_js_syntax.py")


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


class LoadIgnoreFileTests(unittest.TestCase):
    def test_no_filepath_returns_an_empty_list(self):
        self.assertEqual(chk.load_ignore_file(None), [])

    def test_a_nonexistent_path_returns_an_empty_list(self):
        self.assertEqual(chk.load_ignore_file("/does/not/exist.txt"), [])

    def test_a_real_ignore_file_is_parsed_into_compiled_patterns(self):
        tmp = tempfile.mkdtemp()
        try:
            p = os.path.join(tmp, "ignore.txt")
            _write(p, "# a comment\n\nvendor/.*\nnode_modules/.*\n")
            patterns = chk.load_ignore_file(p)
            self.assertEqual(len(patterns), 2)
            for pat in patterns:
                self.assertIsInstance(pat, re.Pattern)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class IsIgnoredTests(unittest.TestCase):
    def test_a_matching_path_is_ignored(self):
        patterns = [re.compile(r"vendor/.*")]
        self.assertTrue(chk.is_ignored("mod_a/vendor/lib.js", patterns))

    def test_a_non_matching_path_is_not_ignored(self):
        patterns = [re.compile(r"vendor/.*")]
        self.assertFalse(chk.is_ignored("mod_a/static/src/js/main.js", patterns))

    def test_no_patterns_never_ignores_anything(self):
        self.assertFalse(chk.is_ignored("anything.js", []))


class CheckFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _path(self, content):
        p = os.path.join(self.tmp, "script.js")
        _write(p, content)
        return p

    def _test_js_path(self, content):
        p = os.path.join(self.tmp, "widget.test.js")
        _write(p, content)
        return p

    def test_valid_syntax_returns_none(self):
        p = self._path("const x = 1;\nfunction f() { return x + 1; }\n")
        self.assertIsNone(chk.check_file(p))

    def test_invalid_syntax_returns_the_file_path_and_a_node_error_with_real_filename(self):
        p = self._path("const x = ;\n")
        result = chk.check_file(p)
        self.assertIsNotNone(result)
        file_path, err_msg = result
        self.assertEqual(file_path, p)
        self.assertIn("script.js", err_msg)
        self.assertNotIn("[stdin]", err_msg)

    def test_es6_import_export_syntax_is_valid_under_module_mode(self):
        # The whole reason for --input-type=module: native ES6 import/
        # export must not be reported as a syntax error.
        p = self._path("import { Component } from '@odoo/owl';\nexport class Foo {}\n")
        self.assertIsNone(chk.check_file(p))

    def test_the_interaction_mount_component_architecture_trap_is_flagged(self):
        p = self._path(
            "class Foo extends Interaction {\n"
            "    start() { this.mountComponent(Bar); }\n"
            "}\n"
        )
        result = chk.check_file(p)
        self.assertIsNotNone(result)
        _file_path, err_msg = result
        self.assertIn("ARCHITECTURE TRAP", err_msg)

    def test_an_unreadable_file_returns_none_without_crashing(self):
        with self.assertLogs(level="WARNING"):
            self.assertIsNone(chk.check_file(os.path.join(self.tmp, "does_not_exist.js")))

    def test_an_invalid_hoot_matcher_is_flagged_in_a_test_js_file(self):
        p = self._test_js_path("test('x', () => {\n    expect(foo).toBeUndefined();\n});\n")
        result = chk.check_file(p)
        self.assertIsNotNone(result)
        _file_path, err_msg = result
        self.assertIn("INVALID HOOT MATCHER", err_msg)
        self.assertIn("toBeUndefined", err_msg)
        self.assertIn("toBe(undefined)", err_msg)

    def test_a_real_hoot_matcher_is_not_flagged(self):
        p = self._test_js_path("test('x', () => {\n    expect(foo).toBe(undefined);\n});\n")
        self.assertIsNone(chk.check_file(p))

    def test_the_invalid_matcher_check_is_scoped_to_test_js_files_only(self):
        # A plain .js file (not a test file) calling a same-named method on
        # some other object entirely shouldn't be flagged -- the check only
        # applies to files that actually use hoot's expect().
        p = self._path("foo.toBeUndefined();\n")
        self.assertIsNone(chk.check_file(p))

    def test_toContain_is_flagged_with_toInclude_as_the_real_equivalent(self):
        p = self._test_js_path("test('x', () => {\n    expect(s).toContain('a');\n});\n")
        result = chk.check_file(p)
        self.assertIsNotNone(result)
        _file_path, err_msg = result
        self.assertIn("toContain", err_msg)
        self.assertIn("toInclude", err_msg)


class MainIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *extra_args):
        result = subprocess.run(
            [sys.executable, _SCRIPT, *extra_args, self.tmp],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode, result.stdout + result.stderr

    def test_all_valid_files_pass(self):
        _write(os.path.join(self.tmp, "mod_a", "static", "src", "js", "a.js"), "const x = 1;\n")
        code, out = self._run()
        self.assertEqual(code, 0, out)
        self.assertIn("All 1 files passed", out)

    def test_an_invalid_file_fails_with_the_file_path_shown(self):
        _write(os.path.join(self.tmp, "mod_a", "static", "src", "js", "bad.js"), "const x = ;\n")
        code, out = self._run()
        self.assertEqual(code, 1)
        self.assertIn("SYNTAX ERRORS DETECTED", out)
        self.assertIn("bad.js", out)

    def test_no_js_files_at_all_passes_with_its_own_message(self):
        code, out = self._run()
        self.assertEqual(code, 0, out)
        self.assertIn("No JS files found", out)

    def test_a_minified_js_file_is_never_checked(self):
        _write(os.path.join(self.tmp, "mod_a", "static", "src", "lib", "vendor.min.js"), "const x=;")
        code, out = self._run()
        self.assertEqual(code, 0, out)
        self.assertIn("No JS files found", out)

    def test_node_modules_is_never_walked(self):
        _write(os.path.join(self.tmp, "node_modules", "pkg", "index.js"), "const x = ;\n")
        code, out = self._run()
        self.assertEqual(code, 0, out)

    def test_a_static_lib_directory_is_excluded_like_a_vendored_directory(self):
        # Real, specific exclusion rule: a "lib" dir whose parent directory
        # is literally named "static" is pruned, mirroring this codebase's
        # own vendoring convention (see check_external_library_locality.py
        # in this same sweep).
        _write(
            os.path.join(self.tmp, "mod_a", "static", "lib", "vendored.js"), "const x = ;\n"
        )
        code, out = self._run()
        self.assertEqual(code, 0, out)
        self.assertIn("No JS files found", out)

    def test_the_radae_directory_is_never_walked(self):
        _write(os.path.join(self.tmp, "some_daemon", "radae", "bad.js"), "const x = ;\n")
        code, out = self._run()
        self.assertEqual(code, 0, out)

    def test_an_ignore_file_pattern_excludes_a_matching_path(self):
        _write(os.path.join(self.tmp, "mod_a", "static", "src", "js", "bad.js"), "const x = ;\n")
        ignore_path = os.path.join(self.tmp, "ignore.txt")
        _write(ignore_path, "bad\\.js$\n")
        code, out = self._run("--ignore-file", ignore_path)
        self.assertEqual(code, 0, out)
        self.assertIn("No JS files found", out)


if __name__ == "__main__":
    unittest.main()
