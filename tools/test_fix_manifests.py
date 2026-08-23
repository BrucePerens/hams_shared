#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for fix_manifests.py.

This is a one-off migration script, not a reusable module: its bottom
half is bare top-level code (`for root, dirs, files in os.walk('.'): ...`)
that runs immediately, with real writes, the instant the module is
imported -- there is no `if __name__ == "__main__":` guard. `import
fix_manifests` in a test would walk whatever the test process's cwd
happens to be and start rewriting real __manifest__.py files in place.

To test fix_manifest(path) without ever triggering that, this test
extracts just the function definition's exact source text by regex
(never the trailing os.walk loop) and exec()'s it in isolation -- the
same technique test_run_linters.py uses for run_linters.py's
step-27 discovery snippet, for the same reason: real source, no
side-effecting module-level code along for the ride.
"""

import os
import re
import shutil
import tempfile
import unittest

_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fix_manifests.py")


def _extract_fix_manifest():
    with open(_SCRIPT, encoding="utf-8") as f:
        source = f.read()
    match = re.search(r"(def fix_manifest\(path\):.*?\n)\nfor root", source, re.DOTALL)
    if not match:
        raise AssertionError(
            "Could not locate fix_manifest()'s source in fix_manifests.py -- "
            "its shape changed; update this test's extraction regex."
        )
    namespace = {"os": os}
    exec(match.group(1), namespace)  # real source, not user input
    return namespace["fix_manifest"]


def _write(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


class FixManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fix_manifest = _extract_fix_manifest()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _path(self, content):
        p = os.path.join(self.tmp, "__manifest__.py")
        _write(p, content)
        return p

    def test_documentation_html_entries_are_removed_from_a_double_quoted_data_array(self):
        p = self._path(
            '{\n'
            '    "name": "Foo",\n'
            '    "data": [\n'
            '        "data/a.xml",\n'
            '        "data/documentation.html",\n'
            '        "data/testing_documentation.html",\n'
            '        "data/b.xml",\n'
            '    ],\n'
            '}\n'
        )
        self.fix_manifest(p)
        with open(p, encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn("documentation.html", content)
        self.assertIn("data/a.xml", content)
        self.assertIn("data/b.xml", content)

    def test_single_quoted_style_is_also_matched(self):
        p = self._path(
            "{\n"
            "    'data': [\n"
            "        'data/documentation.html',\n"
            "        'data/x.xml',\n"
            "    ],\n"
            "}\n"
        )
        self.fix_manifest(p)
        with open(p, encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn("documentation.html", content)
        self.assertIn("data/x.xml", content)

    def test_a_manifest_with_no_documentation_html_entries_is_left_byte_identical_and_unwritten(self):
        original = '{\n    "data": [\n        "data/x.xml",\n    ],\n}\n'
        p = self._path(original)
        before_mtime = os.path.getmtime(p)
        self.fix_manifest(p)
        with open(p, encoding="utf-8") as f:
            content = f.read()
        self.assertEqual(content, original)
        self.assertEqual(os.path.getmtime(p), before_mtime)

    def test_both_documentation_html_and_testing_documentation_html_are_removed_independently(self):
        p = self._path(
            '{\n'
            '    "data": [\n'
            '        "data/testing_documentation.html",\n'
            '        "data/x.xml",\n'
            '    ],\n'
            '}\n'
        )
        self.fix_manifest(p)
        with open(p, encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn("testing_documentation.html", content)
        self.assertIn("data/x.xml", content)

    def test_a_documentation_html_reference_outside_any_data_array_is_left_alone(self):
        p = self._path(
            '{\n'
            '    "name": "See data/documentation.html for background",\n'
            '    "data": [\n'
            '        "data/x.xml",\n'
            '    ],\n'
            '}\n'
        )
        self.fix_manifest(p)
        with open(p, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("data/documentation.html", content)

    def test_a_data_array_whose_closing_bracket_shares_a_line_with_a_quoted_entry_leaves_the_scanner_stuck_open(self):
        # Real, verified limitation of the script's own "rudimentary end of
        # array detection" (its comment's words): the elif that detects the
        # closing "]" requires the line to contain NO quote characters, so
        # a manifest whose array closes as `"data/x.xml"],` on one line
        # never flips in_data_array back to False. A later, wholly
        # unrelated line that happens to contain the literal string
        # "data/documentation.html" -- even outside any data array, e.g. in
        # a comment or an unrelated dict value -- is then silently deleted
        # too. Confirmed empirically before writing this assertion. This
        # script is dead one-off migration tooling (unreferenced anywhere
        # in the repo or in run_linters.py, already run once historically),
        # so this test documents the real bug rather than fixing it.
        p = self._path(
            '{\n'
            '    "data": [\n'
            '        "data/x.xml"],\n'
            '    # see also data/documentation.html for reference\n'
            '    "other_key": "data/documentation.html",\n'
            '}\n'
        )
        self.fix_manifest(p)
        with open(p, encoding="utf-8") as f:
            content = f.read()
        # The comment line survives: it has no quotes around
        # "data/documentation.html", so it never matches the deletion
        # pattern in the first place. Only the quoted "other_key" line,
        # legitimately outside the data array but caught anyway because
        # in_data_array never flipped back off, is wrongly stripped.
        self.assertIn("see also data/documentation.html", content)
        self.assertNotIn("other_key", content)


if __name__ == "__main__":
    unittest.main()
