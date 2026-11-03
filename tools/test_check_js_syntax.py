#!/usr/bin/env python3
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

    def test_a_malformed_regex_line_is_reported_and_skipped_not_fatal(self):
        """load_ignore_file's own re.compile(line) call had no exception handling -- a single
        typo'd regex anywhere in the ignore file (an unbalanced '(' is the classic case) raises
        an uncaught re.error, crashing the whole checker before it checks a single JS file. Not
        reachable via run_linters.py's own invocation today (it never passes --ignore-file at
        all), but a real crash risk for any future/manual invocation that does."""
        tmp = tempfile.mkdtemp()
        try:
            p = os.path.join(tmp, "ignore.txt")
            _write(p, "vendor/.*\nunbalanced(\nnode_modules/.*\n")
            patterns = chk.load_ignore_file(p)
            self.assertEqual(len(patterns), 2)
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

    def test_a_test_on_the_projects_own_harness_is_not_held_to_the_hoot_matcher_list(self):
        # daemons/shack_console's tests import expect from ./harness.js, which has toContain.
        p = self._test_js_path("import { test, expect } from './harness.js';\ntest('x', () => {\n    expect('abc').toContain('b');\n});\n")
        self.assertIsNone(chk.check_file(p))

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

    def test_pushstate_combined_with_document_location_is_flagged(self):
        p = self._test_js_path(
            'window.history.pushState({}, "", "?x=1");\n'
            "expect(document.location.search).toBe(\"?x=1\");\n"
        )
        result = chk.check_file(p)
        self.assertIsNotNone(result)
        _file_path, err_msg = result
        self.assertIn("UNTESTABLE URL MOCK", err_msg)

    def test_pushstate_alone_without_any_location_reference_is_not_flagged(self):
        # pushState/replaceState by itself, read back only via hoot's own
        # mockLocation (the correct fix), is exactly what this rule must
        # allow -- it must not forbid the fix it recommends.
        p = self._test_js_path(
            'window.history.pushState({}, "", "?x=1");\n'
            "expect(mockLocation.search).toBe(\"?x=1\");\n"
        )
        self.assertIsNone(chk.check_file(p))

    def test_document_location_alone_without_pushstate_is_not_flagged(self):
        p = self._test_js_path("expect(document.location.pathname).toBe(\"/foo\");\n")
        self.assertIsNone(chk.check_file(p))

    def test_pushstate_and_location_only_inside_comments_is_not_flagged(self):
        p = self._test_js_path(
            "// window.history.pushState(...) then document.location -- don't do this\n"
            "expect(true).toBe(true);\n"
        )
        self.assertIsNone(chk.check_file(p))


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


SYNTAX_CASES = {
    "ok_import.js": "import x from 'y';\nexport const z = x;\n",
    "ok_tla.js": "const a = await Promise.resolve(1);\nexport {a};\n",
    "ok_meta.js": "export const u = import.meta.url;\n",
    "ok_hashbang.js": "#!/usr/bin/env node\nexport {};\n",
    "ok_bom.js": "\ufeffexport const x = 1;\n",
    "bad_expr.js": "const x = ;\n",
    "bad_with.js": "with (a) {}\n",
    "bad_html_comment.js": "<!-- x\nfoo\n",
    "bad_octal.js": "'use strict'; 010\n",
    "bad_return.js": "return 5\n",
    "bad_enum.js": "var enum = 1;\n",
    "bad_await_function.js": "function f() { return await 1 }\n",
}


class BatchTests(unittest.TestCase):
    """The batch (one node process) must fail exactly the files that `node --check` fails on their own."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.paths = []
        for name, text in SYNTAX_CASES.items():
            path = os.path.join(self.tmp, name)
            _write(path, text)
            self.paths.append(path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _single(self):
        return sorted(p for p in self.paths if chk.check_file(p))

    def test_the_batch_fails_exactly_the_files_that_fail_on_their_own(self):
        self.assertEqual(sorted(p for p, _msg in chk.check_files(self.paths, 1)), self._single())
        self.assertEqual(self._single(), sorted(p for p in self.paths if os.path.basename(p).startswith("bad_")))

    def test_the_message_of_a_failure_is_the_one_node_check_prints(self):
        by_path = dict(chk.check_files(self.paths, 1))
        bad = os.path.join(self.tmp, "bad_expr.js")
        self.assertEqual(by_path[bad], chk.check_file(bad)[1])
        self.assertIn("bad_expr.js", by_path[bad])

    def test_a_batch_that_cannot_run_falls_back_to_one_process_per_file(self):
        original = chk.unparsable_in_batch
        chk.unparsable_in_batch = lambda items: None
        try:
            self.assertEqual(sorted(p for p, _msg in chk.check_files(self.paths, 1)), self._single())
        finally:
            chk.unparsable_in_batch = original

    def test_batches_split_at_the_batch_size(self):
        original = chk.BATCH_SIZE
        chk.BATCH_SIZE = 3
        try:
            self.assertEqual(sorted(p for p, _msg in chk.check_files(self.paths, 1)), self._single())
        finally:
            chk.BATCH_SIZE = original

    def test_the_audit_rules_still_fail_a_file_that_parses(self):
        path = os.path.join(self.tmp, "x.test.js")
        _write(path, "expect(a).toBeTruthy();\n")
        self.assertEqual([p for p, _msg in chk.check_files([path], 1)], [path])

    def test_many_files_take_one_node_process_not_one_each(self):
        calls = []
        real_run = chk.subprocess.run

        def counting_run(argv, *args, **kwargs):
            calls.append(argv)
            return real_run(argv, *args, **kwargs)

        chk.subprocess.run = counting_run
        try:
            ok = [p for p in self.paths if os.path.basename(p).startswith("ok_")]
            self.assertEqual(chk.check_files(ok, 1), [])
        finally:
            chk.subprocess.run = real_run
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
