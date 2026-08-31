#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for verify_anchors.py (ADR-0054, ADR-0055, ADR-0074).

main() computes its own `repo_root` from this script's real on-disk
location (not from the scanned target directory), so every location string
it builds is relative to the real hams_shared root even when scanning a
/tmp fixture -- verified empirically before writing these tests: the
round-trip through os.path.relpath()/os.path.join()/os.path.abspath() still
resolves back to the real fixture path correctly, so main() works
hermetically against a fixture despite that. Also verified empirically: a
docs/stories/ (or journeys/) directory must live INSIDE the module
directory (e.g. mod_a/docs/stories/x.md), not at a fixture's top level
separate from the module, for get_module() to attribute it to the same
module as the code anchor it documents -- a flat top-level docs/ directory
resolves to module "global" instead, which is a real, worth-knowing
behavior of get_module()'s fallback chain, not a test-authoring shortcut.
No real-repo assertions anywhere in this file: verify_anchors.py currently
fails against the real hams_open/hams_com tree (a real, pre-existing
documentation-coverage backlog, tracked separately in night_shift_todo.md,
not this sweep's concern), so any real-repo assertion here would be
guaranteed-fragile on arrival.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import verify_anchors as va  # noqa: E402

_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify_anchors.py")
_ANCHOR_PATTERN = re.compile(r"\[@ANCHOR:\s*([a-zA-Z0-9_:]+)\s*\]")


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


class CleanTests(unittest.TestCase):
    def test_strips_comm_prefix(self):
        self.assertEqual(va._clean("COMM_my_feature"), "my_feature")

    def test_strips_pri_prefix(self):
        self.assertEqual(va._clean("PRI_my_feature"), "my_feature")

    def test_leaves_an_unprefixed_name_alone(self):
        self.assertEqual(va._clean("my_feature"), "my_feature")


class GetModuleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_file_under_a_real_manifest_resolves_to_that_module(self):
        _write(os.path.join(self.tmp, "mod_a", "__manifest__.py"), "{}\n")
        p = os.path.join(self.tmp, "mod_a", "models", "foo.py")
        _write(p, "pass\n")
        self.assertEqual(va.get_module(p), "mod_a")

    def test_a_docs_modules_markdown_file_resolves_to_its_own_filename(self):
        p = os.path.join(self.tmp, "docs", "modules", "ham_qso.md")
        _write(p, "docs\n")
        self.assertEqual(va.get_module(p), "ham_qso")

    def test_a_daemons_subdirectory_file_with_no_manifest_resolves_to_the_daemon_name(self):
        p = os.path.join(self.tmp, "daemons", "some_daemon", "src", "main.rs")
        _write(p, "// x\n")
        self.assertEqual(va.get_module(p), "some_daemon")

    def test_a_file_under_a_recognized_common_dir_with_no_manifest_falls_back_to_its_parent(self):
        p = os.path.join(self.tmp, "orphan_mod", "models", "foo.py")
        _write(p, "pass\n")
        self.assertEqual(va.get_module(p), "orphan_mod")

    def test_a_file_matching_nothing_falls_back_to_global(self):
        p = os.path.join(self.tmp, "some_random_dir", "notes.txt")
        _write(p, "x\n")
        self.assertEqual(va.get_module(p), "global")


class IsPrimaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_primary_dirs_means_everything_is_primary(self):
        self.assertTrue(va.is_primary("./anything.py:1", [], self.tmp))

    def test_a_location_under_a_primary_dir_is_primary(self):
        primary = os.path.join(self.tmp, "mod_a")
        loc = f"./{os.path.relpath(os.path.join(primary, 'foo.py'), self.tmp)}:1"
        self.assertTrue(va.is_primary(loc, [primary], self.tmp))

    def test_a_location_outside_every_primary_dir_is_not_primary(self):
        primary = os.path.join(self.tmp, "mod_a")
        other = os.path.join(self.tmp, "mod_b", "foo.py")
        loc = f"./{os.path.relpath(other, self.tmp)}:1"
        self.assertFalse(va.is_primary(loc, [primary], self.tmp))

    def test_explicit_non_primary_wins_even_over_a_matching_primary_dir(self):
        primary = os.path.join(self.tmp, "mod_a")
        loc = f"./{os.path.relpath(os.path.join(primary, 'foo.py'), self.tmp)}:1"
        self.assertFalse(
            va.is_primary(loc, [primary], self.tmp, explicit_non_primary=[primary])
        )


class FindAnchorsInDocsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_readme_anchor_is_a_contract_anchor(self):
        _write(os.path.join(self.tmp, "mod_a", "__manifest__.py"), "{}\n")
        _write(os.path.join(self.tmp, "mod_a", "README.md"), "[@ANCHOR: COMM_x]\n")
        docs, contracts, _lines = va.find_anchors_in_docs(self.tmp, self.tmp)
        self.assertIn("mod_a:COMM_x", contracts)
        self.assertNotIn("mod_a:COMM_x", docs)

    def test_a_docs_directory_markdown_anchor_is_a_plain_doc_anchor(self):
        _write(os.path.join(self.tmp, "mod_a", "__manifest__.py"), "{}\n")
        _write(os.path.join(self.tmp, "mod_a", "docs", "stories", "x.md"), "[@ANCHOR: COMM_x]\n")
        docs, contracts, _lines = va.find_anchors_in_docs(self.tmp, self.tmp)
        self.assertIn("mod_a:COMM_x", docs)
        self.assertNotIn("mod_a:COMM_x", contracts)

    def test_llm_linter_guide_is_always_skipped(self):
        _write(os.path.join(self.tmp, "docs", "LLM_LINTER_GUIDE.md"), "[@ANCHOR: COMM_x]\n")
        docs, contracts, _lines = va.find_anchors_in_docs(self.tmp, self.tmp)
        self.assertEqual(docs, {})
        self.assertEqual(contracts, {})

    def test_an_explicit_module_prefix_overrides_the_files_own_inferred_module(self):
        _write(
            os.path.join(self.tmp, "mod_a", "docs", "stories", "x.md"),
            "[@ANCHOR: other_module:COMM_x]\n",
        )
        docs, _contracts, _lines = va.find_anchors_in_docs(self.tmp, self.tmp)
        self.assertIn("other_module:COMM_x", docs)

    def test_radae_is_never_walked(self):
        _write(os.path.join(self.tmp, "radae", "docs", "x.md"), "[@ANCHOR: COMM_x]\n")
        docs, contracts, _lines = va.find_anchors_in_docs(self.tmp, self.tmp)
        self.assertEqual(docs, {})
        self.assertEqual(contracts, {})


class FindAnchorsInCodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _scan(self):
        return va.find_anchors_in_code(self.tmp, self.tmp)

    def test_a_base_anchor_declaration_is_captured(self):
        _write(os.path.join(self.tmp, "mod_a", "models", "foo.py"), "# [@ANCHOR: COMM_x]\n")
        code_anchors, anchor_locations, *_rest = self._scan()
        self.assertIn("mod_a:COMM_x", code_anchors)
        self.assertIn("mod_a:COMM_x", anchor_locations)

    def test_a_tests_link_is_captured_separately_from_a_base_declaration(self):
        _write(os.path.join(self.tmp, "mod_a", "tests", "test_foo.py"), "# Tests [@ANCHOR: COMM_x]\n")
        code_anchors, _locs, tests_links, tests_links_set, *_rest = self._scan()
        self.assertIn("mod_a:COMM_x", code_anchors)
        self.assertIn("mod_a:COMM_x", tests_links_set)
        self.assertEqual(len(tests_links), 1)

    def test_a_verified_by_link_is_captured(self):
        _write(os.path.join(self.tmp, "mod_a", "models", "foo.py"), "# # Verified by [@ANCHOR: COMM_x]\n")
        *_rest, verified_by_links, _audit_ignore, _cross_refs, _dups, _lines = self._scan()
        self.assertIn("mod_a:COMM_x", verified_by_links)

    def test_a_triggers_link_is_captured_as_a_cross_reference(self):
        _write(os.path.join(self.tmp, "mod_a", "models", "foo.py"), "# Triggers [@ANCHOR: mod_b:COMM_x]\n")
        *_rest, _verified, cross_references, _dups, _lines = self._scan()
        self.assertIn("mod_b:COMM_x", cross_references)

    def test_a_genuine_duplicate_base_declaration_is_flagged(self):
        _write(
            os.path.join(self.tmp, "mod_a", "models", "foo.py"),
            "# [@ANCHOR: COMM_x]\nsome code\n# [@ANCHOR: COMM_x]\n",
        )
        *_rest, duplicates, _lines = self._scan()
        self.assertEqual(len(duplicates), 1)

    def test_an_example_prefixed_anchor_repeated_is_never_a_duplicate(self):
        _write(
            os.path.join(self.tmp, "mod_a", "models", "foo.py"),
            "# [@ANCHOR: COMM_example_x]\nsome code\n# [@ANCHOR: COMM_example_x]\n",
        )
        *_rest, duplicates, _lines = self._scan()
        self.assertEqual(duplicates, [])

    def test_a_doc_prefixed_anchor_is_not_treated_as_a_base_code_anchor(self):
        _write(os.path.join(self.tmp, "mod_a", "models", "foo.py"), "# [@ANCHOR: COMM_story_x]\n")
        code_anchors, anchor_locations, *_rest = self._scan()
        self.assertEqual(code_anchors, {})
        self.assertEqual(anchor_locations, {})

    def test_a_conversational_reference_ending_in_see_is_ignored(self):
        _write(os.path.join(self.tmp, "mod_a", "models", "foo.py"), "# See [@ANCHOR: COMM_x]\n")
        code_anchors, anchor_locations, *_rest = self._scan()
        self.assertEqual(code_anchors, {})
        self.assertEqual(anchor_locations, {})


class ReportDuplicatesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_primary_duplicate_is_reported(self):
        dup = ("mod_a:COMM_x", "./mod_a/models/b.py:5", ["./mod_a/models/a.py:1"])
        self.assertTrue(va._report_duplicates([dup], [], self.tmp))

    def test_no_duplicates_reports_nothing(self):
        self.assertFalse(va._report_duplicates([], [], self.tmp))


class ReportMissingTestsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_tests_link_to_a_nonexistent_anchor_is_flagged(self):
        filepath = os.path.join(self.tmp, "mod_a", "tests", "test_foo.py")
        tests_links = {filepath: [("mod_a:COMM_ghost", 3)]}
        self.assertTrue(
            va._report_missing_tests(tests_links, {}, {}, self.tmp, [])
        )

    def test_a_tests_link_to_a_real_anchor_is_not_flagged(self):
        filepath = os.path.join(self.tmp, "mod_a", "tests", "test_foo.py")
        tests_links = {filepath: [("mod_a:COMM_real", 3)]}
        code_anchors = {"mod_a:COMM_real": ["./mod_a/models/foo.py:1"]}
        self.assertFalse(
            va._report_missing_tests(tests_links, code_anchors, {}, self.tmp, [])
        )

    def test_a_non_primary_filepath_is_never_checked(self):
        filepath = os.path.join(self.tmp, "mod_b", "tests", "test_foo.py")
        tests_links = {filepath: [("mod_b:COMM_ghost", 3)]}
        primary = os.path.join(self.tmp, "mod_a")
        self.assertFalse(
            va._report_missing_tests(tests_links, {}, {}, self.tmp, [primary])
        )


class ReportMissingCrossRefsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_triggers_target_that_does_not_exist_anywhere_is_flagged(self):
        cross_references = {"mod_b:COMM_ghost": ["./mod_a/models/foo.py:5"]}
        self.assertTrue(
            va._report_missing_cross_refs(cross_references, {}, {}, [], self.tmp)
        )

    def test_a_triggers_target_that_exists_in_code_anchors_is_not_flagged(self):
        cross_references = {"mod_b:COMM_real": ["./mod_a/models/foo.py:5"]}
        code_anchors = {"mod_b:COMM_real": ["./mod_b/models/bar.py:1"]}
        self.assertFalse(
            va._report_missing_cross_refs(
                cross_references, code_anchors, {}, [], self.tmp
            )
        )

    def test_a_triggers_target_that_exists_only_as_a_contract_anchor_is_not_flagged(self):
        cross_references = {"mod_b:COMM_real": ["./mod_a/models/foo.py:5"]}
        contract_anchors = {"mod_b:COMM_real": ["./mod_b/README.md:1"]}
        self.assertFalse(
            va._report_missing_cross_refs(
                cross_references, {}, contract_anchors, [], self.tmp
            )
        )

    def test_a_triggers_source_with_only_non_primary_locations_is_never_checked(self):
        cross_references = {"mod_b:COMM_ghost": ["./mod_b/models/foo.py:5"]}
        primary = os.path.join(self.tmp, "mod_a")
        self.assertFalse(
            va._report_missing_cross_refs(cross_references, {}, {}, [primary], self.tmp)
        )


class ReportBidirectionalOrphansTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_source_anchor_with_no_test_link_is_an_orphaned_source(self):
        code_anchors = {"mod_a:COMM_x": ["./mod_a/models/foo.py:1"]}
        has_errors, source_anchors = va._report_bidirectional_orphans(
            code_anchors, {}, {}, {}, [], self.tmp
        )
        self.assertTrue(has_errors)
        self.assertIn("mod_a:COMM_x", source_anchors)

    def test_a_source_anchor_with_a_matching_test_link_is_not_orphaned(self):
        code_anchors = {"mod_a:COMM_x": ["./mod_a/models/foo.py:1"]}
        tests_links_set = {"mod_a:COMM_x": ["./mod_a/tests/test_foo.py:1"]}
        has_errors, _source_anchors = va._report_bidirectional_orphans(
            code_anchors, tests_links_set, {}, {}, [], self.tmp
        )
        self.assertFalse(has_errors)

    def test_an_unverified_test_anchor_is_an_orphaned_test(self):
        code_anchors = {"mod_a:test_COMM_x": ["./mod_a/tests/test_foo.py:1"]}
        has_errors, _source_anchors = va._report_bidirectional_orphans(
            code_anchors, {}, {}, {}, [], self.tmp
        )
        self.assertTrue(has_errors)

    def test_test_tour_signup_is_specifically_exempt_from_the_orphaned_test_check(self):
        # A real, hardcoded exception in the script's own logic -- verified
        # rather than assumed to still apply.
        code_anchors = {"mod_a:test_tour_signup": ["./mod_a/tests/test_foo.py:1"]}
        has_errors, _source_anchors = va._report_bidirectional_orphans(
            code_anchors, {}, {}, {}, [], self.tmp
        )
        self.assertFalse(has_errors)


class ReportDocumentationGapsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_source_anchor_missing_from_docs_is_flagged(self):
        source_anchors = {"mod_a:COMM_x": ["./mod_a/models/foo.py:1"]}
        self.assertTrue(
            va._report_documentation_gaps(source_anchors, {}, {}, {}, [], self.tmp)
        )

    def test_a_source_anchor_present_in_docs_is_not_flagged(self):
        # code_anchors must also contain the anchor here: the function
        # separately checks the inverse direction (a docs_anchors entry
        # missing from code_anchors trips its own "missing_in_code"
        # branch), so an empty code_anchors would trip that branch for
        # this same key and defeat the point of this test.
        source_anchors = {"mod_a:COMM_x": ["./mod_a/models/foo.py:1"]}
        docs_anchors = {"mod_a:COMM_x": ["./mod_a/docs/stories/x.md:1"]}
        code_anchors = {"mod_a:COMM_x": ["./mod_a/models/foo.py:1"]}
        self.assertFalse(
            va._report_documentation_gaps(
                source_anchors, docs_anchors, code_anchors, {}, [], self.tmp
            )
        )

    def test_a_doc_anchor_with_no_matching_code_anchor_still_sets_has_errors(self):
        # Real, non-obvious behavior: this branch prints "CI/CD WARNING"
        # (not "FAILURE" like every other category), but still sets
        # has_errors = True -- it DOES fail the build, the "WARNING" label
        # is misleading. Verified directly, not assumed from the message
        # text.
        docs_anchors = {"mod_a:COMM_ghost": ["./mod_a/docs/stories/x.md:1"]}
        self.assertTrue(
            va._report_documentation_gaps({}, docs_anchors, {}, {}, [], self.tmp)
        )

    def test_a_story_prefixed_doc_anchor_missing_from_code_is_not_flagged(self):
        docs_anchors = {"mod_a:COMM_story_x": ["./mod_a/docs/stories/x.md:1"]}
        self.assertFalse(
            va._report_documentation_gaps({}, docs_anchors, {}, {}, [], self.tmp)
        )


class ReportDummyBlocksTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_two_consecutive_anchor_lines_are_flagged_as_stacked(self):
        filepath = os.path.join(self.tmp, "mod_a", "tests", "test_foo.py")
        all_lines = {filepath: {5, 6}}
        self.assertTrue(va._report_dummy_blocks(all_lines, [], self.tmp))

    def test_non_consecutive_anchor_lines_are_not_flagged(self):
        filepath = os.path.join(self.tmp, "mod_a", "tests", "test_foo.py")
        all_lines = {filepath: {5, 9}}
        self.assertFalse(va._report_dummy_blocks(all_lines, [], self.tmp))

    def test_a_single_anchor_line_is_never_flagged(self):
        filepath = os.path.join(self.tmp, "mod_a", "tests", "test_foo.py")
        all_lines = {filepath: {5}}
        self.assertFalse(va._report_dummy_blocks(all_lines, [], self.tmp))

    def test_the_check_is_blind_to_anchor_role_a_base_declaration_next_to_a_verified_by_still_trips_it(self):
        # The real, empirically-discovered behavior that cost a fixture
        # rewrite in MainIntegrationTests: this check only looks at
        # adjacent line numbers, not what kind of anchor comment is on
        # them. A base declaration immediately followed by an unrelated
        # "# # Verified by" comment for a DIFFERENT anchor trips it exactly
        # like two stacked test declarations would -- going through the
        # real find_anchors_in_code() scan, not a hand-built line-number
        # set, so a future change that makes the two functions disagree
        # about what counts as "adjacent anchors" would be caught here.
        _write(
            os.path.join(self.tmp, "mod_a", "models", "foo.py"),
            "# [@ANCHOR: COMM_my_feature]\n"
            "# # Verified by [@ANCHOR: COMM_test_my_feature]\n"
            "class Foo:\n    pass\n",
        )
        _code_anchors, _locs, _tests, _tests_set, _verified, _audit_ignore, _cross, _dups, code_anchor_lines = (
            va.find_anchors_in_code(self.tmp, self.tmp)
        )
        self.assertTrue(va._report_dummy_blocks(code_anchor_lines, [], self.tmp))


class ReportMissingUxDocsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_ux_anchor_missing_from_the_user_manual_is_flagged(self):
        code_anchors = {"mod_a:COMM_UX_x": ["./mod_a/static/src/js/x.js:1"]}
        self.assertTrue(va._report_missing_ux_docs(code_anchors, set(), [], self.tmp))

    def test_a_ux_anchor_present_in_the_user_manual_is_not_flagged(self):
        code_anchors = {"mod_a:COMM_UX_x": ["./mod_a/static/src/js/x.js:1"]}
        self.assertFalse(
            va._report_missing_ux_docs(code_anchors, {"mod_a:COMM_UX_x"}, [], self.tmp)
        )

    def test_a_non_ux_anchor_is_never_considered(self):
        code_anchors = {"mod_a:COMM_x": ["./mod_a/models/foo.py:1"]}
        self.assertFalse(va._report_missing_ux_docs(code_anchors, set(), [], self.tmp))


class MainIntegrationTests(unittest.TestCase):
    """Hermetic subprocess runs against real temp-directory fixtures only --
    verify_anchors.py currently fails against the real repo tree (a
    pre-existing documentation-coverage backlog, not this sweep's
    concern), so no real-repo assertion is safe to make here."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self):
        result = subprocess.run(
            [sys.executable, _SCRIPT, self.tmp], capture_output=True, text=True, timeout=30
        )
        return result.returncode, result.stdout + result.stderr

    def test_a_fully_traced_feature_passes(self):
        # A base anchor, a test link, a verified-by back-link, and a docs
        # entry INSIDE the module directory (verified empirically: a
        # top-level docs/ separate from the module resolves to module
        # "global" instead and would not satisfy this same-module check).
        # The base anchor and the Verified-by anchor are deliberately kept
        # on non-adjacent lines: two anchor-bearing lines right next to
        # each other trips _report_dummy_blocks's own separate "stacked
        # anchors" check regardless of the anchors' different roles here,
        # found empirically while first writing this fixture.
        _write(os.path.join(self.tmp, "mod_a", "__manifest__.py"), "{}\n")
        _write(
            os.path.join(self.tmp, "mod_a", "models", "foo.py"),
            "# [@ANCHOR: COMM_my_feature]\n"
            "class Foo:\n"
            "    pass\n"
            "# # Verified by [@ANCHOR: COMM_test_my_feature]\n",
        )
        _write(
            os.path.join(self.tmp, "mod_a", "tests", "test_foo.py"),
            "# Tests [@ANCHOR: COMM_my_feature]\n"
            "class TestFoo:\n    def test_my_feature(self):\n        pass\n",
        )
        _write(
            os.path.join(self.tmp, "mod_a", "docs", "stories", "my_feature.md"),
            "# My Feature\n[@ANCHOR: COMM_my_feature]\n",
        )
        code, out = self._run()
        self.assertEqual(code, 0, out)
        self.assertIn("SUCCESS", out)

    def test_a_source_anchor_with_no_test_or_doc_coverage_fails(self):
        _write(os.path.join(self.tmp, "mod_a", "__manifest__.py"), "{}\n")
        _write(
            os.path.join(self.tmp, "mod_a", "models", "foo.py"),
            "# [@ANCHOR: COMM_orphan_feature]\n",
        )
        code, out = self._run()
        self.assertEqual(code, 1)
        self.assertIn("CI/CD FAILURE", out)

    def test_a_duplicate_base_anchor_across_two_files_fails(self):
        _write(os.path.join(self.tmp, "mod_a", "__manifest__.py"), "{}\n")
        _write(os.path.join(self.tmp, "mod_a", "models", "a.py"), "# [@ANCHOR: COMM_dup]\n")
        _write(os.path.join(self.tmp, "mod_a", "models", "b.py"), "# [@ANCHOR: COMM_dup]\n")
        code, out = self._run()
        self.assertEqual(code, 1)
        self.assertIn("Duplicate Semantic Anchors", out)


if __name__ == "__main__":
    unittest.main()
