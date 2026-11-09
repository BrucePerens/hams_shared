#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for dispatch_ok.py. Every test writes its own stats file in a temporary directory.

Tests [@ANCHOR: dispatch_ok:evaluate]
Tests [@ANCHOR: dispatch_ok:stats_format]
Tests [@ANCHOR: dispatch_ok:landing_limits]
Tests [@ANCHOR: dispatch_ok:landing_verdict]
Tests [@ANCHOR: dispatch_ok:landing]
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

    def test_a_stamp_in_the_future_is_hold(self):
        self.write(written_epoch=NOW + 3600)
        self.assertEqual(self.evaluate()[0], "HOLD")
        self.write(written_epoch=NOW + 1)
        self.assertEqual(self.evaluate()[0], "OK")

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


def entry(state, age_minutes=1, slug="BrucePerens/hams_com"):
    return {"repo": "/srv/checkouts/" + slug.split("/")[1], "slug": slug, "pr": 1, "state": state, "enqueued": NOW - age_minutes * 60}


class LandingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.queue = os.path.join(self.tmp.name, "queue.json")
        self.stats = os.path.join(self.tmp.name, "stats.json")

    def verdict(self, entries):
        return dispatch_ok.landing_verdict(entries, now=lambda: NOW)

    def write_queue(self, entries):
        with open(self.queue, "w") as handle:
            json.dump({"version": 1, "entries": entries}, handle)

    def test_empty_and_short_queue_is_ok(self):
        self.assertEqual(self.verdict([])[0], "LANDING_OK")
        state, reasons, summary = self.verdict([entry("queued", 5), entry("testing", 9, "BrucePerens/hams_open"), entry("merged", 500)])
        self.assertEqual((state, reasons), ("LANDING_OK", []))
        self.assertIn("1 queued, 1 testing", summary)
        self.assertIn("hams_com 1q/0t", summary)
        self.assertIn("hams_open 0q/1t", summary)

    def test_depth_limit_is_more_than_25(self):
        self.assertEqual(self.verdict([entry("queued")] * 20 + [entry("testing")] * 5)[0], "LANDING_OK")
        state, reasons, _ = self.verdict([entry("queued")] * 20 + [entry("testing")] * 6)
        self.assertEqual(state, "LANDING_HOLD")
        self.assertIn("26", reasons[0])

    def test_oldest_queued_wait_limit_is_over_60_minutes(self):
        self.assertEqual(self.verdict([entry("queued", 60)])[0], "LANDING_OK")
        state, reasons, _ = self.verdict([entry("queued", 61)])
        self.assertEqual(state, "LANDING_HOLD")
        self.assertIn("61 min", reasons[0])

    def test_old_testing_or_finished_entries_do_not_trip_the_age_limit(self):
        self.assertEqual(self.verdict([entry("testing", 300), entry("failed", 900), entry("merged", 900)])[0], "LANDING_OK")

    def test_malformed_entries_are_ignored(self):
        self.assertEqual(self.verdict(["x", None, {"state": "queued"}, {"state": "queued", "enqueued": "no"}])[0], "LANDING_OK")

    def test_landing_reads_the_file(self):
        self.assertEqual(dispatch_ok.landing(os.path.join(self.tmp.name, "absent.json"), now=lambda: NOW)[0], "LANDING_OK")
        self.write_queue([entry("queued", 90)])
        self.assertEqual(dispatch_ok.landing(self.queue, now=lambda: NOW)[0], "LANDING_HOLD")

    def test_unreadable_or_malformed_queue_file_is_hold(self):
        with open(self.queue, "w") as handle:
            handle.write("{nope")
        self.assertEqual(dispatch_ok.landing(self.queue)[0], "LANDING_HOLD")
        with open(self.queue, "w") as handle:
            json.dump({"entries": "x"}, handle)
        self.assertEqual(dispatch_ok.landing(self.queue)[0], "LANDING_HOLD")

    def run_main(self, *args):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = dispatch_ok.main(["--file", self.stats, "--queue-file", self.queue, *args])
        return code, buffer.getvalue()

    def test_landing_hold_does_not_change_the_agent_start_exit_status(self):
        with open(self.stats, "w") as handle:
            json.dump({"written_epoch": __import__("time").time(), "dispatch": {"state": "OK", "reasons": ["ok"]}}, handle)
        self.write_queue([dict(entry("queued"), enqueued=0)])
        code, text = self.run_main()
        self.assertEqual(code, 0)
        lines = text.splitlines()
        self.assertEqual(lines[0], "OK")
        self.assertIn("LANDING_HOLD", lines)
        self.assertNotIn("\nHOLD\n", text)
        code, _ = self.run_main("--landing")
        self.assertEqual(code, dispatch_ok.EXIT_LANDING_HOLD)
        self.assertEqual(self.run_main("--quiet", "--landing")[1], "")

    def test_agent_hold_does_not_change_the_landing_exit_status(self):
        with open(self.stats, "w") as handle:
            json.dump({"written_epoch": __import__("time").time(), "dispatch": {"state": "HOLD", "reasons": ["memory"]}}, handle)
        self.write_queue([])
        self.assertEqual(self.run_main()[0], 1)
        code, text = self.run_main("--landing")
        self.assertEqual(code, 0)
        self.assertIn("LANDING_OK", text.splitlines())


if __name__ == "__main__":
    unittest.main()
