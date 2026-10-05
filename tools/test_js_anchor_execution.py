#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for js_anchor_execution.py. Real node/acorn scans of real temporary JS files, as in
test_check_js_function_test_anchors.py; the coverage document is built in the shape
run_js_coverage.py prints."""

import os
import shutil
import tempfile
import unittest

import js_anchor_execution as jae  # noqa: E402

SOURCE = """\
// [@ANCHOR: top_level_note]

// [@ANCHOR: ran_function]
function ranFunction() {
    return 1;
}

// [@ANCHOR: idle_function]
function idleFunction() {
    return 2;
}

class Widget {
    // [@ANCHOR: widget_method]
    render() {
        return 3;
    }

    // [@ANCHOR: widget_unseen]
    unseen() {
        return 4;
    }

    // Tests [@ANCHOR: only_a_citation]
    other() {
        return 5;
    }
}
"""


def _coverage(functions_by_file):
    return {
        "files": {},
        "functions": {
            rel: {
                "functions": {n: {"executed": e, "total": 1} for n, e in names.items()},
                "anonymous": {"executed": 0, "total": 0},
            }
            for rel, names in functions_by_file.items()
        },
        "unresolved": [],
    }


class JsAnchorExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        with open(os.path.join(self.tmp, "widget.js"), "w", encoding="utf-8") as f:
            f.write(SOURCE)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _report(self, functions_by_file):
        return jae.build_report(_coverage(functions_by_file), self.tmp, scanned_files=["widget.js"])

    def _anchors(self, report, key):
        return {e["anchor"] for e in report[key]}

    def test_executed_not_executed_and_unattributed_are_told_apart(self):
        report = self._report({"widget.js": {"ranFunction": 3, "idleFunction": 0, "render": 2}})
        self.assertEqual(self._anchors(report, "executed"), {"ran_function", "widget_method"})
        self.assertEqual(self._anchors(report, "not_executed"), {"idle_function"})
        reasons = {e["anchor"]: e["reason"] for e in report["unattributed"]}
        self.assertIn("outside any named function", reasons["top_level_note"])
        self.assertIn("no function of this name", reasons["widget_unseen"])

    def test_a_citation_is_not_an_anchor_declaration(self):
        report = self._report({"widget.js": {}})
        everything = {e["anchor"] for key in report for e in report[key]}
        self.assertNotIn("only_a_citation", everything)

    def test_a_file_missing_from_the_coverage_is_reported_as_such(self):
        report = self._report({})
        self.assertEqual(report["executed"], [])
        function_entries = [e for e in report["unattributed"] if "function" in e]
        self.assertEqual(len(function_entries), 4)
        self.assertTrue(all("not in the coverage data" in e["reason"] for e in function_entries))

    def test_class_methods_are_looked_up_by_bare_name(self):
        report = self._report({"widget.js": {"render": 0, "unseen": 1}})
        self.assertEqual(self._anchors(report, "not_executed"), {"widget_method"})
        self.assertIn("widget_unseen", self._anchors(report, "executed"))

    def test_two_functions_sharing_a_name_are_flagged_ambiguous(self):
        with open(os.path.join(self.tmp, "dup.js"), "w", encoding="utf-8") as f:
            f.write(
                "class A {\n    // [@ANCHOR: a_go]\n    go() {}\n}\n"
                "class B {\n    // [@ANCHOR: b_go]\n    go() {}\n}\n"
            )
        report = jae.build_report(_coverage({"dup.js": {"go": 1}}), self.tmp, scanned_files=["dup.js"])
        self.assertEqual(len(report["executed"]), 2)
        self.assertTrue(all(e.get("ambiguous_name") for e in report["executed"]))


if __name__ == "__main__":
    unittest.main()
