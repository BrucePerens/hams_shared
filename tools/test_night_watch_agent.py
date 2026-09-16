#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for night_watch_agent.py."""

import contextlib
import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import night_watch_agent as nwa


def _at(hour, minute=0):
    return datetime.datetime(2026, 9, 16, hour, minute, tzinfo=datetime.timezone.utc)


class TestNightWatchAgent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "sub", "agent.json")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_no_state_file_means_no_agent(self):
        self.assertEqual(nwa.describe(nwa.read_state(self.path)), {"agent": None})

    def test_claim_records_the_session_and_marks_it_working(self):
        rc, state = nwa.claim(self.path, "workspace-4a", "e5ef45", now=_at(10))
        self.assertEqual(rc, 0)
        self.assertEqual(nwa.read_state(self.path), state)
        self.assertEqual(state["state"], "working")
        self.assertEqual(state["last_active"], "2026-09-16T10:00:00Z")

    def test_a_second_session_cannot_claim_without_force(self):
        nwa.claim(self.path, "workspace-4a", "e5ef45", now=_at(10))
        rc, current = nwa.claim(self.path, "workspace-77", "abc123", now=_at(11))
        self.assertEqual(rc, nwa.EXIT_CLAIM_REFUSED)
        self.assertEqual(current["ref"], "e5ef45")
        self.assertEqual(nwa.read_state(self.path)["ref"], "e5ef45")

    def test_force_takes_over_from_a_dead_agent(self):
        nwa.claim(self.path, "workspace-4a", "e5ef45", now=_at(10))
        rc, state = nwa.claim(self.path, "workspace-77", "abc123", force=True, now=_at(11))
        self.assertEqual(rc, 0)
        self.assertEqual(nwa.read_state(self.path)["ref"], "abc123")
        self.assertEqual(state["claimed_at"], "2026-09-16T11:00:00Z")

    def test_a_reused_name_with_a_different_ref_is_a_different_agent(self):
        # Session names are reused over time; only the ref identifies a session.
        nwa.claim(self.path, "workspace-4a", "e5ef45", now=_at(10))
        rc, _current = nwa.claim(self.path, "workspace-4a", "999999", now=_at(11))
        self.assertEqual(rc, nwa.EXIT_CLAIM_REFUSED)

    def test_reclaiming_by_the_same_session_keeps_its_original_claim_time(self):
        nwa.claim(self.path, "workspace-4a", "e5ef45", now=_at(10))
        rc, state = nwa.claim(self.path, "workspace-4a", "e5ef45", now=_at(12))
        self.assertEqual(rc, 0)
        self.assertEqual(state["claimed_at"], "2026-09-16T10:00:00Z")
        self.assertEqual(state["last_active"], "2026-09-16T12:00:00Z")

    def test_heartbeat_updates_state_and_time_for_the_agent(self):
        nwa.claim(self.path, "workspace-4a", "e5ef45", now=_at(10))
        rc, state = nwa.heartbeat(self.path, "e5ef45", "idle", now=_at(10, 40))
        self.assertEqual(rc, 0)
        self.assertEqual(state["state"], "idle")
        self.assertEqual(nwa.describe(state, now=_at(11, 45))["minutes_since_last_active"], 65)

    def test_heartbeat_from_a_session_that_was_replaced_is_refused(self):
        nwa.claim(self.path, "workspace-4a", "e5ef45", now=_at(10))
        nwa.claim(self.path, "workspace-77", "abc123", force=True, now=_at(11))
        rc, current = nwa.heartbeat(self.path, "e5ef45", "idle", now=_at(12))
        self.assertEqual(rc, nwa.EXIT_NOT_THE_AGENT)
        self.assertEqual(current["ref"], "abc123")
        self.assertEqual(nwa.read_state(self.path)["last_active"], "2026-09-16T11:00:00Z")

    def test_heartbeat_rejects_an_unknown_state(self):
        nwa.claim(self.path, "workspace-4a", "e5ef45", now=_at(10))
        with self.assertRaises(ValueError):
            nwa.heartbeat(self.path, "e5ef45", "asleep")

    def test_release_clears_only_for_the_agent(self):
        nwa.claim(self.path, "workspace-4a", "e5ef45", now=_at(10))
        self.assertEqual(nwa.release(self.path, "abc123")[0], nwa.EXIT_NOT_THE_AGENT)
        self.assertIsNotNone(nwa.read_state(self.path))
        self.assertEqual(nwa.release(self.path, "e5ef45")[0], 0)
        self.assertIsNone(nwa.read_state(self.path))

    def test_cli_round_trip_uses_the_environment_path(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"NIGHT_WATCH_STATE": self.path}), contextlib.redirect_stdout(out):
            self.assertEqual(nwa.main(["claim", "--name", "workspace-4a", "--ref", "e5ef45"]), 0)
            self.assertEqual(nwa.main(["claim", "--name", "workspace-9", "--ref", "zzz"]), nwa.EXIT_CLAIM_REFUSED)
            self.assertEqual(nwa.main(["heartbeat", "--ref", "e5ef45", "--state", "idle"]), 0)
        shown = io.StringIO()
        with mock.patch.dict(os.environ, {"NIGHT_WATCH_STATE": self.path}), contextlib.redirect_stdout(shown):
            self.assertEqual(nwa.main(["show"]), 0)
        report = json.loads(shown.getvalue())
        self.assertEqual(report["agent"]["ref"], "e5ef45")
        self.assertEqual(report["agent"]["state"], "idle")
        self.assertLessEqual(report["minutes_since_last_active"], 1)


if __name__ == "__main__":
    unittest.main()
