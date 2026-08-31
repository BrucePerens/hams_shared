#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_xml_comment_double_hyphen.py.
"""

import os
import shutil
import sys
import tempfile
import unittest

from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_xml_comment_double_hyphen as cxc  # noqa: E402

_SETTINGS = settings(max_examples=200, deadline=None)

# Alphanumeric-plus-space only: keeps generated "safe" filler lines free of
# "-", "<", ">", "&" so they can never accidentally form "--" (a false
# positive) or "-->" (prematurely closing the comment), regardless of how
# many lines or how they're joined -- the property tests below only need
# to reason about the ONE deliberately-inserted "--", not worry the
# strategy might smuggle in an incidental second one.
_SAFE_LINE = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), max_codepoint=127),
    min_size=0,
    max_size=20,
)


class CheckXmlCommentDoubleHyphenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, relpath, content):
        path = os.path.join(self.tmp, relpath)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def test_flags_an_em_dash_style_double_hyphen(self):
        self._write(
            "some_module/security/security_rules.xml",
            "<odoo>\n"
            "<!-- some explanation -- with an aside -->\n"
            "<record/>\n"
            "</odoo>\n",
        )
        violations = cxc.check_xml_comment_double_hyphen(self.tmp)
        self.assertEqual(len(violations), 1)
        self.assertIn("security_rules.xml:2", violations[0])

    def test_flags_a_multiline_comment_double_hyphen(self):
        self._write(
            "some_module/views/some_views.xml",
            "<odoo>\n"
            "<!-- line one is fine\n"
            "     line two has a -- double hyphen\n"
            "     line three is fine -->\n"
            "</odoo>\n",
        )
        violations = cxc.check_xml_comment_double_hyphen(self.tmp)
        self.assertEqual(len(violations), 1)
        self.assertIn("some_views.xml:3", violations[0])

    def test_does_not_flag_a_clean_comment(self):
        self._write(
            "some_module/security/security_rules.xml",
            "<odoo>\n"
            "<!-- some explanation, with an aside -->\n"
            "<record/>\n"
            "</odoo>\n",
        )
        violations = cxc.check_xml_comment_double_hyphen(self.tmp)
        self.assertEqual(violations, [])

    def test_does_not_flag_double_hyphens_outside_comments(self):
        self._write(
            "some_module/views/some_views.xml",
            '<odoo>\n<field name="domain">[(1, \'=\', 1)]</field>\n</odoo>\n',
        )
        violations = cxc.check_xml_comment_double_hyphen(self.tmp)
        self.assertEqual(violations, [])

    def test_ignores_non_xml_files(self):
        self._write("some_module/models/foo.py", "# some comment -- with an aside\n")
        violations = cxc.check_xml_comment_double_hyphen(self.tmp)
        self.assertEqual(violations, [])


class ResolveRepoRootTests(unittest.TestCase):
    def test_a_hams_shared_path_redirects_to_its_parent_repo(self):
        fake_repo = os.path.join(os.sep, "some", "workspace", "some_repo")
        self.assertEqual(
            cxc._resolve_repo_root(os.path.join(fake_repo, "hams_shared")),
            fake_repo,
        )

    def test_a_real_repo_root_passes_through_unchanged(self):
        fake_repo = os.path.join(os.sep, "some", "workspace", "some_repo")
        self.assertEqual(cxc._resolve_repo_root(fake_repo), fake_repo)


class ResolveRepoRootsTests(unittest.TestCase):
    # Regression test for the second half of the same bug class, found 2026-08-27:
    # _resolve_repo_root's redirect only ever lands on ONE repo (hams_shared's literal parent) --
    # but real Odoo XML data/view files exist in both hams_open and hams_com, so the single-repo
    # redirect left hams_com entirely unscanned via run_linters.py's actual invocation.
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.workspace = os.path.join(self.tmp, "workspace")
        os.makedirs(self.workspace)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_repo(self, name, module_names=()):
        repo = os.path.join(self.workspace, name)
        for module_name in module_names:
            manifest_path = os.path.join(repo, module_name, "__manifest__.py")
            os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
            with open(manifest_path, "w", encoding="utf-8") as f:
                f.write("{}")
        return repo

    def test_hams_shared_input_appends_the_real_hams_com_sibling(self):
        self._make_repo("hams_open", module_names=["zero_sudo"])
        hams_com = self._make_repo("hams_com", module_names=["ham_base"])
        hams_shared = os.path.join(self.workspace, "hams_open", "hams_shared")
        os.makedirs(hams_shared)
        roots = cxc._resolve_repo_roots(hams_shared)
        self.assertEqual(roots, [os.path.join(self.workspace, "hams_open"), hams_com])

    def test_a_real_repo_root_with_no_odoo_sibling_present_scans_alone(self):
        repo = self._make_repo("hams_open", module_names=["zero_sudo"])
        self.assertEqual(cxc._resolve_repo_roots(repo), [repo])

    def test_a_sibling_directory_with_no_manifest_py_anywhere_is_not_treated_as_a_repo(self):
        repo = self._make_repo("hams_open", module_names=["zero_sudo"])
        os.makedirs(os.path.join(self.workspace, "hams_com", "not_a_module"))
        self.assertEqual(cxc._resolve_repo_roots(repo), [repo])


@st.composite
def _comment_with_one_double_hyphen(draw):
    # docs/proposals/CODE_REVIEW_PROCESS.md's own "Python: no direct
    # equivalent, but real tools exist" section names Hypothesis as a real
    # strengthening for exactly this shape of target -- a small, pure
    # parser/validator, the same category as generate_odoo_core_stubs.py's
    # _render_method_params() (already covered) -- applied here to the
    # checker that itself exists because this session hit its own bug
    # class twice tonight (see check_xml_comment_double_hyphen.py's own
    # docstring). Builds a multi-line <!-- ... --> comment with exactly
    # one "--" inserted at a random line, and independently tracks which
    # line that is (by counting "\n" in the text built so far), so the
    # property test can check the checker's reported line number against
    # a computation that doesn't reuse the checker's own logic.
    before = draw(st.lists(_SAFE_LINE, min_size=0, max_size=5))
    after = draw(st.lists(_SAFE_LINE, min_size=0, max_size=5))
    # A blank (zero-length) prefix is the case that actually caught a real
    # bug: match.start() (whole-match start, including the 4-char "<!--"
    # delimiter) plus inner.index("--") landed 4 characters too early,
    # which only crosses back over the PRECEDING newline -- an
    # off-by-one-line miscount -- when "--" is the first thing on its own
    # line. A non-empty prefix masks the bug entirely (still 4 characters
    # short, but short of a position on the SAME line, so the reported
    # line number comes out right by coincidence). Both cases stay in the
    # strategy: the non-empty-prefix case keeps guarding the general
    # "right line, no false positive" property, the empty-prefix case is
    # the actual regression guard for the bug found and fixed here.
    prefix = draw(st.one_of(st.just(""), _SAFE_LINE))
    suffix = draw(_SAFE_LINE)
    hyphen_line = f"{prefix}--{suffix}"
    # Inserted directly rather than via a placeholder-and-replace step:
    # _SAFE_LINE's alphabet excludes "-" entirely, so no generated line can
    # ever contain "--" on its own, and this way there's no placeholder
    # string that could itself collide with a randomly-generated line.
    lines = before + [hyphen_line] + after
    hyphen_line_index = len(before)  # 0-indexed position of the "--" line
    content = "<odoo>\n<!--\n" + "\n".join(lines) + "\n-->\n</odoo>\n"
    # Line numbers are 1-indexed; "<odoo>" is line 1, "<!--" is line 2, so
    # lines[0] is line 3 and lines[hyphen_line_index] is line 3+that index.
    expected_lineno = 3 + hyphen_line_index
    return content, expected_lineno


class CheckXmlCommentDoubleHyphenPropertyTests(unittest.TestCase):
    @given(_comment_with_one_double_hyphen())
    @_SETTINGS
    def test_reports_exactly_one_violation_at_the_right_line(self, generated):
        content, expected_lineno = generated
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "some_module", "views", "some_views.xml")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            violations = cxc.check_xml_comment_double_hyphen(tmp)
            self.assertEqual(
                len(violations),
                1,
                f"[!] DIAGNOSTIC FOR AI: expected exactly one violation for "
                f"content={content!r}, got {violations!r}",
            )
            self.assertIn(f"some_views.xml:{expected_lineno} ", violations[0])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @given(st.lists(_SAFE_LINE, min_size=0, max_size=8))
    @_SETTINGS
    def test_never_flags_a_comment_with_no_double_hyphen_anywhere(self, lines):
        content = "<odoo>\n<!--\n" + "\n".join(lines) + "\n-->\n</odoo>\n"
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "some_module", "views", "some_views.xml")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            self.assertEqual(cxc.check_xml_comment_double_hyphen(tmp), [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
