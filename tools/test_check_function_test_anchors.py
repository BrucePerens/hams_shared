#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_function_test_anchors.py (ADR 0090).
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_function_test_anchors as cfta  # noqa: E402


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _init_git_repo(root):
    # scan_tree walks `git ls-files`, not a raw os.walk -- a real repo
    # (even an empty, un-pushed one) is needed for these fixtures to be
    # discovered at all.
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)


class DirectFunctionsTests(unittest.TestCase):
    def test_a_module_level_function_is_found(self):
        import ast

        tree = ast.parse("def foo():\n    pass\n")
        found = list(cfta._direct_functions(tree.body, []))
        self.assertEqual([q for q, _n in found], ["foo"])

    def test_a_class_method_is_found_with_its_qualified_name(self):
        import ast

        tree = ast.parse("class Foo:\n    def bar(self):\n        pass\n")
        found = list(cfta._direct_functions(tree.body, []))
        self.assertEqual([q for q, _n in found], ["Foo.bar"])

    def test_a_nested_closure_is_not_found(self):
        import ast

        tree = ast.parse("def outer():\n    def inner():\n        pass\n    return inner\n")
        found = list(cfta._direct_functions(tree.body, []))
        self.assertEqual([q for q, _n in found], ["outer"])

    def test_an_async_function_is_found(self):
        import ast

        tree = ast.parse("async def foo():\n    pass\n")
        found = list(cfta._direct_functions(tree.body, []))
        self.assertEqual([q for q, _n in found], ["foo"])


class ScanFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_function_with_a_base_anchor_is_reported_as_anchored(self):
        path = os.path.join(self.tmp, "foo.py")
        _write(path, "def bar():\n    # [@ANCHOR: COMM_bar]\n    pass\n")
        results = cfta.scan_file(path, self.tmp)
        self.assertEqual(results, [("foo.py::bar", True)])

    def test_a_function_with_no_anchor_is_reported_as_unanchored(self):
        path = os.path.join(self.tmp, "foo.py")
        _write(path, "def bar():\n    pass\n")
        results = cfta.scan_file(path, self.tmp)
        self.assertEqual(results, [("foo.py::bar", False)])

    def test_a_begin_end_anchor_counts_as_anchored(self):
        path = os.path.join(self.tmp, "foo.py")
        _write(
            path,
            "def bar():\n    # [@ANCHOR-BEGIN: COMM_bar]\n    pass\n    # [@ANCHOR-END: COMM_bar]\n",
        )
        results = cfta.scan_file(path, self.tmp)
        self.assertEqual(results, [("foo.py::bar", True)])

    def test_a_syntax_error_file_is_skipped_not_crashed_on(self):
        path = os.path.join(self.tmp, "broken.py")
        _write(path, "def broken(:\n    pass\n")
        results = cfta.scan_file(path, self.tmp)
        self.assertEqual(results, [])

    def test_an_anchor_on_a_different_function_does_not_count(self):
        path = os.path.join(self.tmp, "foo.py")
        _write(
            path,
            "def bar():\n    pass\n\n\ndef baz():\n    # [@ANCHOR: COMM_baz]\n    pass\n",
        )
        results = dict(cfta.scan_file(path, self.tmp))
        self.assertFalse(results["foo.py::bar"])
        self.assertTrue(results["foo.py::baz"])

    def test_an_anchor_above_a_decorator_counts_as_anchored(self):
        # The real bug found sweeping compliance/controllers/main.py: a
        # decorated function's own node.lineno is the `def` line, NOT the
        # decorator -- an anchor comment placed above the decorator (this
        # codebase's own established convention) must still count.
        path = os.path.join(self.tmp, "foo.py")
        _write(
            path,
            "class C:\n"
            "    # [@ANCHOR: COMM_bar]\n"
            "    @http.route('/x')\n"
            "    def bar(self):\n"
            "        pass\n",
        )
        results = dict(cfta.scan_file(path, self.tmp))
        self.assertTrue(results["foo.py::C.bar"])

    def test_the_decorator_lookback_does_not_cross_a_blank_line_into_the_prior_function(self):
        # The real, named risk of the fix above: an unrelated trailing
        # comment on the PREVIOUS function, separated by the ordinary
        # blank line between two defs, must not be absorbed as if it
        # anchored this one.
        path = os.path.join(self.tmp, "foo.py")
        _write(
            path,
            "def bar():\n"
            "    pass\n"
            "    # [@ANCHOR: COMM_bar]\n"
            "\n"
            "@http.route('/x')\n"
            "def baz():\n"
            "    pass\n",
        )
        results = dict(cfta.scan_file(path, self.tmp))
        self.assertFalse(results["foo.py::baz"])


class ScanTreeAndBaselineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scan_tree_only_sees_git_tracked_files(self):
        _write(os.path.join(self.tmp, "tracked.py"), "def a():\n    pass\n")
        _init_git_repo(self.tmp)
        # Written AFTER the repo's own `git add -A`/init above, and never
        # added itself -- genuinely untracked, not just uncommitted.
        _write(os.path.join(self.tmp, "untracked.py"), "def b():\n    pass\n")
        gaps = cfta.scan_tree(self.tmp)
        self.assertIn("tracked.py::a", gaps)
        self.assertNotIn("untracked.py::b", gaps)

    def test_a_test_file_itself_is_not_scanned(self):
        _write(os.path.join(self.tmp, "tests", "test_foo.py"), "def test_something():\n    pass\n")
        _init_git_repo(self.tmp)
        gaps = cfta.scan_tree(self.tmp)
        self.assertEqual(gaps, {})

    def test_a_tools_directory_is_not_scanned(self):
        _write(os.path.join(self.tmp, "tools", "helper.py"), "def helper():\n    pass\n")
        _init_git_repo(self.tmp)
        gaps = cfta.scan_tree(self.tmp)
        self.assertEqual(gaps, {})

    def test_baseline_round_trips_through_save_and_load(self):
        gaps = {"foo.py::bar": True, "baz.py::qux": True}
        path = os.path.join(self.tmp, "baseline.json")
        cfta.save_baseline(path, gaps)
        loaded = cfta.load_baseline(path)
        self.assertEqual(sorted(loaded), sorted(gaps.keys()))

    def test_load_baseline_returns_empty_dict_when_file_is_absent(self):
        self.assertEqual(cfta.load_baseline(os.path.join(self.tmp, "nonexistent.json")), {})


class MainIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.baseline = os.path.join(self.tmp, "baseline.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *extra_args):
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "check_function_test_anchors.py")
        return subprocess.run(
            [sys.executable, script, self.tmp, "--baseline", self.baseline, *extra_args],
            capture_output=True,
            text=True,
        )

    def test_a_new_unanchored_function_fails_with_no_baseline(self):
        _write(os.path.join(self.tmp, "foo.py"), "def bar():\n    pass\n")
        _init_git_repo(self.tmp)
        result = self._run()
        self.assertEqual(result.returncode, 1)
        self.assertIn("foo.py::bar", result.stdout)

    def test_generating_a_baseline_then_checking_again_passes(self):
        _write(os.path.join(self.tmp, "foo.py"), "def bar():\n    pass\n")
        _init_git_repo(self.tmp)
        gen = self._run("--generate-baseline")
        self.assertEqual(gen.returncode, 0)
        check = self._run()
        self.assertEqual(check.returncode, 0)

    def test_a_function_added_after_the_baseline_is_a_new_failure(self):
        _write(os.path.join(self.tmp, "foo.py"), "def bar():\n    pass\n")
        _init_git_repo(self.tmp)
        self._run("--generate-baseline")

        _write(os.path.join(self.tmp, "foo.py"), "def bar():\n    pass\n\n\ndef new_one():\n    pass\n")
        result = self._run()
        self.assertEqual(result.returncode, 1)
        self.assertIn("foo.py::new_one", result.stdout)
        self.assertNotIn("foo.py::bar\n", result.stdout)

    def test_anchoring_a_baselined_function_keeps_it_passing(self):
        _write(os.path.join(self.tmp, "foo.py"), "def bar():\n    pass\n")
        _init_git_repo(self.tmp)
        self._run("--generate-baseline")

        _write(os.path.join(self.tmp, "foo.py"), "def bar():\n    # [@ANCHOR: COMM_bar]\n    pass\n")
        result = self._run()
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
