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

    def test_a_code_islands_py_file_is_not_double_counted_as_a_doc_anchor(self):
        # Real bug found live, 2026-09-06: a genuine PDF-generation pipeline
        # (build_spec.py/build_drawings.py/make_pdf.py) lives under
        # docs/proposals/.../_pipeline -- this scanner used to treat every
        # .py file under any docs/ subtree as documentation, so its real,
        # correctly-anchored-and-tested functions were silently classified
        # as doc-only references, producing false "missing from operational
        # source code" reports. CODE_ISLANDS_UNDER_DOCS carves this one
        # directory out so its .py files are only ever code (see the
        # matching FindAnchorsInCodeTests case below), while its own
        # README.md (if any) still counts here, as a normal contract anchor.
        island = va.CODE_ISLANDS_UNDER_DOCS[0]
        _write(os.path.join(self.tmp, island, "build_spec.py"), "[@ANCHOR: patent_pipeline:x]\n")
        _write(os.path.join(self.tmp, island, "README.md"), "[@ANCHOR: patent_pipeline:x]\n")
        docs, contracts, _lines = va.find_anchors_in_docs(self.tmp, self.tmp)
        self.assertNotIn("patent_pipeline:x", docs, "the .py file itself must not be treated as doc content")
        self.assertIn("patent_pipeline:x", contracts, "the island's own README.md is still a real contract anchor")


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

    def test_a_code_islands_py_file_under_docs_is_scanned_as_real_code(self):
        # Companion to FindAnchorsInDocsTests' matching case above -- the
        # same fixture must resolve as real code here, not just "not doc
        # content" there. A plain .py file elsewhere under docs/ (NOT one
        # of CODE_ISLANDS_UNDER_DOCS) must still be invisible to this scan,
        # confirming the carve-out is narrow, not a blanket "docs/ is code
        # after all" reversal.
        island = va.CODE_ISLANDS_UNDER_DOCS[0]
        _write(os.path.join(self.tmp, island, "build_spec.py"), "# [@ANCHOR: patent_pipeline:x]\n")
        _write(os.path.join(self.tmp, "docs", "proposals", "unrelated.py"), "# [@ANCHOR: should_never_appear:y]\n")
        code_anchors, *_rest = self._scan()
        self.assertIn("patent_pipeline:x", code_anchors)
        self.assertNotIn("should_never_appear:y", code_anchors)

    def test_a_begin_marker_base_declaration_is_captured_the_same_as_plain(self):
        # 2026-09-04 (ADR 0089, Bruce's own request): [@ANCHOR-BEGIN: name] /
        # [@ANCHOR-END: name] must resolve identically to a plain
        # [@ANCHOR: name] everywhere this tool looks for one -- otherwise a
        # multi-line anchor written per check_burn_list.py's own syntax
        # would be silently invisible here.
        _write(os.path.join(self.tmp, "mod_a", "models", "foo.py"), "# [@ANCHOR-BEGIN: COMM_x]\nbody\n# [@ANCHOR-END: COMM_x]\n")
        code_anchors, anchor_locations, *_rest = self._scan()
        self.assertIn("mod_a:COMM_x", code_anchors)
        self.assertIn("mod_a:COMM_x", anchor_locations)

    def test_a_begin_marker_tests_link_is_captured_the_same_as_plain(self):
        _write(os.path.join(self.tmp, "mod_a", "tests", "test_foo.py"), "# Tests [@ANCHOR-BEGIN: COMM_x]\nbody\n# [@ANCHOR-END: COMM_x]\n")
        code_anchors, _locs, tests_links, tests_links_set, *_rest = self._scan()
        self.assertIn("mod_a:COMM_x", code_anchors)
        self.assertIn("mod_a:COMM_x", tests_links_set)

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

    # ---- previous-line lookback parity with check_function_test_anchors.py (2026-09-17 fix) ----
    #
    # Real, live divergence found and fixed: check_function_test_anchors.py's own
    # _is_base_anchor_declaration gained a prev_line lookback 2026-09-13 for a comment that wraps
    # its referencing word onto the line BEFORE a same-line-prefix-empty anchor -- but this
    # module's own _process_file_for_anchors, despite that function's docstring claiming to mirror
    # it exactly, never got the same fix. Confirmed against the real repo: hams_com
    # ham_shack/tests/test_station_timeshare_security.py:53-54 and
    # ham_shack/tests/test_operator_chat_friends_api.py:122-123 (both `# ...(see` /
    # `# [@ANCHOR: ham_operator_friendship_create_authz]) -- ...`) were each reported as a false
    # "Duplicate Semantic Anchors" CI failure against the function's real declaration in
    # ham_shack/models/ham_operator_friendship.py, purely from this gap.

    def test_a_wrapped_conversational_reference_is_not_a_base_declaration(self):
        _write(
            os.path.join(self.tmp, "mod_a", "tests", "test_foo.py"),
            "# some prose ending in (see\n# [@ANCHOR: COMM_x]) -- go through the real flow instead\n",
        )
        code_anchors, anchor_locations, *_rest = self._scan()
        self.assertNotIn("mod_a:COMM_x", anchor_locations)
        self.assertNotIn("mod_a:COMM_x", code_anchors)

    def test_a_wrapped_tests_link_is_captured_as_a_tests_link_not_a_base_declaration(self):
        _write(
            os.path.join(self.tmp, "mod_a", "tests", "test_foo.py"),
            "# Tests\n# [@ANCHOR: COMM_x]\n",
        )
        code_anchors, _locs, _tests_links, tests_links_set, *_rest = self._scan()
        self.assertIn("mod_a:COMM_x", tests_links_set)

    def test_a_wrapped_verified_by_link_is_captured_as_a_verified_by_link(self):
        _write(
            os.path.join(self.tmp, "mod_a", "models", "foo.py"),
            "# # Verified by\n# [@ANCHOR: COMM_x]\n",
        )
        *_rest, verified_by_links, _audit_ignore, _cross_refs, _dups, _lines = self._scan()
        self.assertIn("mod_a:COMM_x", verified_by_links)

    def test_a_wrapped_triggers_link_is_captured_as_a_cross_reference(self):
        _write(
            os.path.join(self.tmp, "mod_a", "models", "foo.py"),
            "# Triggers\n# [@ANCHOR: mod_b:COMM_x]\n",
        )
        *_rest, _verified, cross_references, _dups, _lines = self._scan()
        self.assertIn("mod_b:COMM_x", cross_references)

    def test_a_wrapped_reference_does_not_falsely_duplicate_the_real_base_declaration(self):
        # The exact live shape: a real base declaration in one file, and elsewhere a wrapped
        # conversational reference to the same anchor -- must never be flagged as a second
        # declaration of it.
        _write(os.path.join(self.tmp, "mod_a", "models", "foo.py"), "# [@ANCHOR: COMM_x]\n")
        _write(
            os.path.join(self.tmp, "mod_a", "tests", "test_bar.py"),
            "# some prose ending in (see\n# [@ANCHOR: COMM_x]) -- elsewhere\n",
        )
        *_rest, duplicates, _lines = self._scan()
        self.assertEqual(duplicates, [])

    def test_a_real_same_line_declaration_is_not_overridden_by_the_previous_line(self):
        # The lookback must never win over a real same-line signal -- a genuine base declaration
        # immediately below unrelated prose (not itself a Tests/Verified-by/conversational marker)
        # stays a base declaration.
        _write(
            os.path.join(self.tmp, "mod_a", "models", "foo.py"),
            "# Some unrelated context here.\n# [@ANCHOR: COMM_x]\n",
        )
        code_anchors, anchor_locations, *_rest = self._scan()
        self.assertIn("mod_a:COMM_x", anchor_locations)

    def test_the_lookback_only_applies_to_the_first_anchor_on_a_line(self):
        # A second anchor sharing a line with a first one is classified by the text BETWEEN them,
        # never by the line above -- the lookback exists for "this whole line has no signal of its
        # own", not for an anchor that merely has little text immediately before it mid-line.
        _write(
            os.path.join(self.tmp, "mod_a", "models", "foo.py"),
            "# Tests\n# [@ANCHOR: COMM_x][@ANCHOR: COMM_y]\n",
        )
        code_anchors, anchor_locations, *_rest = self._scan()
        # The first anchor on the line correctly falls back to "# Tests" on the line above.
        self.assertNotIn("mod_a:COMM_x", anchor_locations)
        # The second, with nothing but the first anchor's own closing bracket before it, must NOT
        # also inherit the line-above fallback -- it stays a (spurious but real) base declaration.
        self.assertIn("mod_a:COMM_y", anchor_locations)

    def test_a_real_begin_end_pair_with_body_between_does_not_produce_adjacent_anchor_lines(self):
        # The real practical question this widening raises: does a normal
        # multi-line anchor (content between BEGIN and END, the whole
        # point of the syntax) accidentally look like the "stacked/dummy"
        # shape _report_dummy_blocks flags? It shouldn't -- BEGIN and END
        # land on lines 1 and 3 here, not adjacent, so code_anchor_lines
        # for this file must not contain two consecutive line numbers.
        _write(
            os.path.join(self.tmp, "mod_a", "tests", "test_foo.py"),
            "# Tests [@ANCHOR-BEGIN: COMM_x]\nreal_test_body_here()\n# [@ANCHOR-END: COMM_x]\n",
        )
        *_rest, code_anchor_lines = self._scan()
        lines = sorted(next(iter(code_anchor_lines.values())))
        self.assertEqual(lines, [1, 3])

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

    def test_a_second_anchor_on_the_same_line_is_classified_by_its_own_preceding_text(self):
        # Real bug found 2026-09-10: classification used to reuse the text before the FIRST
        # anchor on a line for EVERY anchor on that line. A base declaration followed on the
        # same physical line by a real "# Verified by [@ANCHOR: ...]" comment silently dropped
        # the verification link entirely -- the second anchor was misclassified as a second base
        # declaration (using the first anchor's own empty/"#" prefix) instead of being recorded
        # in verified_by_links. Confirmed empirically against the pre-fix source before writing
        # this test. Each anchor must now be classified using the text between the END of the
        # previous anchor on the line (or the line start, for the first) and its OWN start.
        _write(
            os.path.join(self.tmp, "mod_a", "models", "foo.py"),
            "# [@ANCHOR: COMM_feature] # Verified by [@ANCHOR: test_it]\n",
        )
        code_anchors, anchor_locations, _tl, _tls, verified_by_links, *_rest = self._scan()
        self.assertIn("mod_a:COMM_feature", anchor_locations)
        self.assertIn("mod_a:test_it", verified_by_links)
        # The second anchor must NOT also have been recorded as a second base declaration.
        self.assertNotIn("mod_a:test_it", anchor_locations)

    def test_a_tests_tag_followed_by_a_conversational_second_anchor_is_not_double_counted(self):
        # The other direction of the same bug: a real "# Tests [@ANCHOR: real_target]" comment
        # followed on the same line by an incidental "and [@ANCHOR: mentioned_elsewhere]" mention
        # used to silently record `mentioned_elsewhere` as test-covered too, purely because it
        # shared a line with a real Tests-tag -- exactly the kind of false "tested" classification
        # that could mask a real coverage gap for `mentioned_elsewhere`. The conversational-word
        # regex (`\b(See|and|also|or|to)\b$`) exists precisely to catch this shape, but could never
        # fire for anything but the first anchor on a line before this fix.
        _write(
            os.path.join(self.tmp, "mod_a", "tests", "test_foo.py"),
            "# Tests [@ANCHOR: real_target] and [@ANCHOR: mentioned_elsewhere]\n",
        )
        code_anchors, _locs, _tl, tests_links_set, *_rest = self._scan()
        self.assertIn("mod_a:real_target", tests_links_set)
        self.assertNotIn("mod_a:mentioned_elsewhere", tests_links_set)
        self.assertNotIn("mod_a:mentioned_elsewhere", code_anchors)

    def test_a_base_declaration_whose_preceding_text_merely_ends_in_the_word_tests_is_not_a_tests_link(self):
        # Real bug found 2026-09-12, confirmed empirically: the "Tests"/"Verified by"/"Tested
        # by"/"Triggers"/"Triggered by" marker checks used bare `str.endswith(...)`, with no
        # word-boundary check at all. A perfectly ordinary base-declaration comment that happens
        # to end in a word SUFFIXED by "Tests" -- e.g. "UnitTests", nothing to do with the
        # documented `# Tests [@ANCHOR: ...]` convention -- silently misclassified the anchor as
        # test-covered, purely because "UnitTests".endswith("Tests") is True. Consequence: a truly
        # untested feature's own orphaned-source check (`a not in tests_links_set`) would
        # incorrectly conclude it already has test coverage, masking a real ADR-0054 gap -- a
        # false negative in the exact class of bug this campaign hunts for.
        _write(
            os.path.join(self.tmp, "mod_a", "models", "foo.py"),
            "# Runs our UnitTests [@ANCHOR: COMM_should_be_a_base_anchor]\n",
        )
        code_anchors, anchor_locations, _tl, tests_links_set, *_rest = self._scan()
        self.assertIn("mod_a:COMM_should_be_a_base_anchor", anchor_locations)
        self.assertNotIn("mod_a:COMM_should_be_a_base_anchor", tests_links_set)

    def test_a_base_declaration_whose_preceding_text_ends_in_a_word_glued_onto_triggers_is_not_a_cross_reference(self):
        # Same bug, the "Triggers" marker: "MisTriggers" ends with the substring "Triggers" too.
        _write(
            os.path.join(self.tmp, "mod_a", "models", "foo.py"),
            "# MisTriggers [@ANCHOR: COMM_should_be_a_base_anchor]\n",
        )
        code_anchors, anchor_locations, _tl, _tls, _verified, _audit, cross_refs, *_rest = self._scan()
        self.assertIn("mod_a:COMM_should_be_a_base_anchor", anchor_locations)
        self.assertNotIn("mod_a:COMM_should_be_a_base_anchor", cross_refs)

    def test_a_base_declaration_whose_preceding_text_ends_in_a_word_glued_onto_verified_by_is_not_a_verified_by_link(self):
        # Same bug, the "Verified by" marker: "NotVerified by" ends with the phrase "Verified by".
        _write(
            os.path.join(self.tmp, "mod_a", "models", "foo.py"),
            "# NotVerified by [@ANCHOR: COMM_should_be_a_base_anchor]\n",
        )
        code_anchors, anchor_locations, _tl, _tls, verified_by_links, *_rest = self._scan()
        self.assertIn("mod_a:COMM_should_be_a_base_anchor", anchor_locations)
        self.assertNotIn("mod_a:COMM_should_be_a_base_anchor", verified_by_links)

    def test_a_genuine_tests_marker_with_extra_leading_prose_still_works_after_the_word_boundary_fix(self):
        # Guards against an over-correction: the word-boundary fix must not break the
        # already-permissive (and already-tested-elsewhere) acceptance of extra whitespace-
        # separated words before the marker, only the glued-word-suffix case.
        _write(
            os.path.join(self.tmp, "mod_a", "tests", "test_foo.py"),
            "# Add more Tests [@ANCHOR: COMM_x]\n",
        )
        code_anchors, _locs, _tl, tests_links_set, *_rest = self._scan()
        self.assertIn("mod_a:COMM_x", tests_links_set)


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

    def test_a_source_anchor_cited_in_the_modules_own_readme_is_not_flagged(self):
        # ADR 0090, Bruce's own direct decision, 2026-09-04: every function
        # should get a real doc citation, but WHERE depends on audience --
        # an infrastructure anchor a real user never sees can be cited in
        # the module's own README.md (already a recognized "contract"
        # location, see all_contracts above) rather than docs/stories/,
        # without being exempt from documentation altogether. Verified
        # directly, not assumed: README.md citations already flow into
        # contract_anchors via find_anchors_in_docs's own is_readme check.
        source_anchors = {"mod_a:COMM_infra_helper": ["./mod_a/models/foo.py:1"]}
        contract_anchors = {"mod_a:COMM_infra_helper": ["./mod_a/README.md:3"]}
        self.assertFalse(
            va._report_documentation_gaps(
                source_anchors, {}, {}, contract_anchors, [], self.tmp
            )
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


class AddSiblingRepoTargetTests(unittest.TestCase):
    # Real bug found 2026-09-10: the old check in main() tested for the substring
    # "hams_community" (a name no real directory in this codebase has ever used) or "hams_com"
    # anywhere in a target path. Since ANY path under a repo named "hams_com" trivially contains
    # the substring "hams_com", invoking verify_anchors.py unscoped from hams_com made that check
    # true purely by SELF-match, skipping sibling detection entirely -- confirmed empirically
    # against the real repo tree before writing this fix. Even when the check was false (e.g.
    # invoked from hams_open), the four candidate directory names it probed were
    # "hams_community"/"hams_com" only, never the real name "hams_open" -- so a hams_com-rooted
    # invocation could never have found hams_open as a sibling either way. Net effect:
    # verify_anchors.py invoked unscoped from hams_com never scanned hams_open at all, silently
    # missing every cross-repo anchor reference into it.
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_starting_from_hams_com_finds_the_real_hams_open_sibling(self):
        # The exact case the real bug got wrong: repo_root IS hams_com, target_dirs is just
        # [hams_com] -- hams_open must still be found and added as a sibling target.
        hams_com = os.path.join(self.tmp, "hams_com")
        hams_open = os.path.join(self.tmp, "hams_open")
        os.makedirs(hams_com)
        os.makedirs(hams_open)
        new_targets, warning = va._add_sibling_repo_target(hams_com, [hams_com])
        self.assertIn(hams_open, new_targets)
        self.assertIsNone(warning)

    def test_starting_from_hams_open_finds_the_real_hams_com_sibling(self):
        hams_com = os.path.join(self.tmp, "hams_com")
        hams_open = os.path.join(self.tmp, "hams_open")
        os.makedirs(hams_com)
        os.makedirs(hams_open)
        new_targets, warning = va._add_sibling_repo_target(hams_open, [hams_open])
        self.assertIn(hams_com, new_targets)
        self.assertIsNone(warning)

    def test_a_module_scoped_target_under_hams_com_still_finds_hams_open(self):
        # target_dirs need not literally be the repo root -- a module-scoped invocation
        # (run_linters.py's own `targets` list) passes a deeper path whose basename is the
        # module name, not "hams_com". The sibling must still be found.
        hams_com = os.path.join(self.tmp, "hams_com")
        hams_open = os.path.join(self.tmp, "hams_open")
        scoped_module = os.path.join(hams_com, "some_module")
        os.makedirs(scoped_module)
        os.makedirs(hams_open)
        new_targets, warning = va._add_sibling_repo_target(hams_com, [scoped_module])
        self.assertIn(hams_open, new_targets)

    def test_the_sibling_is_not_re_added_when_already_present(self):
        hams_com = os.path.join(self.tmp, "hams_com")
        hams_open = os.path.join(self.tmp, "hams_open")
        os.makedirs(hams_com)
        os.makedirs(hams_open)
        new_targets, warning = va._add_sibling_repo_target(hams_com, [hams_com, hams_open])
        self.assertEqual(new_targets, [hams_com, hams_open])
        self.assertIsNone(warning)

    def test_a_missing_sibling_directory_is_simply_not_added(self):
        hams_com = os.path.join(self.tmp, "hams_com")
        os.makedirs(hams_com)
        new_targets, warning = va._add_sibling_repo_target(hams_com, [hams_com])
        self.assertEqual(new_targets, [hams_com])
        self.assertIsNone(warning)

    def test_a_sibling_nested_as_a_child_is_still_found_but_warns(self):
        hams_com = os.path.join(self.tmp, "hams_com")
        nested_hams_open = os.path.join(hams_com, "hams_open")
        os.makedirs(nested_hams_open)
        new_targets, warning = va._add_sibling_repo_target(hams_com, [hams_com])
        self.assertIn(nested_hams_open, new_targets)
        self.assertIsNotNone(warning)
        self.assertIn("ANTI-PATTERN", warning)


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

    def test_a_duplicate_base_anchor_declared_once_in_each_of_two_scanned_target_dirs_is_flagged(self):
        # Real bug found 2026-09-12, confirmed empirically against this exact fixture before the
        # fix (zero "Duplicate Semantic Anchors" output): find_anchors_in_code's own duplicate
        # detection is scoped to a single call (one target_dir) -- main() calls it once per
        # `final_target` and merges the accumulated results afterward, but never re-checked the
        # merged `anchor_locations` for a base anchor declared once in EACH of two separately-
        # scanned target dirs. Since this tool's flagship use case is scanning hams_com and
        # hams_open TOGETHER (see AddSiblingRepoTargetTests / `_add_sibling_repo_target` above), a
        # genuine duplicate straddling that exact repo boundary went completely undetected -- the
        # other checks (missing-test-link, missing-docs) still correctly saw both locations,
        # because those operate on the already-merged dicts, but "Duplicate Semantic Anchors"
        # itself silently passed.
        dir_a = os.path.join(self.tmp, "repo_a")
        dir_b = os.path.join(self.tmp, "repo_b")
        _write(os.path.join(dir_a, "mod_a", "__manifest__.py"), "{}\n")
        _write(os.path.join(dir_a, "mod_a", "models", "foo.py"), "# [@ANCHOR: COMM_dup_cross_repo]\n")
        _write(os.path.join(dir_b, "mod_a", "__manifest__.py"), "{}\n")
        _write(os.path.join(dir_b, "mod_a", "models", "foo.py"), "# [@ANCHOR: COMM_dup_cross_repo]\n")
        result = subprocess.run(
            [sys.executable, _SCRIPT, dir_a, dir_b], capture_output=True, text=True, timeout=30
        )
        out = result.stdout + result.stderr
        self.assertEqual(result.returncode, 1, out)
        self.assertIn("Duplicate Semantic Anchors", out, out)
        self.assertIn("mod_a:COMM_dup_cross_repo", out, out)


class SiblingRepoScanningIntegrationTests(unittest.TestCase):
    """Real bug found 2026-09-12, confirmed empirically against the real hams_com/hams_open
    checkouts before the fix: main()'s own `repo_root` (this script's containing directory,
    deliberately `hams_shared` itself -- see this file's own module docstring) was being passed
    straight into `_add_sibling_repo_target`, whose own logic and unit tests (see
    AddSiblingRepoTargetTests above) assume that argument IS the enclosing hams_com/hams_open
    checkout, not hams_shared. Since `os.path.basename(<...>/hams_shared)` is always literally
    "hams_shared", the function's own `own_name`-exclusion logic never excluded either real repo
    name, so `has_sibling` went true purely because the PRIMARY scan target happened to be named
    "hams_com" (or "hams_open") -- never because the actual sibling had been added -- and even
    where that short-circuit didn't fire (a module-scoped invocation), the sibling-candidate paths
    were computed one directory level too deep to ever resolve. Net, directly-observed effect
    against the real repo: a real, unscoped `verify_anchors.py .` run from hams_com's own root
    never scanned hams_open at all -- e.g. `zero_sudo:*` anchors, whose real source lives only in
    hams_open, were reported as "missing from operational source code" purely because hams_open
    was silently never scanned.

    These fixtures reproduce the real script's own directory shape -- a COPY of the real
    verify_anchors.py under `<tmp>/hams_com/hams_shared/tools/` (so its own `__file__`-derived
    `repo_root` resolves exactly the way it does in the real checkout) with a real sibling
    `<tmp>/hams_open` directory -- and confirm a cross-repo `# Triggers` reference into the
    sibling resolves cleanly, both unscoped and module-scoped, matching the two real invocation
    shapes `run_linters.py` actually uses.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.hams_com = os.path.join(self.tmp, "hams_com")
        self.hams_open = os.path.join(self.tmp, "hams_open")
        self.script_copy = os.path.join(self.hams_com, "hams_shared", "tools", "verify_anchors.py")
        os.makedirs(os.path.dirname(self.script_copy), exist_ok=True)
        shutil.copy(_SCRIPT, self.script_copy)

        # A real anchor whose ONLY source-code declaration lives in the hams_open sibling.
        _write(os.path.join(self.hams_open, "mod_b", "__manifest__.py"), "{}\n")
        _write(
            os.path.join(self.hams_open, "mod_b", "models", "target.py"),
            "# [@ANCHOR: COMM_shared_target]\n",
        )
        _write(
            os.path.join(self.hams_open, "mod_b", "tests", "test_target.py"),
            "# Tests [@ANCHOR: COMM_shared_target]\n",
        )
        _write(
            os.path.join(self.hams_open, "mod_b", "docs", "stories", "target.md"),
            "[@ANCHOR: COMM_shared_target]\n",
        )

        # A hams_com module that cross-references it via the real `# Triggers` syntax.
        _write(os.path.join(self.hams_com, "mod_a", "__manifest__.py"), "{}\n")
        _write(
            os.path.join(self.hams_com, "mod_a", "models", "trigger.py"),
            "# Triggers [@ANCHOR: mod_b:COMM_shared_target]\n",
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_an_unscoped_run_from_the_hams_com_root_still_scans_the_hams_open_sibling(self):
        # The exact shape run_linters.py uses for an unscoped run: `targets == [repo_root]`.
        result = subprocess.run(
            [sys.executable, self.script_copy, self.hams_com],
            capture_output=True,
            text=True,
            timeout=30,
        )
        out = result.stdout + result.stderr
        self.assertNotIn("Missing Cross-Reference Target", out, out)
        self.assertNotIn(
            "mod_b:COMM_shared_target' is missing from operational source code", out, out
        )

    def test_a_module_scoped_run_under_hams_com_still_scans_the_hams_open_sibling(self):
        # The exact shape run_linters.py uses for a scoped run: `targets == resolved_mod_paths`,
        # a deeper path whose basename is a module name, not "hams_com" or "hams_open" -- the
        # sibling must still be found via the candidate-path search, not the has_sibling
        # short-circuit.
        result = subprocess.run(
            [sys.executable, self.script_copy, os.path.join(self.hams_com, "mod_a")],
            capture_output=True,
            text=True,
            timeout=30,
        )
        out = result.stdout + result.stderr
        self.assertNotIn("Missing Cross-Reference Target", out, out)


if __name__ == "__main__":
    unittest.main()


class BaselineRatchetTests(unittest.TestCase):
    """The baseline ratchet: findings already recorded in a baseline file are grandfathered, any
    finding NOT in it still fails, and with no baseline nothing is grandfathered."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.baseline = os.path.join(self.tmp, "baseline.json")
        self.saved = (va._BASELINE, set(va._CURRENT_FINDINGS))
        va._CURRENT_FINDINGS.clear()

    def tearDown(self):
        va._BASELINE = self.saved[0]
        va._CURRENT_FINDINGS.clear()
        va._CURRENT_FINDINGS.update(self.saved[1])
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fixture(self, anchors):
        target = os.path.join(self.tmp, "fixture")
        shutil.rmtree(target, ignore_errors=True)
        _write(os.path.join(target, "mod_a", "__manifest__.py"), "{}\n")
        body = "".join(f"# [@ANCHOR: COMM_{a}]\nclass C_{a}:\n    pass\n\n" for a in anchors)
        _write(os.path.join(target, "mod_a", "models", "foo.py"), body)
        return target

    def _run(self, target, *flags):
        r = subprocess.run(
            [sys.executable, _SCRIPT, target, "--baseline", self.baseline, *flags],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return r.returncode, r.stdout + r.stderr

    def test_load_returns_none_for_a_missing_file_and_round_trips_a_saved_one(self):
        self.assertIsNone(va.load_baseline(self.baseline))
        va.save_baseline(self.baseline, {"doc_gap|b:x", "duplicate|a:y"})
        self.assertEqual(va.load_baseline(self.baseline), {"doc_gap|b:x", "duplicate|a:y"})

    def test_is_grandfathered_records_every_finding_but_only_passes_baselined_ones(self):
        va._BASELINE = {"doc_gap|m:old"}
        self.assertTrue(va._is_grandfathered("doc_gap", "m:old"))
        self.assertFalse(va._is_grandfathered("doc_gap", "m:new"))
        self.assertFalse(va._is_grandfathered("no_test_link", "m:old"))
        self.assertEqual(
            va._CURRENT_FINDINGS,
            {"doc_gap|m:old", "doc_gap|m:new", "no_test_link|m:old"},
        )

    def test_no_baseline_grandfathers_nothing(self):
        va._BASELINE = None
        self.assertFalse(va._is_grandfathered("doc_gap", "m:old"))

    def test_documentation_gap_report_skips_a_baselined_anchor_only(self):
        source_anchors = {
            "mod_a:COMM_old": ["./mod_a/models/foo.py:1"],
            "mod_a:COMM_new": ["./mod_a/models/foo.py:9"],
        }
        va._BASELINE = {"doc_gap|mod_a:COMM_old"}
        self.assertTrue(
            va._report_documentation_gaps(source_anchors, {}, {}, {}, [], self.tmp)
        )
        va._BASELINE = {"doc_gap|mod_a:COMM_old", "doc_gap|mod_a:COMM_new"}
        self.assertFalse(
            va._report_documentation_gaps(source_anchors, {}, {}, {}, [], self.tmp)
        )

    def test_end_to_end_baseline_lets_current_gaps_pass_but_fails_a_new_one(self):
        target = self._fixture(["old_one"])
        code, _ = self._run(target)
        self.assertEqual(code, 1, "the fixture must fail with no baseline present")

        code, out = self._run(target, "--generate-baseline")
        self.assertEqual(code, 0, out)
        recorded = va.load_baseline(self.baseline)
        self.assertIn("doc_gap|mod_a:COMM_old_one", recorded)

        code, out = self._run(target)
        self.assertEqual(code, 0, out)
        self.assertNotIn("Code Feature", out)

        target = self._fixture(["old_one", "brand_new"])
        code, out = self._run(target)
        self.assertEqual(code, 1, out)
        self.assertIn("COMM_brand_new", out)
        self.assertNotIn("Code Feature 'mod_a:COMM_old_one'", out)

    def test_a_fixed_finding_is_reported_as_a_shrinkable_baseline_entry(self):
        target = self._fixture(["old_one"])
        self._run(target, "--generate-baseline")
        va.save_baseline(self.baseline, va.load_baseline(self.baseline) | {"doc_gap|mod_a:gone"})
        code, out = self._run(target)
        self.assertEqual(code, 0, out)
        self.assertIn("1 baseline entries no longer occur", out)
