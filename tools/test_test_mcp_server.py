#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for test_mcp_server.py.

`test_mcp_server.py` is a real MCP tool server (spawned by test.py as the live
Odoo process itself, communicating over stdio), not a pytest suite -- run_linters.py
deliberately excludes it from test-suite collection (see check_burn_list.py's own
comment on it and test_run_linters.py's test_test_mcp_server_py_is_excluded). It had
zero test coverage before this file: importing it is safe (the `@mcp.tool()`
decorators just register functions on a module-level FastMCP instance, and `odoo`/
`mcp` are both installed in this dev environment), but every tool function past that
either bootstraps a real Odoo registry or, in kill_server's case, terminates the
current process group -- both too high blast-radius to exercise directly in a unit
test. This file covers what's safe to verify without a live registry or an actual
process kill: kill_server's response-before-termination ordering, via a mocked
os.killpg that records whether it already fired by the time kill_server() returns.
"""

import threading
import time
import unittest
from unittest import mock

import test_mcp_server


class KillServerOrderingTests(unittest.TestCase):
    """kill_server() must return its "Killed" response before the process group
    is actually SIGKILLed -- a self-directed SIGKILL can't be caught or deferred,
    so firing it synchronously before the return statement means the MCP caller
    never receives the response at all, just a dropped connection."""

    def test_kill_server_returns_before_the_process_group_is_killed(self):
        killed = mock.Mock()
        # Keep the patch alive past kill_server()'s own return -- the fix defers
        # the real kill to a background thread on a short delay, and letting the
        # `with` block exit (restoring the real os.killpg) before that thread
        # fires would let it run UNMOCKED, actually killing this test process.
        with mock.patch.object(test_mcp_server.os, "killpg", killed):
            result = test_mcp_server.kill_server()
            self.assertEqual(result, "Killed")
            # At the moment kill_server() returns, the kill must not have fired yet.
            killed.assert_not_called()
            time.sleep(1.0)
            killed.assert_called()

    def test_the_process_group_is_still_actually_killed_shortly_after(self):
        killed_event = threading.Event()

        def fake_killpg(*args, **kwargs):
            killed_event.set()

        with mock.patch.object(test_mcp_server.os, "killpg", side_effect=fake_killpg):
            test_mcp_server.kill_server()
            # The deferred kill runs on a fixed short delay (see
            # _kill_own_process_group's caller) -- give it a generous window.
            self.assertTrue(killed_event.wait(timeout=3.0), "os.killpg was never called")


class CrashVisibilityTests(unittest.TestCase):
    """Real bug found live, 2026-09-13: run_tests/update_modules/reload_test_files each
    caught any unexpected exception with `except Exception: _logger.exception(...)`, then
    returned whatever had already been written to their own output buffer -- often empty,
    since a crash during discovery/registry setup happens before any real output is
    produced. The calling MCP client got back an empty or truncated string,
    indistinguishable from "ran cleanly, nothing to report." Each tool now also writes the
    traceback into its own returned buffer, so a real crash is never silently invisible to
    the caller. None of these bootstrap a live Odoo registry -- each mocks out exactly the
    call that would otherwise need one, so the crash path is exercised without one."""

    def test_run_tests_reports_a_discovery_crash_in_its_own_return_value(self):
        fake_module = mock.Mock()
        fake_module.__file__ = "/fake/path/tests/__init__.py"
        with mock.patch.object(
            test_mcp_server.importlib, "import_module", return_value=fake_module
        ), mock.patch.object(
            test_mcp_server.unittest.defaultTestLoader,
            "discover",
            side_effect=RuntimeError("boom: discovery exploded"),
        ):
            result = test_mcp_server.run_tests("some_module", mock.Mock())
        self.assertIn("MCP SERVER ERROR", result)
        self.assertIn("run_tests crashed", result)
        self.assertIn("boom: discovery exploded", result)

    def test_update_modules_reports_a_registry_crash_in_its_own_return_value(self):
        # odoo.registry is resolved lazily (Odoo's own namespace-package
        # __getattr__), not a real static attribute -- mock.patch.object's
        # getattr-based lookup can't see it, but it still works to call and to
        # assign, so patch it with create=True instead.
        with mock.patch.object(
            test_mcp_server.odoo,
            "registry",
            create=True,
            side_effect=RuntimeError("boom: registry exploded"),
        ):
            result = test_mcp_server.update_modules("some_module")
        self.assertIn("MCP SERVER ERROR", result)
        self.assertIn("update_modules crashed", result)
        self.assertIn("boom: registry exploded", result)

    def test_reload_test_files_reports_a_reload_crash_in_its_own_return_value(self):
        fake_module = mock.Mock()
        with mock.patch.object(
            test_mcp_server.importlib, "import_module", return_value=fake_module
        ), mock.patch.object(
            test_mcp_server.importlib,
            "reload",
            side_effect=RuntimeError("boom: reload exploded"),
        ):
            result = test_mcp_server.reload_test_files("some_module")
        self.assertIn("MCP SERVER ERROR", result)
        self.assertIn("reload_test_files crashed", result)
        self.assertIn("boom: reload exploded", result)


if __name__ == "__main__":
    unittest.main()
