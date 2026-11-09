#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for anchor_doc_inventory.py, the measurement behind night_shift_todo/low/document-every-anchor-completely
(ADR 0113). They build small documentation and source trees in a temporary directory.

Tests [@ANCHOR: anchor_doc_inventory:entry_shape]
Tests [@ANCHOR: anchor_doc_inventory:markdown_entries]
Tests [@ANCHOR: anchor_doc_inventory:scan_docs]
Tests [@ANCHOR: anchor_doc_inventory:find_data_structures]
Tests [@ANCHOR: anchor_doc_inventory:scan_structures]
Tests [@ANCHOR: anchor_doc_inventory:main]
"""

import contextlib
import io
import os
import tempfile
import unittest

import anchor_doc_inventory as inv


def tag(name):
    """An anchor tag, built so that this file's source holds no literal one."""
    return "[@ANCH" + "OR: %s]" % name


def write(root, relative, text):
    path = os.path.join(root, relative)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


FULL_ENTRY = """## `m:full` %s

Purpose: what it is for, why it exists, who calls it and when, written in plain sentences for a human reader, with enough detail that nobody has to open the code to learn how to call it correctly.

| Argument | Type | Meaning |
|---|---|---|
| `x` | int | the thing |

**Errors:** none.
**Concurrency:** none.
""" % tag("m:full")


class EntryShapeTests(unittest.TestCase):
    def test_words_exclude_anchor_tags_and_tables_and_labels_are_seen(self):
        shape = inv.entry_shape(FULL_ENTRY)
        self.assertTrue(shape["table"])
        self.assertEqual(shape["labelled"], 2)
        self.assertNotIn("ANCHOR", str(shape))
        self.assertGreater(shape["words"], 25)
        self.assertEqual(inv.entry_shape("* one two three %s" % tag("m:a"))["words"], 3)  # the bullet mark is not a word
        self.assertEqual(inv.entry_shape("")["words"], 0)


class DocEntryTests(unittest.TestCase):
    def test_heading_entries_run_to_the_next_heading_of_the_same_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write(tmp, "README.md", FULL_ENTRY + "### sub heading no anchor\nmore words here\n## next %s\nshort\n" % tag("m:next"))
            entries = inv.doc_entries(path)
            self.assertEqual([e["anchors"] for e in entries], [["m:full"], ["m:next"]])
            self.assertEqual(entries[0]["line"], 1)
            self.assertTrue(entries[0]["shape"]["table"])
            self.assertIn("more words", "more words here")  # the sub-heading text belongs to the first entry:
            self.assertGreater(entries[0]["shape"]["words"], inv.entry_shape(FULL_ENTRY)["words"])
            self.assertFalse(entries[1]["shape"]["table"])

    def test_a_bullet_paragraph_with_several_anchors_is_one_shared_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write(tmp, "README.md", "* The tool reads a value. %s %s\n\n* Another. %s\n" % (tag("m:a"), tag("m:b"), tag("m:c")))
            entries = inv.doc_entries(path)
            self.assertEqual([e["anchors"] for e in entries], [["m:a", "m:b"], ["m:c"]])

    def test_an_unreadable_or_missing_file_has_no_entries(self):
        self.assertEqual(inv.doc_entries("/nonexistent/README.md"), [])
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "README.md")
            with open(path, "wb") as handle:
                handle.write(b"\xff\xfe\x00bad")
            self.assertEqual(inv.doc_entries(path), [])


class ScanDocsTests(unittest.TestCase):
    def test_counts_short_entries_and_orders_the_relay_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "aaa/README.md", "* One line. %s\n" % tag("a:one"))
            write(tmp, "daemons/hams_local_relay/README.md", FULL_ENTRY + "\n## `r:short` %s\nShort.\n" % tag("r:short"))
            write(tmp, "docs/story.md", "# Story\n* x %s\n" % tag("s:x"))
            write(tmp, "notes/other.md", "* not read, not a README or docs file %s\n" % tag("n:x"))
            write(tmp, "target/README.md", "* pruned %s\n" % tag("t:x"))
            write(tmp, "empty/README.md", "no anchors here\n")
            rows = inv.scan_docs(tmp)
            self.assertEqual([r["path"] for r in rows],
                             ["daemons/hams_local_relay/README.md", "aaa/README.md", "docs/story.md"])
            relay = rows[0]
            self.assertEqual((relay["entries"], relay["anchors"], relay["short"], relay["tables"]), (2, 2, 1, 1))
            self.assertEqual(rows[1]["short"], 1)
            self.assertEqual(inv.scan_docs(tmp, short_words=1)[1]["short"], 0)


class DataStructureTests(unittest.TestCase):
    def test_rust_python_and_javascript_definitions_without_an_anchor_are_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            rust = write(tmp, "src/a.rs", "\n".join([
                "// %s" % tag("m:Anchored"), "#[derive(Debug)]", "pub struct Anchored { x: u8 }",
                "pub(crate) enum Bare { A, B }", "type Alias = u8;", "fn not_a_type() {}",
                "#[cfg(test)]", "mod tests { struct InTests; }"]))
            self.assertEqual(inv.find_data_structures(rust), [(4, "enum", "Bare"), (5, "type", "Alias")])
            py = write(tmp, "m/models.py", "class Bare:\n    pass\n\n# %s\nclass Fine:\n    pass\n" % tag("m:Fine"))
            self.assertEqual(inv.find_data_structures(py), [(1, "class", "Bare")])
            js = write(tmp, "static/x.js", "export class Widget {}\nclass Other {}\nfunction f() {}\n")
            self.assertEqual(inv.find_data_structures(js), [(1, "class", "Widget"), (2, "class", "Other")])
            self.assertEqual(inv.find_data_structures(write(tmp, "static/x.txt", "class X")), [])
            self.assertEqual(inv.find_data_structures(write(tmp, "tests/test_x.py", "class T:\n    pass\n")), [])
            self.assertEqual(inv.find_data_structures("/nonexistent/a.rs"), [])

    def test_scan_structures_walks_the_tree_and_prunes_build_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "src/a.rs", "struct Bare;\n")
            write(tmp, "target/debug/b.rs", "struct Built;\n")
            write(tmp, "node_modules/p/i.js", "class Vendor {}\n")
            write(tmp, "ok/c.py", "# %s\nclass Fine:\n    pass\n" % tag("m:Fine"))
            self.assertEqual(inv.scan_structures(tmp), {os.path.join("src", "a.rs"): [(1, "struct", "Bare")]})

    def test_a_test_named_directory_above_the_root_does_not_hide_the_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "tests/repo/src/a.rs", "struct Bare;\n")
            write(tmp, "tests/repo/tests/b.rs", "struct InTests;\n")
            self.assertEqual(
                inv.scan_structures(os.path.join(tmp, "tests", "repo")), {os.path.join("src", "a.rs"): [(1, "struct", "Bare")]}
            )


class MainTests(unittest.TestCase):
    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = inv.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_prints_the_table_and_the_structures_and_rejects_a_missing_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(tmp, "README.md", "* One line. %s\n" % tag("a:one"))
            write(tmp, "src/a.rs", "struct Bare;\n")
            code, out, _ = self.run_main(tmp, "--structures")
            self.assertEqual(code, 0)
            self.assertIn("1 documentation files, 1 anchors, 1 under 40 words", out)
            self.assertIn("data structures without an anchor: 1 in 1 files", out)
            self.assertIn("struct Bare:1", out)
            self.assertNotIn("data structures", self.run_main(tmp)[1])
        code, _, err = self.run_main("/nonexistent-root")
        self.assertEqual(code, 2)
        self.assertIn("not a directory", err)


if __name__ == "__main__":
    unittest.main()
