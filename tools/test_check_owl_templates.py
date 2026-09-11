#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for check_owl_templates.py. Real, non-mocked headless-Chrome + real Owl compiler runs for
the actual compile-checking behavior, matching this codebase's own established "use the real tool,
don't mock it" precedent (test_check_rust_function_test_anchors.py's own module doc comment) --
this is exactly the integration boundary (a real browser compiling real Owl templates) a mock would
hide the one real failure mode this tool exists to catch.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from check_owl_templates import find_owl_template_files  # noqa: E402

_SCRIPT = str(Path(__file__).parent / "check_owl_templates.py")


class FindOwlTemplateFilesTests(unittest.TestCase):
    def test_finds_a_file_under_static_src_xml(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "some_module" / "static" / "src" / "xml" / "foo.xml"
            target.parent.mkdir(parents=True)
            target.write_text("<templates/>", encoding="utf-8")
            found = list(find_owl_template_files(tmpdir))
        self.assertEqual(found, [str(target)])

    def test_ignores_an_ordinary_views_xml_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "some_module" / "views" / "foo_views.xml"
            target.parent.mkdir(parents=True)
            target.write_text("<odoo/>", encoding="utf-8")
            found = list(find_owl_template_files(tmpdir))
        self.assertEqual(found, [])

    def test_ignores_a_directory_whose_name_merely_contains_xml_as_a_substring(self):
        """Real bug found reviewing this checker: the "cheap prefilter" computes
        os.path.join(os.sep, "static", os.sep, "src", os.sep, "xml"), intending "/static/src/xml"
        -- but os.path.join resets to the last absolute-path argument it sees, and every os.sep
        argument here IS itself absolute, so the actual value is just "/xml" (confirmed directly:
        `os.path.join(os.sep, "static", os.sep, "src", os.sep, "xml")` returns '/xml', not
        '/static/src/xml'). Any directory whose path contains the substring "/xml" ANYWHERE (not
        just as its own trailing 'static/src/xml' path component) makes the cheap check's `not in`
        test False, short-circuiting the whole `and` before the real, precise `parts[-3:] ==
        ["static", "src", "xml"]` check ever runs -- so a directory like
        'some_module/static/src/xml_export/' (a plausible real name, NOT a genuine Owl-template
        directory) has every .xml file under it wrongly yielded as an Owl template candidate."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "some_module" / "static" / "src" / "xml_export" / "foo.xml"
            target.parent.mkdir(parents=True)
            target.write_text("<odoo/>", encoding="utf-8")
            found = list(find_owl_template_files(tmpdir))
        self.assertEqual(found, [])

    def test_ignores_a_claude_worktree_directory(self):
        """Real, live bug found reviewing this checker: the dirs-pruning tuple had no generic
        dot-directory exclusion. This project's own standing convention runs concurrent bug-hunt
        dispatches in isolated git worktrees under .claude/worktrees/<session>/ INSIDE the repo
        root -- confirmed live: running this exact function against the real hams_com repo during
        this review found 40 of 60 template files were duplicates from two other concurrently
        active sessions' own worktrees, meaning this checker (which compiles every template via a
        real headless Chrome) was spending roughly two-thirds of its real compile work re-checking
        other sessions' own unrelated copies."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = (
                Path(tmpdir) / ".claude" / "worktrees" / "agent-other-session" / "some_module"
                / "static" / "src" / "xml" / "foo.xml"
            )
            target.parent.mkdir(parents=True)
            target.write_text("<templates/>", encoding="utf-8")
            found = list(find_owl_template_files(tmpdir))
        self.assertEqual(found, [])

    def test_ignores_non_xml_files_under_static_src_xml(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "some_module" / "static" / "src" / "xml" / "readme.md"
            target.parent.mkdir(parents=True)
            target.write_text("not xml", encoding="utf-8")
            found = list(find_owl_template_files(tmpdir))
        self.assertEqual(found, [])


def _run_checker(tmpdir):
    result = subprocess.run(
        [sys.executable, _SCRIPT, str(tmpdir)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.returncode, result.stdout


@unittest.skipUnless(
    os.path.exists("/usr/lib/python3/dist-packages/odoo/addons/web/static/lib/owl/owl.js"),
    "Real Owl bundle not present -- not an Odoo dev box.",
)
class RealCompileCheckIntegrationTests(unittest.TestCase):
    def _write_template(self, tmpdir, name, t_out_expr):
        target = Path(tmpdir) / "some_module" / "static" / "src" / "xml" / "foo.xml"
        target.parent.mkdir(parents=True)
        target.write_text(
            f'<templates><t t-name="{name}"><div><t t-out="{t_out_expr}"/></div></t></templates>',
            encoding="utf-8",
        )

    def test_a_genuinely_valid_template_passes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_template(tmpdir, "some_module.Valid", "state.value")
            returncode, stdout = _run_checker(tmpdir)
        self.assertEqual(returncode, 0, stdout)
        self.assertIn("SUCCESS", stdout)

    def test_the_real_numeric_separator_bug_is_caught(self):
        # The exact real bug found and fixed in ham_shack.WebShackTemplate, 2026-09-07.
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_template(tmpdir, "some_module.Broken", "(state.value / 1_000_000).toFixed(6)")
            returncode, stdout = _run_checker(tmpdir)
        self.assertEqual(returncode, 1, stdout)
        self.assertIn("some_module.Broken", stdout)
        self.assertIn("Invalid or unexpected token", stdout)

    def test_a_directory_with_no_templates_succeeds_with_nothing_to_check(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            returncode, stdout = _run_checker(tmpdir)
        self.assertEqual(returncode, 0, stdout)
        self.assertIn("nothing to check", stdout)


if __name__ == "__main__":
    unittest.main()
