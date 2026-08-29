# -*- coding: utf-8 -*-
# Copyright © Bruce Perens K6BP. All Rights Reserved. This software is proprietary and confidential.
"""
Regression coverage for usability_audit_daemon.py's own IPC client, found live
(2026-08-28) while running this daemon for real against a running hams.com.
"""
import asyncio
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, __import__("os").path.dirname(__file__))
import usability_audit_daemon as uad


def _fake_sse_client(url):
    class _Ctx:
        async def __aenter__(self_inner):
            return (MagicMock(), MagicMock())

        async def __aexit__(self_inner, *exc):
            return False

    return _Ctx()


def _fake_client_session_factory(sent_calls):
    class _FakeSession:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self_inner):
            return self_inner

        async def __aexit__(self_inner, *exc):
            return False

        async def initialize(self_inner):
            return None

        async def call_tool(self_inner, name, arguments):
            sent_calls.append((name, arguments))
            return MagicMock()

    return _FakeSession


class SendIpcMessageFromARunningEventLoopTests(unittest.TestCase):
    # Found live: usability_audit_daemon.py runs inside Playwright's *sync*
    # API, which already has a running asyncio event loop in the calling
    # thread -- a bare `asyncio.run()` inside _send_ipc_message crashed with
    # the exact "asyncio.run() cannot be called from a running event loop"
    # error docs/MCP_WATCHDOG_REPORT.md describes, just triggered by
    # Playwright instead of Gemini's own MCP proxy. This test reproduces
    # that exact condition (calling the sync _send_ipc_message from a thread
    # that already has a running loop) without needing a real server, by
    # calling it from inside an async function driven by asyncio.run().

    def test_send_succeeds_when_called_from_a_thread_with_a_running_loop(self):
        sent_calls = []

        async def call_from_within_a_running_loop():
            # At this point asyncio.get_running_loop() succeeds in this
            # thread -- the exact condition that broke the old
            # bare-asyncio.run() implementation.
            with patch("mcp.client.sse.sse_client", side_effect=_fake_sse_client), patch(
                "mcp.client.session.ClientSession",
                new=_fake_client_session_factory(sent_calls),
            ):
                uad._send_ipc_message("some_queue", "some content")

        # Must not raise "asyncio.run() cannot be called from a running event loop".
        asyncio.run(call_from_within_a_running_loop())

        self.assertEqual(len(sent_calls), 1)
        name, arguments = sent_calls[0]
        self.assertEqual(name, "send_ipc_message")
        self.assertEqual(arguments, {"queue_name": "some_queue", "content": "some content"})

    def test_a_real_send_failure_propagates_instead_of_being_swallowed(self):
        async def call_from_within_a_running_loop():
            with patch("mcp.client.sse.sse_client", side_effect=RuntimeError("connection refused")):
                uad._send_ipc_message("some_queue", "some content")

        with self.assertRaises(RuntimeError):
            asyncio.run(call_from_within_a_running_loop())


if __name__ == "__main__":
    unittest.main()
