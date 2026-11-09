#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Tests for check_no_rule_lost.py: every unit of the old text must be present verbatim (whitespace collapsed) in the new files.

Tests [@ANCHOR: check_no_rule_lost:normalize]
Tests [@ANCHOR: check_no_rule_lost:units]
Tests [@ANCHOR: check_no_rule_lost:missing_units]
Tests [@ANCHOR: check_no_rule_lost:moved_verbatim]
Tests [@ANCHOR: check_no_rule_lost:read_source]
Tests [@ANCHOR: check_no_rule_lost:main]
"""
import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout

import check_no_rule_lost as checker

OLD = """# Heading

Intro paragraph that
spans two lines.

- **Rule one**: never do X;
  continuation of rule one.
- Rule two.
  ```bash
  sudo -u odoo env A=1 \\
    python3 test.py
  ```
  NOT the other form.

<site_rules>
1. Numbered rule.
</site_rules>
"""


class UnitTests(unittest.TestCase):
    def test_normalize_collapses_whitespace(self):
        self.assertEqual(checker.normalize("  a\n   b\tc  "), "a b c")

    def test_units_split_headings_paragraphs_items_and_fences(self):
        got = checker.units(OLD)
        self.assertEqual(got[0], "# Heading")
        self.assertEqual(got[1], "Intro paragraph that spans two lines.")
        self.assertEqual(got[2], "- **Rule one**: never do X; continuation of rule one.")
        self.assertEqual(got[3], "- Rule two.")
        self.assertEqual(got[4], "```bash sudo -u odoo env A=1 \\ python3 test.py ```")
        self.assertEqual(got[5], "NOT the other form.")
        self.assertEqual(got[6:], ["<site_rules>", "1. Numbered rule.", "</site_rules>"])

    def test_a_verbatim_move_with_different_wrapping_loses_nothing(self):
        kept = "# Heading\n\nIntro paragraph that spans two lines.\n\nPointer: see the doc.\n"
        moved = OLD.split("\n\n", 2)[2].replace("never do X;\n  continuation", "never do X; continuation")
        self.assertEqual(checker.missing_units([OLD], [kept, moved]), [])
        self.assertTrue(checker.moved_verbatim(OLD, [kept, moved]))

    def test_a_condensed_or_dropped_unit_is_reported(self):
        new = OLD.replace("never do X;\n  continuation of rule one.", "never do X.").replace("1. Numbered rule.\n", "")
        missing = checker.missing_units([OLD], [new])
        self.assertEqual(missing, ["- **Rule one**: never do X; continuation of rule one.", "1. Numbered rule."])
        self.assertFalse(checker.moved_verbatim(OLD, [new]))
        self.assertEqual(checker.missing_sentences(missing[0], [new]), ["never do X;", "continuation of rule one."])

    def test_a_unit_split_across_two_files_is_missing(self):
        para = "First sentence of a rule. Second sentence of a rule."
        self.assertEqual(checker.missing_units([para], ["First sentence of a rule.", "Second sentence of a rule."]), [para])


class MainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def path(self, name, text):
        full = os.path.join(self.tmp.name, name)
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(text)
        return full

    def run_main(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = checker.main(list(argv))
        return code, out.getvalue()

    def test_main_ok_fail_and_measure(self):
        old = self.path("old.md", OLD)
        same = self.path("same.md", OLD)
        cut = self.path("cut.md", OLD.replace("- Rule two.\n", ""))
        code, out = self.run_main("--before", old, "--after", same)
        self.assertEqual(code, 0, out)
        self.assertIn("OK, all 9 units", out)
        code, out = self.run_main("--before", old, "--after", cut)
        self.assertEqual(code, 1, out)
        self.assertIn("unit: - Rule two.", out)
        code, out = self.run_main("--measure", old)
        self.assertEqual(code, 0)
        self.assertIn("~%6d tokens (chars/4)" % round(len(OLD) / 4), out)

    def test_read_source_reads_git_revisions(self):
        repo = self.tmp.name
        for args in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "T"]):
            subprocess.run(["git", *args], cwd=repo, check=True)
        self.path("f.md", "committed text\n")
        subprocess.run(["git", "add", "f.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "x"], cwd=repo, check=True)
        self.path("f.md", "changed\n")
        old_cwd = os.getcwd()
        os.chdir(repo)
        try:
            self.assertEqual(checker.read_source("HEAD:f.md"), "committed text\n")
            self.assertEqual(checker.read_source("f.md"), "changed\n")
        finally:
            os.chdir(old_cwd)


if __name__ == "__main__":
    unittest.main()
