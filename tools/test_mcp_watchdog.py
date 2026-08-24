#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for mcp_watchdog.py's pure/near-pure logic: agent-liveness
detection, family-tree parsing, and persisted-state/IPC-queue bookkeeping.
The `@mcp.tool()`-decorated async coroutines (wait_for_*, spawn_*, etc.)
need a live FastMCP context and real file-watching to exercise meaningfully
and are out of scope here -- this covers the functions those tools are
built on, which is where a parsing bug would actually hide.
"""

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import mcp_watchdog as wd


def _write_transcript(path, lines):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write((json.dumps(line) if isinstance(line, dict) else line) + "\n")


class _SafePatchTestCase(unittest.TestCase):
    """Matches test_provision.py's own convention: a self.safe_patch()
    wrapper instead of a bare `with patch(...)` context manager or
    `@patch` decorator at each call site."""

    def safe_patch(self, target, *args, **kwargs):
        patcher = patch(target, *args, **kwargs)
        mock_obj = patcher.start()
        self.addCleanup(patcher.stop)
        return mock_obj


class GetStateFileTests(unittest.TestCase):
    def test_no_agent_id_uses_the_bare_filename(self):
        self.assertTrue(wd.get_state_file(None).endswith("watchdog_state.json"))
        self.assertTrue(wd.get_state_file("").endswith("watchdog_state.json"))

    def test_an_agent_id_is_appended_as_a_suffix(self):
        path = wd.get_state_file("agent-42")
        self.assertTrue(path.endswith("watchdog_state_agent-42.json"))


class PersistedStateRoundTripTests(_SafePatchTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.safe_patch("mcp_watchdog.os.path.expanduser", side_effect=lambda p: p.replace("~", self.tmp))

    def test_load_returns_none_when_no_state_file_exists_yet(self):
        self.assertIsNone(wd.load_persisted_states("agent-1"))

    def test_save_then_load_round_trips_the_real_data(self):
        states = {"agent-1": {"turns": 3, "stalled": False}}
        wd.save_persisted_states(states, "agent-1")
        self.assertEqual(wd.load_persisted_states("agent-1"), states)

    def test_different_agent_ids_persist_to_separate_files(self):
        wd.save_persisted_states({"a": 1}, "agent-1")
        wd.save_persisted_states({"b": 2}, "agent-2")
        self.assertEqual(wd.load_persisted_states("agent-1"), {"a": 1})
        self.assertEqual(wd.load_persisted_states("agent-2"), {"b": 2})

    def test_a_corrupt_state_file_is_treated_as_no_state_not_a_crash(self):
        state_file = wd.get_state_file("agent-1")
        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        with open(state_file, "w") as f:
            f.write("{not valid json")
        self.assertIsNone(wd.load_persisted_states("agent-1"))


class MtimeFsizeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_get_mtime_of_a_real_file_is_a_real_positive_number(self):
        path = os.path.join(self.tmp, "f.txt")
        with open(path, "w") as f:
            f.write("hi")
        self.assertGreater(wd.get_mtime(path), 0)

    def test_get_mtime_of_a_missing_file_is_zero_not_an_exception(self):
        self.assertEqual(wd.get_mtime(os.path.join(self.tmp, "nope.txt")), 0)

    def test_get_fsize_of_a_real_file_matches_its_real_size(self):
        path = os.path.join(self.tmp, "f.txt")
        with open(path, "w") as f:
            f.write("hello")
        self.assertEqual(wd.get_fsize(path), 5)

    def test_get_fsize_of_a_missing_file_is_zero_not_an_exception(self):
        self.assertEqual(wd.get_fsize(os.path.join(self.tmp, "nope.txt")), 0)


class IsAgentDeadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "transcript.jsonl")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_missing_transcript_counts_as_dead(self):
        self.assertTrue(wd.is_agent_dead(os.path.join(self.tmp, "nope.jsonl")))

    def test_a_terminal_type_marks_the_agent_dead(self):
        for terminal_type in ("CANCELLATION", "TERMINATION", "ERROR", "KILLED"):
            with self.subTest(terminal_type=terminal_type):
                _write_transcript(self.path, [{"type": terminal_type}])
                self.assertTrue(wd.is_agent_dead(self.path))

    def test_a_terminal_status_marks_the_agent_dead(self):
        for terminal_status in ("CANCELLED", "KILLED", "FAILED", "ERROR", "TERMINATED"):
            with self.subTest(terminal_status=terminal_status):
                _write_transcript(self.path, [{"status": terminal_status}])
                self.assertTrue(wd.is_agent_dead(self.path))

    def test_a_system_message_mentioning_killed_marks_the_agent_dead(self):
        _write_transcript(self.path, [
            {"source": "SYSTEM", "type": "SYSTEM_MESSAGE", "content": "Agent was killed by the orchestrator"}
        ])
        self.assertTrue(wd.is_agent_dead(self.path))

    def test_a_system_message_not_mentioning_a_kill_word_does_not_mark_it_dead(self):
        _write_transcript(self.path, [
            {"source": "SYSTEM", "type": "SYSTEM_MESSAGE", "content": "Agent checked in normally"}
        ])
        self.assertFalse(wd.is_agent_dead(self.path))

    def test_a_planner_response_with_no_tool_calls_is_idle_and_counts_as_dead(self):
        _write_transcript(self.path, [
            {"source": "MODEL", "type": "PLANNER_RESPONSE", "tool_calls": []}
        ])
        self.assertTrue(wd.is_agent_dead(self.path))

    def test_a_planner_response_with_pending_tool_calls_is_alive(self):
        _write_transcript(self.path, [
            {"source": "MODEL", "type": "PLANNER_RESPONSE", "tool_calls": [{"name": "Bash"}]}
        ])
        self.assertFalse(wd.is_agent_dead(self.path))

    def test_only_the_most_recent_matching_line_decides_the_verdict(self):
        # An earlier idle PLANNER_RESPONSE followed by a later one with real
        # tool calls must read as alive -- is_agent_dead scans in reverse and
        # must stop at the FIRST (i.e. most recent) decisive entry, not the
        # first one found in file order.
        _write_transcript(self.path, [
            {"source": "MODEL", "type": "PLANNER_RESPONSE", "tool_calls": []},
            {"source": "MODEL", "type": "PLANNER_RESPONSE", "tool_calls": [{"name": "Bash"}]},
        ])
        self.assertFalse(wd.is_agent_dead(self.path))

    def test_malformed_json_lines_are_skipped_not_fatal(self):
        with open(self.path, "w") as f:
            f.write("{not valid json\n")
            f.write(json.dumps({"source": "MODEL", "type": "PLANNER_RESPONSE", "tool_calls": []}) + "\n")
        self.assertTrue(wd.is_agent_dead(self.path))

    def test_a_transcript_with_no_decisive_signal_defaults_to_alive(self):
        _write_transcript(self.path, [{"type": "SOME_OTHER_EVENT"}])
        self.assertFalse(wd.is_agent_dead(self.path))

    def test_an_empty_transcript_defaults_to_alive(self):
        with open(self.path, "w"):
            pass
        self.assertFalse(wd.is_agent_dead(self.path))


def _write_subagent_spawn_line(transcript_path, child_ids):
    """A minimal real-shaped line matching parse_family_tree's own scan:
    a plain (non-EPHEMERAL_MESSAGE) entry whose serialized content contains
    both the literal 'Created the following subagents' marker and each
    child's conversationId, extractable by parse_family_tree's own regex."""
    content = "Created the following subagents: " + " ".join(
        f'"conversationId": "{cid}"' for cid in child_ids
    )
    entry = {"type": "MODEL_MESSAGE", "content": content}
    # parse_family_tree's own membership check requires the LITERAL
    # substring 'conversationId' in the raw line too (its first, cheap
    # pre-filter before a real json.loads).
    _write_transcript(transcript_path, [entry])


class ParseFamilyTreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _transcript_path(self, agent_id):
        return os.path.join(self.tmp, agent_id, ".system_generated", "logs", "transcript.jsonl")

    def test_a_lone_agent_with_no_children_is_its_own_root_and_family(self):
        active_family, parent_map, root_id = wd.parse_family_tree(self.tmp, "solo-agent")
        self.assertEqual(root_id, "solo-agent")
        self.assertEqual(active_family, {"solo-agent"})
        self.assertEqual(parent_map, {})

    def test_a_parent_spawning_one_child_links_them(self):
        _write_subagent_spawn_line(self._transcript_path("parent-1"), ["child-1"])

        active_family, parent_map, root_id = wd.parse_family_tree(self.tmp, "child-1")
        self.assertEqual(root_id, "parent-1")
        self.assertEqual(parent_map["child-1"], "parent-1")
        self.assertEqual(active_family, {"parent-1", "child-1"})

    def test_a_three_generation_chain_resolves_to_the_true_root(self):
        _write_subagent_spawn_line(self._transcript_path("grandparent"), ["parent-1"])
        _write_subagent_spawn_line(self._transcript_path("parent-1"), ["child-1"])

        active_family, parent_map, root_id = wd.parse_family_tree(self.tmp, "child-1")
        self.assertEqual(root_id, "grandparent")
        self.assertEqual(active_family, {"grandparent", "parent-1", "child-1"})

    def test_siblings_of_the_queried_agent_are_included_in_the_active_family(self):
        _write_subagent_spawn_line(self._transcript_path("parent-1"), ["child-1", "child-2"])

        active_family, parent_map, root_id = wd.parse_family_tree(self.tmp, "child-1")
        self.assertEqual(root_id, "parent-1")
        self.assertEqual(active_family, {"parent-1", "child-1", "child-2"})

    def test_an_unrelated_agent_directory_is_not_pulled_into_the_family(self):
        _write_subagent_spawn_line(self._transcript_path("parent-1"), ["child-1"])
        _write_subagent_spawn_line(self._transcript_path("unrelated-parent"), ["unrelated-child"])

        active_family, parent_map, root_id = wd.parse_family_tree(self.tmp, "child-1")
        self.assertNotIn("unrelated-parent", active_family)
        self.assertNotIn("unrelated-child", active_family)


class GetQueueTests(unittest.TestCase):
    def setUp(self):
        wd._QUEUES.clear()
        wd._QUEUE_META.clear()

    def test_first_call_creates_a_queue_with_fresh_metadata(self):
        q = wd._get_queue("my-queue")
        self.assertIn("my-queue", wd._QUEUES)
        meta = wd._QUEUE_META["my-queue"]
        self.assertIsNone(meta["last_put_at"])
        self.assertIsNone(meta["last_get_at"])
        self.assertEqual(meta["waiters"], 0)
        self.assertIs(q, wd._QUEUES["my-queue"])

    def test_repeated_calls_for_the_same_name_return_the_same_queue_object(self):
        q1 = wd._get_queue("shared")
        q1.put("hello")
        q2 = wd._get_queue("shared")
        self.assertIs(q1, q2)
        self.assertEqual(q2.get_nowait(), "hello")


class _FakeConn:
    """Stands in for a socket connection: recv() returns pre-scripted
    chunks in order, then b"" (EOF), matching _handle_bridge_conn's own
    read-until-EOF loop."""

    def __init__(self, chunks):
        self._chunks = list(chunks) + [b""]
        self.closed = False

    def recv(self, _n):
        return self._chunks.pop(0)

    def close(self):
        self.closed = True


class HandleBridgeConnTests(unittest.TestCase):
    def setUp(self):
        wd._QUEUES.clear()
        wd._QUEUE_META.clear()

    def test_a_well_formed_message_is_queued_under_its_named_queue(self):
        conn = _FakeConn([b"my-queue\nhello world"])
        wd._handle_bridge_conn(conn)
        self.assertEqual(wd._get_queue("my-queue").get_nowait(), "hello world")
        self.assertTrue(conn.closed)
        self.assertIsNotNone(wd._QUEUE_META["my-queue"]["last_put_at"])

    def test_a_leading_slash_on_the_queue_name_is_stripped(self):
        conn = _FakeConn([b"/my-queue\nhello"])
        wd._handle_bridge_conn(conn)
        self.assertEqual(wd._get_queue("my-queue").get_nowait(), "hello")

    def test_a_message_split_across_multiple_recv_chunks_is_reassembled(self):
        conn = _FakeConn([b"my-queue\nhel", b"lo wor", b"ld"])
        wd._handle_bridge_conn(conn)
        self.assertEqual(wd._get_queue("my-queue").get_nowait(), "hello world")

    def test_a_message_with_no_newline_header_is_dropped_not_queued(self):
        conn = _FakeConn([b"no header here at all"])
        wd._handle_bridge_conn(conn)
        self.assertEqual(wd._QUEUES, {})
        self.assertTrue(conn.closed)

    def test_an_empty_queue_name_is_dropped(self):
        conn = _FakeConn([b"\nsome content"])
        wd._handle_bridge_conn(conn)
        self.assertEqual(wd._QUEUES, {})

    def test_a_recv_exception_still_closes_the_connection(self):
        class ExplodingConn:
            def recv(self, _n):
                raise OSError("simulated socket failure")

            def close(self):
                self.closed = True

        conn = ExplodingConn()
        wd._handle_bridge_conn(conn)
        self.assertTrue(conn.closed)


if __name__ == "__main__":
    unittest.main()
