#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_hoot_runner_coverage.py.
"""

import io
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

import check_hoot_runner_coverage as chk  # noqa: E402


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


_MANIFEST_WITH_TEST_JS = """{
    'name': 'fake_module',
    'assets': {
        'web.assets_unit_tests': [
            'fake_module/static/src/js/widget.js',
            'fake_module/static/tests/widget.test.js',
        ],
    },
}
"""

_MANIFEST_NO_TEST_JS = """{
    'name': 'fake_module',
    'assets': {
        'web.assets_unit_tests': [
            'fake_module/static/src/js/widget.js',
        ],
    },
}
"""

_HOOT_RUNNER_PY = """
class TestWidgetHoot(HamsHttpCase):
    def test_widget_hoot_suite_passes(self):
        self.browser_js(
            "/web/tests?headless&tag=fake_module_widget",
            "", "", login="admin", timeout=120,
            success_signal="[HOOT] Test suite succeeded",
        )
"""

_NON_HOOT_TEST_PY = """
class TestWidgetOther(HamsHttpCase):
    def test_something_else(self):
        pass
"""

# The JS-side match for _HOOT_RUNNER_PY's own "fake_module_widget" tag= request. Needed
# wherever a fixture writes an actual widget.test.js and check_wrapper_requests_declared_tag
# is in play (i.e. everywhere main() runs): a *.test.js written with no describe.current.tags()
# call declares nothing, so a wrapper requesting "fake_module_widget" would itself be flagged
# as requesting a tag nothing declares -- a real gap this fixture had before that check existed.
_TAGGED_SUITE_JS = """
describe("widget", () => {
    describe.current.tags("fake_module_widget");
    test("does a thing", () => { expect(1).toBe(1); });
});
"""


class CheckHootRunnerCoverageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                chk.main()
            except SystemExit as e:
                return e.code, buf.getvalue()
        return 0, buf.getvalue()

    def _run_with_argv(self, *roots):
        old_argv = sys.argv
        sys.argv = ["check_hoot_runner_coverage.py", *roots]
        try:
            return self._run()
        finally:
            sys.argv = old_argv

    def test_module_with_test_js_and_no_hoot_runner_is_flagged(self):
        mod = os.path.join(self.tmp, "fake_module")
        _write(os.path.join(mod, "__manifest__.py"), _MANIFEST_WITH_TEST_JS)
        code, out = self._run_with_argv(self.tmp)
        self.assertEqual(code, 1)
        self.assertIn("fake_module", out)
        self.assertIn("widget.test.js", out)

    def test_module_with_test_js_and_a_hoot_runner_is_clean(self):
        mod = os.path.join(self.tmp, "fake_module")
        _write(os.path.join(mod, "__manifest__.py"), _MANIFEST_WITH_TEST_JS)
        _write(os.path.join(mod, "tests", "test_widget_hoot.py"), _HOOT_RUNNER_PY)
        _write(os.path.join(mod, "static", "tests", "widget.test.js"), _TAGGED_SUITE_JS)
        code, out = self._run_with_argv(self.tmp)
        self.assertEqual(code, 0)
        self.assertNotIn("VIOLATION", out)

    def test_module_with_test_js_and_a_non_hoot_test_file_is_still_flagged(self):
        # A tests/test_*.py file that exists but never calls browser_js()
        # with a [HOOT] success_signal doesn't count -- it's not actually
        # running the hoot suite, just coincidentally present.
        mod = os.path.join(self.tmp, "fake_module")
        _write(os.path.join(mod, "__manifest__.py"), _MANIFEST_WITH_TEST_JS)
        _write(os.path.join(mod, "tests", "test_something_else.py"), _NON_HOOT_TEST_PY)
        code, out = self._run_with_argv(self.tmp)
        self.assertEqual(code, 1)
        self.assertIn("fake_module", out)

    def test_module_with_no_test_js_at_all_is_never_flagged(self):
        mod = os.path.join(self.tmp, "fake_module")
        _write(os.path.join(mod, "__manifest__.py"), _MANIFEST_NO_TEST_JS)
        code, out = self._run_with_argv(self.tmp)
        self.assertEqual(code, 0)
        self.assertNotIn("VIOLATION", out)

    def test_burn_ignore_marker_in_manifest_suppresses_the_violation(self):
        mod = os.path.join(self.tmp, "fake_module")
        marked = _MANIFEST_WITH_TEST_JS.replace(
            "{\n", "{\n    # burn-ignore-hoot-runner-coverage: tracked follow-up\n", 1
        )
        _write(os.path.join(mod, "__manifest__.py"), marked)
        code, out = self._run_with_argv(self.tmp)
        self.assertEqual(code, 0)
        self.assertNotIn("VIOLATION", out)

    def test_a_module_without_any_manifest_is_skipped_without_crashing(self):
        os.makedirs(os.path.join(self.tmp, "not_a_module"), exist_ok=True)
        code, out = self._run_with_argv(self.tmp)
        self.assertEqual(code, 0)

    def test_multiple_roots_are_both_scanned(self):
        mod_a = os.path.join(self.tmp, "repo_a", "fake_module_a")
        mod_b = os.path.join(self.tmp, "repo_b", "fake_module_b")
        _write(os.path.join(mod_a, "__manifest__.py"), _MANIFEST_WITH_TEST_JS)
        _write(os.path.join(mod_b, "__manifest__.py"), _MANIFEST_WITH_TEST_JS)
        code, out = self._run_with_argv(
            os.path.join(self.tmp, "repo_a"), os.path.join(self.tmp, "repo_b")
        )
        self.assertEqual(code, 1)
        self.assertIn("fake_module_a", out)
        self.assertIn("fake_module_b", out)



class UnbundledHootSuiteTests(unittest.TestCase):
    """The same silent-pass failure as this file's other tests, in the other direction: a
    *.test.js on disk that no bundle names. /web/tests loads only what a manifest lists, so the
    wrapper's tag matches nothing, and hoot reports an empty run as "Test suite succeeded" --
    exactly what browser_js() waits for. Six real files were in this state on 2026-09-15.

    Deliberately NOT a subclass of CheckHootRunnerCoverageTests: inheriting it to reuse setUp and
    _run_with_argv would re-run all of its tests under this class's name too, inflating the
    reported count with duplicates. Overstating how much testing happened is the exact failure
    this whole check exists to catch, so it would be a poor thing to do in its own test file.
    """

    setUp = CheckHootRunnerCoverageTests.setUp
    tearDown = CheckHootRunnerCoverageTests.tearDown
    _run = CheckHootRunnerCoverageTests._run
    _run_with_argv = CheckHootRunnerCoverageTests._run_with_argv

    def test_a_test_js_on_disk_that_no_bundle_lists_is_flagged(self):
        mod = os.path.join(self.tmp, "fake_module")
        _write(os.path.join(mod, "__manifest__.py"), _MANIFEST_WITH_TEST_JS)
        _write(os.path.join(mod, "tests", "test_widget_hoot.py"), _HOOT_RUNNER_PY)
        _write(os.path.join(mod, "static", "tests", "orphan.test.js"), "// never bundled\n")
        code, out = self._run_with_argv(self.tmp)
        self.assertEqual(code, 1)
        self.assertIn("UNBUNDLED HOOT SUITE", out)
        self.assertIn("orphan.test.js", out)

    def test_a_bundled_test_js_on_disk_is_not_flagged(self):
        """The discriminating case: same file on disk, but named in the manifest."""
        mod = os.path.join(self.tmp, "fake_module")
        _write(os.path.join(mod, "__manifest__.py"), _MANIFEST_WITH_TEST_JS)
        _write(os.path.join(mod, "tests", "test_widget_hoot.py"), _HOOT_RUNNER_PY)
        _write(os.path.join(mod, "static", "tests", "widget.test.js"), _TAGGED_SUITE_JS)
        code, out = self._run_with_argv(self.tmp)
        self.assertEqual(code, 0)
        self.assertNotIn("UNBUNDLED", out)

    def test_a_tour_file_under_static_tests_tours_is_not_flagged(self):
        """Tours live in web.assets_tests and are driven by start_tour, not by a hoot tag, so
        they are not this check's business."""
        mod = os.path.join(self.tmp, "fake_module")
        _write(os.path.join(mod, "__manifest__.py"), _MANIFEST_WITH_TEST_JS)
        _write(os.path.join(mod, "tests", "test_widget_hoot.py"), _HOOT_RUNNER_PY)
        _write(os.path.join(mod, "static", "tests", "widget.test.js"), _TAGGED_SUITE_JS)
        _write(os.path.join(mod, "static", "tests", "tours", "a_tour.test.js"), "// a tour\n")
        code, out = self._run_with_argv(self.tmp)
        self.assertEqual(code, 0)
        self.assertNotIn("UNBUNDLED", out)

    def test_a_file_listed_in_some_other_bundle_is_not_flagged(self):
        """Wired up on purpose, even if arguably in the wrong bundle. Deciding which bundle is
        right needs the assets_unit_tests_setup reasoning ham_shack's manifest records at
        length; this check only catches a file mentioned nowhere at all."""
        mod = os.path.join(self.tmp, "fake_module")
        _write(
            os.path.join(mod, "__manifest__.py"),
            "{\n"
            "    'name': 'fake_module',\n"
            "    'assets': {\n"
            "        'web.assets_unit_tests': ['fake_module/static/tests/widget.test.js'],\n"
            "        'web.assets_tests': ['fake_module/static/tests/elsewhere.test.js'],\n"
            "    },\n"
            "}\n",
        )
        _write(os.path.join(mod, "tests", "test_widget_hoot.py"), _HOOT_RUNNER_PY)
        _write(os.path.join(mod, "static", "tests", "widget.test.js"), _TAGGED_SUITE_JS)
        _write(os.path.join(mod, "static", "tests", "elsewhere.test.js"), "// other bundle\n")
        code, out = self._run_with_argv(self.tmp)
        self.assertEqual(code, 0)
        self.assertNotIn("UNBUNDLED", out)

    def test_a_module_with_no_static_tests_directory_is_not_flagged(self):
        mod = os.path.join(self.tmp, "fake_module")
        _write(os.path.join(mod, "__manifest__.py"), _MANIFEST_NO_TEST_JS)
        code, out = self._run_with_argv(self.tmp)
        self.assertEqual(code, 0)
        self.assertNotIn("UNBUNDLED", out)



_TAGGED_SUITE_NO_TESTS_JS = """
describe("widget", () => {
    describe.current.tags("fake_module_widget");
});
"""

_WRAPPER_ASKING_FOR_TAG = """
class TestWidgetHoot(HamsHttpCase):
    def test_widget_hoot_suite_passes(self):
        self.browser_js(
            "/web/tests?headless&tag=fake_module_widget",
            "", "", success_signal="[HOOT] Test suite succeeded",
        )
"""

_WRAPPER_ASKING_FOR_ANOTHER_TAG = """
class TestOtherHoot(HamsHttpCase):
    def test_other_hoot_suite_passes(self):
        self.browser_js(
            "/web/tests?headless&tag=fake_module_something_else",
            "", "", success_signal="[HOOT] Test suite succeeded",
        )
"""

_UNTAGGED_WRAPPER = """
class TestEverythingHoot(HamsHttpCase):
    def test_everything(self):
        self.browser_js(
            "/web/tests?headless",
            "", "", success_signal="[HOOT] Test suite succeeded",
        )
"""


class UntriggeredHootTagTests(unittest.TestCase):
    """The third face of the same silent pass, and the one this checker's own docstring used to
    name as not attempted: a suite that is bundled, loadable and tagged, which nothing ever asks
    /web/tests for. The module-level check is satisfied by a single runner, so a module can have
    a runner and still never trigger most of its suites -- which is exactly how
    user_websites/static/tests/violation_report.test.js went unexecuted.

    Standalone rather than a subclass, for the reason UnbundledHootSuiteTests states.
    """

    setUp = CheckHootRunnerCoverageTests.setUp
    tearDown = CheckHootRunnerCoverageTests.tearDown
    _run = CheckHootRunnerCoverageTests._run
    _run_with_argv = CheckHootRunnerCoverageTests._run_with_argv

    def _module(self, wrapper=None, suite_js=_TAGGED_SUITE_JS):
        mod = os.path.join(self.tmp, "fake_module")
        _write(os.path.join(mod, "__manifest__.py"), _MANIFEST_WITH_TEST_JS)
        _write(os.path.join(mod, "static", "tests", "widget.test.js"), suite_js)
        _write(os.path.join(mod, "tests", "test_widget_hoot.py"), wrapper or _HOOT_RUNNER_PY)
        return mod

    def test_a_tag_no_wrapper_asks_for_is_flagged(self):
        self._module(wrapper=_WRAPPER_ASKING_FOR_ANOTHER_TAG)
        code, out = self._run_with_argv(self.tmp)
        self.assertEqual(code, 1)
        self.assertIn("UNTRIGGERED HOOT SUITE", out)
        self.assertIn("fake_module_widget", out)

    def test_a_tag_a_wrapper_does_ask_for_is_clean(self):
        """The discriminating case: same suite, same module, one wrapper that asks for it."""
        self._module(wrapper=_WRAPPER_ASKING_FOR_TAG)
        code, out = self._run_with_argv(self.tmp)
        self.assertEqual(code, 0)
        self.assertNotIn("UNTRIGGERED", out)

    def test_a_wrapper_in_a_different_module_still_counts(self):
        """Tags are collected across every scanned root before any module is judged, because a
        wrapper and the suite it triggers need not live in the same module."""
        mod = self._module(wrapper=_WRAPPER_ASKING_FOR_ANOTHER_TAG)
        # _WRAPPER_ASKING_FOR_ANOTHER_TAG's own "fake_module_something_else" needs a real
        # declaration too, now that check_wrapper_requests_declared_tag exists. Appended into
        # the module's already-bundled widget.test.js (rather than a new file) so it doesn't
        # also trip the separate, pre-existing UNBUNDLED HOOT SUITE check.
        _write(
            os.path.join(mod, "static", "tests", "widget.test.js"),
            _TAGGED_SUITE_JS
            + '\ndescribe("something_else", () => {\n'
            '    describe.current.tags("fake_module_something_else");\n'
            '    test("y", () => { expect(1).toBe(1); });\n'
            "});\n",
        )
        other = os.path.join(self.tmp, "other_module")
        _write(os.path.join(other, "__manifest__.py"), _MANIFEST_NO_TEST_JS)
        _write(os.path.join(other, "tests", "test_elsewhere_hoot.py"), _WRAPPER_ASKING_FOR_TAG)
        code, out = self._run_with_argv(self.tmp)
        self.assertEqual(code, 0)
        self.assertNotIn("UNTRIGGERED", out)

    def test_an_untagged_wrapper_suppresses_the_check_and_says_so(self):
        """An untagged /web/tests run triggers every bundled suite, so while one exists, "no
        wrapper asks for this tag" is not evidence a suite never runs. Report that rather than
        either failing wrongly or silently ignoring it."""
        self._module(wrapper=_UNTAGGED_WRAPPER)
        code, out = self._run_with_argv(self.tmp)
        self.assertEqual(code, 0)
        self.assertIn("Untriggered-tag check skipped", out)

    def test_a_tagged_suite_with_no_test_call_is_flagged(self):
        """Hoot reports a describe block with no test() exactly like an empty run -- "Passed 0
        tests", then "Test suite succeeded" -- so a wrapper asking for it goes green having
        asserted nothing. Unlike a runtime skip, this one is textually checkable."""
        self._module(wrapper=_WRAPPER_ASKING_FOR_TAG, suite_js=_TAGGED_SUITE_NO_TESTS_JS)
        code, out = self._run_with_argv(self.tmp)
        self.assertEqual(code, 1)
        self.assertIn("EMPTY HOOT SUITE", out)

    def test_an_unbundled_tagged_suite_is_not_double_reported(self):
        """It is already reported as UNBUNDLED; reporting the same file twice would make the
        real count harder to read, which is the failure this whole checker is about."""
        mod = os.path.join(self.tmp, "fake_module")
        _write(os.path.join(mod, "__manifest__.py"), _MANIFEST_NO_TEST_JS)
        _write(os.path.join(mod, "tests", "test_widget_hoot.py"), _HOOT_RUNNER_PY)
        _write(os.path.join(mod, "static", "tests", "orphan.test.js"), _TAGGED_SUITE_JS)
        code, out = self._run_with_argv(self.tmp)
        self.assertEqual(code, 1)
        self.assertIn("UNBUNDLED HOOT SUITE", out)
        self.assertNotIn("UNTRIGGERED HOOT SUITE", out)


_WRAPPER_ASKING_FOR_UNDECLARED_TAG = """
class TestNonexistentHoot(HamsHttpCase):
    def test_nonexistent_hoot_suite_passes(self):
        self.browser_js(
            "/web/tests?headless&tag=fake_module_typo_tag",
            "", "", success_signal="[HOOT] Test suite succeeded",
        )
"""

_WRAPPER_WITH_UNDOCUMENTED_EXPECT_EMPTY = """
class TestNonexistentHoot(HamsHttpCase):
    def test_nonexistent_hoot_suite_passes(self):
        self.browser_js(
            "/web/tests?headless&tag=fake_module_typo_tag",
            "", "", success_signal="[HOOT] Test suite succeeded",
            expect_empty=True,
        )
"""

_WRAPPER_WITH_REASON_BUT_NO_EXPECT_EMPTY = """
class TestNonexistentHoot(HamsHttpCase):
    def test_nonexistent_hoot_suite_passes(self):
        self.browser_js(
            # hoot-tag-intentionally-undeclared: proves the empty-run guard fires
            "/web/tests?headless&tag=fake_module_typo_tag",
            "", "", success_signal="[HOOT] Test suite succeeded",
        )
"""

_WRAPPER_PROPERLY_EXEMPTED = """
class TestNonexistentHoot(HamsHttpCase):
    def test_nonexistent_hoot_suite_passes(self):
        self.browser_js(
            # hoot-tag-intentionally-undeclared: proves the empty-run guard fires
            "/web/tests?headless&tag=fake_module_typo_tag",
            "", "", success_signal="[HOOT] Test suite succeeded",
            expect_empty=True,
        )
"""

_WRAPPER_ASKING_FOR_TWO_TAGS_ONE_UNDECLARED = """
class TestTwoTagsHoot(HamsHttpCase):
    def test_two_tags_hoot_suite_passes(self):
        self.browser_js(
            "/web/tests?headless&tag=fake_module_widget+fake_module_typo_tag",
            "", "", success_signal="[HOOT] Test suite succeeded",
        )
"""


class WrapperRequestsDeclaredTagTests(unittest.TestCase):
    """The reverse of UntriggeredHootTagTests: a browser_js() wrapper asking /web/tests for a
    tag= that no describe.current.tags(...) anywhere declares -- a typo in a wrapper's own tag=
    is the realistic failure, and hoot reports the resulting empty run identically to a real
    pass, so nothing else catches it. Standalone rather than a subclass, for the reason
    UnbundledHootSuiteTests states.
    """

    setUp = CheckHootRunnerCoverageTests.setUp
    tearDown = CheckHootRunnerCoverageTests.tearDown
    _run = CheckHootRunnerCoverageTests._run
    _run_with_argv = CheckHootRunnerCoverageTests._run_with_argv

    def _module(self, wrapper):
        mod = os.path.join(self.tmp, "fake_module")
        _write(os.path.join(mod, "__manifest__.py"), _MANIFEST_WITH_TEST_JS)
        _write(os.path.join(mod, "static", "tests", "widget.test.js"), _TAGGED_SUITE_JS)
        _write(os.path.join(mod, "tests", "test_widget_hoot.py"), wrapper)
        return mod

    def test_a_tag_no_suite_declares_is_flagged(self):
        self._module(wrapper=_WRAPPER_ASKING_FOR_UNDECLARED_TAG)
        code, out = self._run_with_argv(self.tmp)
        self.assertEqual(code, 1)
        self.assertIn("REQUESTED HOOT TAG NEVER DECLARED", out)
        self.assertIn("fake_module_typo_tag", out)

    def test_a_tag_a_suite_does_declare_is_clean(self):
        """The discriminating case: same tag, same repo, a real describe() declares it."""
        self._module(wrapper=_WRAPPER_ASKING_FOR_TAG)
        code, out = self._run_with_argv(self.tmp)
        self.assertEqual(code, 0)
        self.assertNotIn("NEVER DECLARED", out)

    def test_a_declaration_in_a_different_module_still_counts(self):
        """Declared tags are collected across every scanned root before any wrapper is judged,
        matching collect_requested_tags's own cross-module reach."""
        mod = os.path.join(self.tmp, "fake_module")
        _write(os.path.join(mod, "__manifest__.py"), _MANIFEST_NO_TEST_JS)
        _write(os.path.join(mod, "tests", "test_widget_hoot.py"), _WRAPPER_ASKING_FOR_TAG)
        other = os.path.join(self.tmp, "other_module")
        # _MANIFEST_WITH_TEST_JS, not _MANIFEST_NO_TEST_JS: widget.test.js needs to be BUNDLED
        # here too, or the pre-existing UNBUNDLED HOOT SUITE check fires on it independently of
        # what this test is actually checking (its basename-only bundle match doesn't care that
        # the manifest's own paths still say "fake_module/..."). And bundling it means this
        # module now needs its own runner too, or module_has_hoot_runner's own check fires --
        # _HOOT_RUNNER_PY happens to request the same tag widget.test.js declares, so it costs
        # nothing extra.
        _write(os.path.join(other, "__manifest__.py"), _MANIFEST_WITH_TEST_JS)
        _write(os.path.join(other, "static", "tests", "widget.test.js"), _TAGGED_SUITE_JS)
        _write(os.path.join(other, "tests", "test_widget_hoot.py"), _HOOT_RUNNER_PY)
        code, out = self._run_with_argv(self.tmp)
        self.assertEqual(code, 0)
        self.assertNotIn("NEVER DECLARED", out)

    def test_undocumented_expect_empty_is_flagged(self):
        """expect_empty=True with no `# hoot-tag-intentionally-undeclared:` reason is itself
        suspicious -- it silences the runtime guard with no record of why."""
        self._module(wrapper=_WRAPPER_WITH_UNDOCUMENTED_EXPECT_EMPTY)
        code, out = self._run_with_argv(self.tmp)
        self.assertEqual(code, 1)
        self.assertIn("UNDOCUMENTED EMPTY HOOT RUN", out)

    def test_reason_without_expect_empty_is_flagged(self):
        """A reason comment with no matching expect_empty=True doesn't actually change
        anything -- HamsHttpCase's own empty-run guard would still fail the run."""
        self._module(wrapper=_WRAPPER_WITH_REASON_BUT_NO_EXPECT_EMPTY)
        code, out = self._run_with_argv(self.tmp)
        self.assertEqual(code, 1)
        self.assertIn("UNDECLARED HOOT TAG WITHOUT expect_empty", out)

    def test_expect_empty_paired_with_a_reason_is_the_documented_exception(self):
        """The one legitimate shape: test_hoot_empty_run_guard.py's own pattern, both halves
        present together. _MANIFEST_NO_TEST_JS, not the full _module() helper: this wrapper's
        tag is deliberately undeclared, so bundling an unrelated real widget.test.js here would
        leave IT untriggered and trip a different, pre-existing check instead."""
        mod = os.path.join(self.tmp, "fake_module")
        _write(os.path.join(mod, "__manifest__.py"), _MANIFEST_NO_TEST_JS)
        _write(os.path.join(mod, "tests", "test_widget_hoot.py"), _WRAPPER_PROPERLY_EXEMPTED)
        code, out = self._run_with_argv(self.tmp)
        self.assertEqual(code, 0)
        self.assertNotIn("NEVER DECLARED", out)
        self.assertNotIn("EMPTY HOOT RUN", out)
        self.assertNotIn("WITHOUT expect_empty", out)

    def test_one_undeclared_tag_in_a_multi_tag_request_is_flagged(self):
        """A tag= can request several suites at once, joined by `+` -- splitting matters, or an
        undeclared tag riding alongside a real one is invisible."""
        self._module(wrapper=_WRAPPER_ASKING_FOR_TWO_TAGS_ONE_UNDECLARED)
        code, out = self._run_with_argv(self.tmp)
        self.assertEqual(code, 1)
        self.assertIn("REQUESTED HOOT TAG NEVER DECLARED", out)
        self.assertIn("fake_module_typo_tag", out)
        self.assertNotIn("fake_module_widget,", out)  # the declared half is not also flagged


if __name__ == "__main__":
    unittest.main()
