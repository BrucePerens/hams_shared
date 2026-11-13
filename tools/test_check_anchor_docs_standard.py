#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for check_anchor_docs_standard.py, the opt-in gate of night_shift_todo/low/document-every-anchor-completely
(ADR 0113).

Tests [@ANCHOR: anchor_docs_standard:read_list]
Tests [@ANCHOR: anchor_docs_standard:doc_findings]
Tests [@ANCHOR: anchor_docs_standard:source_findings]
Tests [@ANCHOR: anchor_docs_standard:find_findings]
Tests [@ANCHOR: anchor_docs_standard:main]
"""

import contextlib
import io
import os
import tempfile
import unittest

import check_anchor_docs_standard as gate

LONG = " ".join(["word"] * 60)


def tag(name):
    """An anchor tag built at run time, so this file does not itself define anchors."""
    return "[@" + "ANCHOR: " + name + "]"


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


class ListTests(unittest.TestCase):
    def test_comments_blank_lines_and_trailing_slashes_are_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write(tmp, "list.txt", "# header\n\na/b.md  # why\nsrc/dir/\n")
            self.assertEqual(gate.read_list(path), ["a/b.md", "src/dir"])
            self.assertEqual(gate.read_list(os.path.join(tmp, "missing.txt")), [])

    def test_the_shipped_list_is_not_empty(self):
        self.assertTrue(gate.read_list())


class FindingTests(unittest.TestCase):
    def test_a_stub_entry_and_an_unanchored_structure_are_findings_and_a_full_entry_is_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            readme = "### `%s` `full()`\n\n%s\n\n### `%s` `stub()`\n\nOne line.\n" % (tag("m:full"), LONG, tag("m:stub"))
            write(tmp, "mod/README.md", readme)
            write(tmp, "mod/a.rs", "// %s\nstruct Good;\nstruct Bare;\n" % tag("m:Good"))
            write(tmp, "other/b.py", "class NotListed:\n    pass\n")
            findings, checked = gate.find_findings(tmp, ["mod/README.md", "mod", "absent/dir"])
            self.assertEqual(checked, 2)
            self.assertEqual(len(findings), 2, findings)
            self.assertTrue(any("README.md" in f and "m:stub" in f for f in findings))
            self.assertTrue(any("a.rs:3 struct Bare" in f for f in findings))
            other = gate.find_findings(tmp, ["other"])[0]
            self.assertEqual(len(other), 1)
            self.assertIn("other/b.py:1 class NotListed has no anchor", other[0])

    def test_a_table_with_a_header_and_no_rows_is_a_finding_and_a_filled_table_is_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = "### `%s`\n\n%s\n\n| Field | Meaning |\n|---|---|\n" % (tag("m:empty"), LONG)
            filled = "### `%s`\n\n%s\n\n| Field | Meaning |\n|---|---|\n| `a` | one |\n" % (tag("m:filled"), LONG)
            write(tmp, "mod/EMPTY.md", empty)
            write(tmp, "mod/FILLED.md", filled)
            findings, _ = gate.find_findings(tmp, ["mod/EMPTY.md", "mod/FILLED.md"])
            self.assertEqual(len(findings), 1, findings)
            self.assertIn("EMPTY.md:1 entry for m:empty has a table with a header and no rows", findings[0])

    def test_a_directory_does_not_check_the_markdown_inside_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "mod/README.md", "* %s stub\n" % tag("m:short"))
            self.assertEqual(gate.find_findings(tmp, ["mod"]), ([], 1))


class MainTests(unittest.TestCase):
    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = gate.main(argv)
        return code, out.getvalue() + err.getvalue()

    def test_usage_error_is_exit_2(self):
        self.assertEqual(self.run_main([])[0], 2)

    def test_a_directory_with_none_of_the_listed_paths_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "x.txt", "not listed")
            with self.assertRaises(SystemExit) as refused:
                self.run_main([tmp])
            self.assertEqual(refused.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
