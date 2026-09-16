# -*- coding: utf-8 -*-
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Unit tests for mcp_watchdog_client.py -- a dependency-free CLI client written 2026-08-28 so
anything that can shell out (a subagent whose own MCP tool-calling layer is flaky, in the case
that motivated this) can still reach mcp_watchdog.py's shared instance directly.
"""
import asyncio
import http.server
import json
import os
import subprocess
import sys
import threading
import time
import unittest

import mcp_watchdog_client as client


class _SilentSSEHandler(http.server.BaseHTTPRequestHandler):
    """Accepts the SSE connection, then says nothing at all.

    This is the failure the client's deadlines exist for, and it is NOT a
    connection failure: the TCP connect succeeds, the HTTP response is a real
    `text/event-stream`, and the socket stays open. `sse_client()` waits for an
    `endpoint` event that never comes, so no MCP request is ever in flight --
    which is exactly why the per-request `read_timeout_seconds` cannot catch
    this case and the outer whole-call deadline has to.
    """

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.flush()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not self.server.stop_flag.is_set():
            time.sleep(0.1)

    def log_message(self, *args):  # keep the test output readable
        pass


class _SilentSSEServer:
    def __enter__(self):
        self._srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _SilentSSEHandler)
        self._srv.stop_flag = threading.Event()
        self._srv.daemon_threads = True
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()
        return "http://127.0.0.1:%d/sse" % self._srv.server_address[1]

    def __exit__(self, *exc):
        self._srv.stop_flag.set()
        self._srv.shutdown()
        self._srv.server_close()
        return False


class RunInOwnThreadTests(unittest.TestCase):
    # Same failure this session hit twice already tonight (usability_audit_daemon.py inside
    # Playwright's sync API): a bare asyncio.run() breaks if the calling thread already has a
    # running event loop. _run_in_own_thread must work even when called from inside one.

    def test_returns_the_coroutines_result(self):
        async def _coro():
            return "hello"

        self.assertEqual(client._run_in_own_thread(_coro()), "hello")

    def test_propagates_a_real_exception_instead_of_swallowing_it(self):
        async def _coro():
            raise ValueError("boom")

        with self.assertRaises(ValueError):
            client._run_in_own_thread(_coro())

    def test_works_when_called_from_a_thread_with_a_running_loop(self):
        async def _outer():
            async def _inner():
                return "worked from inside a running loop"

            return client._run_in_own_thread(_inner())

        self.assertEqual(asyncio.run(_outer()), "worked from inside a running loop")


class CliSmokeTests(unittest.TestCase):
    # Exercises the real argparse wiring in main() via subprocess, against a queue name that
    # will never exist, so it stays fast and network-independent: `status` on a never-seen
    # queue always returns "exists": false immediately, without needing a live server.

    def test_status_on_an_unknown_queue_reports_it_does_not_exist(self):
        result = subprocess.run(
            [sys.executable, "mcp_watchdog_client.py", "status", "definitely_never_used_queue_xyz"],
            capture_output=True,
            text=True,
            timeout=15,
            cwd=__import__("os").path.dirname(__file__),
        )
        # Either a real server is up and answers "exists": false, or none is running and the
        # client reports a clean CLIENT ERROR (exit 1) rather than hanging or crashing -- both
        # are acceptable outcomes for this test, which only checks the CLI wiring itself works.
        if result.returncode == 0:
            status = json.loads(result.stdout)
            self.assertFalse(status["exists"])
        else:
            self.assertIn("CLIENT ERROR", result.stderr)


class DeadlineTests(unittest.TestCase):
    """A server that accepts and never answers must fail, not hang.

    Before 2026-09-15 every deadline in this client was the library default:
    `ClientSession(read_timeout_seconds=None)` and
    `call_tool(read_timeout_seconds=None)` both mean "never time out", so an
    unattended agent session shelling out to this CLI could block forever on a
    wedged watchdog. Separately, `sse_client`'s own 300s `sse_read_timeout`
    default was SHORTER than a legitimate `wait --timeout-mins 15` (900s), so
    the one long-running command this CLI has was the one the transport cut
    off early.
    """

    def test_a_silent_server_fails_within_the_deadline_instead_of_hanging(self):
        with _SilentSSEServer() as sse_url:
            started = time.monotonic()
            with self.assertRaises(client.WatchdogClientTimeout):
                client._run_in_own_thread(
                    client._call_tool(
                        "queue_status",
                        {"queue_name": "whatever"},
                        sse_url,
                        call_timeout_secs=1,
                    )
                )
            elapsed = time.monotonic() - started

        # call_timeout_secs=1 + OVERALL_TIMEOUT_MARGIN_SECS. Asserted as a real
        # upper bound (with slack for a loaded box) rather than an exact value:
        # the point is that it returns at all, on its own deadline.
        self.assertLess(
            elapsed,
            1 + client.OVERALL_TIMEOUT_MARGIN_SECS + 10,
            "The client must give up on its own deadline, not hang.",
        )

    def test_the_cli_reports_a_silent_server_as_a_clean_client_error(self):
        """End to end through main(), so the deadline actually reaches a caller
        as exit 1 plus a message, not as a traceback or a wedged process."""
        with _SilentSSEServer() as sse_url:
            result = subprocess.run(
                [
                    sys.executable,
                    "mcp_watchdog_client.py",
                    "--sse-url",
                    sse_url,
                    "--timeout-secs",
                    "1",
                    "status",
                    "whatever",
                ],
                capture_output=True,
                text=True,
                timeout=1 + client.OVERALL_TIMEOUT_MARGIN_SECS + 30,
                cwd=os.path.dirname(__file__),
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn("CLIENT ERROR", result.stderr)

    def test_a_wait_derives_its_deadline_from_its_own_timeout_mins(self):
        """A fixed client deadline would cut off a legitimate long wait. The
        one that matters is the transport's: `sse_read_timeout` must end up
        larger than the server-side wait the caller asked for, not the
        library's 300s default."""
        seen = {}

        def _fake_sse_client(url, sse_read_timeout=None, **kwargs):
            # Deliberately a plain def, not async: the real sse_client is a
            # callable returning an async context manager, so an async def
            # here would hand _call_tool a bare coroutine and fail on the
            # wrong thing.
            seen["sse_read_timeout"] = sse_read_timeout
            raise RuntimeError("stop here -- only the derived timeout is under test")

        original = client.sse_client
        client.sse_client = _fake_sse_client
        try:
            requested_mins = 15
            with self.assertRaises(RuntimeError):
                client._run_in_own_thread(
                    client._call_tool(
                        "wait_for_inbox",
                        {"queue_name": "q", "timeout_mins": requested_mins},
                        "http://127.0.0.1:1/sse",
                        call_timeout_secs=requested_mins * 60
                        + client.WAIT_TIMEOUT_MARGIN_SECS,
                    )
                )
        finally:
            client.sse_client = original

        self.assertGreater(
            seen["sse_read_timeout"],
            requested_mins * 60,
            "The transport read timeout must outlast the wait the caller asked "
            "for, or a legitimate long wait dies on the transport instead of "
            "returning the server's own timeout result.",
        )


if __name__ == "__main__":
    unittest.main()
