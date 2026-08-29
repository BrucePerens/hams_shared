# -*- coding: utf-8 -*-
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Unit tests for mcp_watchdog_client.py -- a dependency-free CLI client written 2026-08-28 so
anything that can shell out (a subagent whose own MCP tool-calling layer is flaky, in the case
that motivated this) can still reach mcp_watchdog.py's shared instance directly.
"""
import asyncio
import unittest

import mcp_watchdog_client as client


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
        import json
        import subprocess
        import sys

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


if __name__ == "__main__":
    unittest.main()
