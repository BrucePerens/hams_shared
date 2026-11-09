#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for dispatch_ok.py. Every test writes its own stats file in a temporary directory.

Tests [@ANCHOR: dispatch_ok:evaluate]
Tests [@ANCHOR: dispatch_ok:stats_format]
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dispatch_ok  # noqa: E402

NOW = 1_800_000_000.0


class EvaluateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "stats.json")

    def write(self, **fields):
        document = {"written_epoch": NOW - 5, "dispatch": {"state": "OK", "reasons": ["short queues"]}}
        document.update(fields)
        with open(self.path, "w") as handle:
            json.dump(document, handle)

    def evaluate(self, **kwargs):
        return dispatch_ok.evaluate(self.path, now=lambda: NOW, **kwargs)

    def test_ok_when_fresh_and_ok(self):
        self.write()
        self.assertEqual(self.evaluate(), ("OK", ["short queues"]))

    def test_hold_with_reasons(self):
        self.write(dispatch={"state": "HOLD", "reasons": ["oldest lint wait 12 min is over 10 min"]})
        state, lines = self.evaluate()
        self.assertEqual(state, "HOLD")
        self.assertIn("lint wait", lines[0])

    def test_stale_file_is_hold(self):
        self.write(written_epoch=NOW - 61)
        state, lines = self.evaluate()
        self.assertEqual(state, "HOLD")
        self.assertIn("old", lines[0])
        self.assertEqual(self.evaluate(max_age=120)[0], "OK")

    def test_missing_and_broken_files_are_hold(self):
        self.assertEqual(self.evaluate()[0], "HOLD")
        with open(self.path, "w") as handle:
            handle.write("{not json")
        self.assertEqual(self.evaluate()[0], "HOLD")
        with open(self.path, "w") as handle:
            handle.write("[1]")
        self.assertEqual(self.evaluate()[0], "HOLD")

    def test_non_finite_timestamp_is_hold(self):
        with open(self.path, "w") as handle:
            handle.write('{"written_epoch": NaN, "dispatch": {"state": "OK"}}')
        self.assertEqual(self.evaluate()[0], "HOLD")

    def test_missing_verdict_or_timestamp_is_hold(self):
        self.write(dispatch={"state": "MAYBE"})
        self.assertEqual(self.evaluate()[0], "HOLD")
        with open(self.path, "w") as handle:
            json.dump({"dispatch": {"state": "OK"}}, handle)
        self.assertEqual(self.evaluate()[0], "HOLD")

    def test_free_overflow_host_is_mentioned_but_does_not_change_the_verdict(self):
        self.write(dispatch={"state": "HOLD", "reasons": ["busy"]},
                   remote_capacity={"hosts": [{"name": "bruce17", "state": "free"}, {"name": "mac1-vm", "state": "busy"},
                                              {"name": "bruce16", "state": "reserved"}]})
        state, lines = self.evaluate()
        self.assertEqual(state, "HOLD")
        self.assertIn("bruce17", lines[-1])
        self.assertNotIn("mac1-vm", lines[-1])
        self.assertNotIn("bruce16", lines[-1])

    def test_main_exit_codes_and_output(self):
        self.write(written_epoch=__import__("time").time())
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(dispatch_ok.main(["--file", self.path]), 0)
        self.assertTrue(buffer.getvalue().startswith("OK"))
        self.write(written_epoch=__import__("time").time(), dispatch={"state": "HOLD", "reasons": ["x"]})
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(dispatch_ok.main(["--file", self.path, "--quiet"]), 1)
        self.assertEqual(buffer.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
