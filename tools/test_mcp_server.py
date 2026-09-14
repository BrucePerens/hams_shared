#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

import sys
import os
import signal
import logging
import io
import unittest
import importlib
import subprocess
import threading
import time
import traceback
from contextlib import redirect_stdout, redirect_stderr

from mcp.server.fastmcp import FastMCP, Context
import odoo
from odoo.tools import config
from odoo.cli import server
import odoo.service.server
import odoo.modules.module
from odoo.modules.registry import Registry

_logger = logging.getLogger(__name__)

mcp = FastMCP("OdooTestServer")

class NotifyStream:
    """Wraps an io stream to intercept and forward critical logs to the MCP context."""
    def __init__(self, target_stream, ctx: Context, prefix="[TEST] "):
        self.target_stream = target_stream
        self.ctx = ctx
        self.prefix = prefix
        self._buffer = ""
        self._line_buffer = []
        self._printing_errors = False

    def write(self, s):
        self.target_stream.write(s)
        self._buffer += s
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._process_line(line)

    def flush(self):
        self.target_stream.flush()
        if self._buffer:
            self._process_line(self._buffer)
            self._buffer = ""

    def _process_line(self, line):
        if line.startswith("======================================================================"):
            self._printing_errors = True

        if self._printing_errors:
            self.ctx.warning(f"{self.prefix}{line}")
            return

        is_success = line == "ok" or line.endswith(" ok") or line.endswith(" skipped") or "expected failure" in line
        is_fail = line == "FAIL" or line.endswith(" FAIL") or line == "ERROR" or line.endswith(" ERROR")
        
        self._line_buffer.append(line)
        
        if is_success:
            self._line_buffer = []
        elif is_fail:
            msg = "\n".join(self._line_buffer)
            self.ctx.warning(f"{self.prefix}TEST FAILED:\n{msg}")
            self._line_buffer = []

def setup_odoo():
    """Initializes Odoo environment without running standard test loops."""
    # Remove --mcp if it exists so Odoo parser doesn't fail
    args = sys.argv[1:]
    if "--mcp" in args:
        args.remove("--mcp")

    config.parse_config(args)
    # We explicitly disable test_enable so Odoo doesn't run tests and exit
    odoo.tools.config["test_enable"] = False
    server.report_configuration()
    db_name = config["db_name"]
    if isinstance(db_name, list) and len(db_name) > 0:
        db_name = db_name[0]

    odoo.service.server.start(preload=[db_name], stop=True)
    registry = Registry(db_name)
    _logger.info("Odoo MCP Server initialized on DB: %s", db_name)
    return registry, db_name


@mcp.tool()
def run_tests(module_names: str, ctx: Context) -> str:
    """
    Run tests for the specified modules (comma separated).
    Example: module_names="user_websites,zero_sudo"
    """
    out = io.StringIO()
    notify_out = NotifyStream(out, ctx, prefix="[TEST] ")
    with redirect_stdout(notify_out), redirect_stderr(notify_out):
        try:
            modules = [m.strip() for m in module_names.split(",") if m.strip()]
            suite = unittest.TestSuite()
            for mod_name in modules:
                try:
                    test_module = importlib.import_module(
                        f"odoo.addons.{mod_name}.tests"
                    )
                except ImportError:
                    print(f"No tests found for module {mod_name}")
                    continue

                # Discover tests in the module
                mod_suite = unittest.defaultTestLoader.discover(
                    os.path.dirname(test_module.__file__),
                    top_level_dir=os.path.dirname(test_module.__file__),
                )
                suite.addTest(mod_suite)

            runner = unittest.TextTestRunner(stream=sys.stdout, verbosity=2)
            runner.run(suite)
        except Exception:  # audit-ignore-catch-all
            # Real bug found live, 2026-09-13: this used to only log via
            # _logger.exception (server-side only) and return whatever had
            # already been written to `out` before the crash -- often empty,
            # since discovery can fail before any test output is produced at
            # all. The calling agent got back an empty or truncated string
            # with zero indication a crash happened, indistinguishable from
            # "ran cleanly, nothing to report." Writing the traceback into
            # the same buffer the caller actually reads means a real
            # discovery/runner crash is never silently invisible to them.
            _logger.exception("Error running tests:")
            out.write(f"\n[MCP SERVER ERROR] run_tests crashed:\n{traceback.format_exc()}")

    return out.getvalue()

@mcp.tool()
def run_linters(module_names: str, ctx: Context) -> str:
    """
    Run linters for the specified modules.
    Example: module_names="user_websites" or module_names="."
    """
    out_buf = io.StringIO()
    dir_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    workspace_dir = os.path.dirname(dir_path)

    if module_names == "." or not module_names:
        try:
            res = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, cwd=workspace_dir)
            changed_modules = set()
            for line in res.stdout.splitlines():
                if line.strip():
                    filepath = line[3:]
                    parts = filepath.split("/")
                    if len(parts) > 1:
                        changed_modules.add(parts[0])
            
            if changed_modules:
                module_names = ",".join(changed_modules)
                ctx.info(f"Auto-detected modified modules: {module_names}")
            else:
                ctx.info("No modified modules detected, running on all modules.")
                module_names = "."
        except Exception as e: # audit-ignore-catch-all
            ctx.info(f"Failed to detect modified files via git: {e}")

    cmd = [sys.executable, os.path.join(dir_path, "tools", "run_linters.py"), module_names]

    ctx.info(f"Starting linters: {' '.join(cmd)}")

    # Run the linter subprocess, streaming output to intercept violations. run_linters.py
    # never opts into unbuffered/line-buffered stdout itself, so without PYTHONUNBUFFERED=1
    # here, Python defaults to block-buffering (~8KB) once stdout isn't a TTY (true for any
    # PIPE) -- the readline() loop below would then see nothing until the child's internal
    # buffer fills or it exits, defeating the whole point of streaming to catch violations live.
    linter_env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=dir_path, env=linter_env)
    
    for line in iter(process.stdout.readline, ''):
        out_buf.write(line)
        if "❌" in line or "⚠️" in line or "ERROR" in line or "FAIL" in line:
            ctx.warning(f"[LINTER] {line.strip()}")
            
    process.wait()
    return out_buf.getvalue()


@mcp.tool()
def update_modules(module_names: str) -> str:
    """
    Trigger Odoo's registry reload and module update mechanism.
    Example: module_names="user_websites"
    """
    db_name = odoo.tools.config["db_name"]
    if isinstance(db_name, list) and db_name:
        db_name = db_name[0]
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(out):
        try:
            modules = [m.strip() for m in module_names.split(",") if m.strip()]
            if not modules:
                print("No modules specified.")
                return out.getvalue()

            # Real fix, 2026-09-14 (night_shift_todo.md's "OPEN QUESTION for Bruce" ->
            # "FIXED" entry): this used to do `env = odoo.api.Environment(cr,
            # odoo.SUPERUSER_ID, {})` and call `mod_records.button_immediate_upgrade()`
            # directly in-process under that superuser env -- a CRITICAL ZERO-SUDO
            # VIOLATION (check_burn_list.py). Unlike list_routes.py's own transient-
            # bootstrap fix in this same pass, resolving a scoped service-account uid
            # instead does NOT work here: Odoo core's own `button_immediate_upgrade`/
            # `button_upgrade` are decorated `@assert_log_admin_access`, which requires
            # `env.is_admin()` (`env.su`, or the acting user holding
            # `base.group_erp_manager`) -- confirmed directly in the installed Odoo
            # source (odoo/addons/base/models/ir_module.py). zero_sudo's own
            # `_get_service_uid()` SQL procedure structurally REJECTS resolving any
            # service account that holds `base.group_system`/`base.group_erp_manager`
            # (zero_sudo/models/security_utils.py's own docstring,
            # zero_sudo/data/postgres_procedures.xml, and
            # zero_sudo/tests/test_security_utils.py's own coverage of that rejection).
            # So no resolved service-account uid could ever legally call this method --
            # there is no second, narrowly-scoped uid capable of doing this specific job
            # at all, so the "transient bootstrap, then a real second env" pattern
            # genuinely does not apply to this one call.
            #
            # Real fix instead: this tool suite's own module-install driver
            # (hams_shared/tools/test.py) already updates/installs modules by shelling
            # out to a fresh, short-lived `odoo` OS process (`/usr/bin/odoo -d <db>
            # -u/-i <mods> --stop-after-init`), never by calling
            # `button_immediate_upgrade()` in-process under any particular ORM uid at
            # all -- the real privilege boundary there is the OS process's own identity
            # (this MCP server already runs as the `odoo` OS user, per this tool
            # suite's standing `sudo -u odoo` invocation convention -- see CLAUDE.md),
            # not an Odoo Environment's uid. Copying that already-established, already-
            # trusted pattern here eliminates the SUPERUSER_ID Environment from this
            # file entirely, rather than merely narrowing it.
            #
            # Module existence is checked via get_manifest() (a filesystem/manifest
            # lookup, no DB Environment needed at all) instead of the old
            # `env["ir.module.module"].search(...)` -- see check_burn_list.py's own
            # "CRITICAL FRAMEWORK ACL" rule, which flags that exact search shape as
            # needing `base.group_user`; sidestepping the ORM search entirely avoids
            # that question rather than routing around it.
            missing = [m for m in modules if not odoo.modules.module.get_manifest(m)]
            if missing:
                print(f"Modules not found on disk: {', '.join(missing)}")
                return out.getvalue()

            cmd = [
                "/usr/bin/odoo",
                "-c", odoo.tools.config["config"],
                "-d", db_name,
                "-u", ",".join(modules),
                "--stop-after-init",
                "--workers=0",
                "--max-cron-threads=0",
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            print(result.stdout)
            if result.stderr:
                print(result.stderr)

            if result.returncode != 0:
                print(
                    f"[MCP SERVER ERROR] module update subprocess exited with code "
                    f"{result.returncode} for: {', '.join(modules)}"
                )
            else:
                # This long-running MCP server's OWN in-process registry was
                # bootstrapped once at server startup by setup_odoo(), long before the
                # subprocess above ran its upgrade -- Registry.new() rebuilds it from
                # the now-upgraded database so subsequent run_tests()/
                # reload_test_files() calls in THIS SAME server process see the
                # upgrade, not the stale pre-upgrade in-memory registry.
                Registry.new(db_name)
                print(f"Successfully triggered update for: {', '.join(modules)}")
        except Exception:  # audit-ignore-catch-all
            # Same real bug as run_tests's own fix above, more consequential here:
            # an unexpected crash (e.g. subprocess.run() itself raising, not just
            # the subprocess exiting non-zero, which is already handled above)
            # used to leave the caller with an EMPTY returned string (nothing is
            # printed before the crash), indistinguishable from "nothing to do"
            # -- for a real DB mutation, that's a caller silently believing an
            # upgrade succeeded when it actually never ran.
            _logger.exception("Error updating modules:")
            out.write(f"\n[MCP SERVER ERROR] update_modules crashed:\n{traceback.format_exc()}")

    return out.getvalue()


@mcp.tool()
def reload_test_files(module_names: str) -> str:
    """
    Hot-reload test files using importlib.reload.
    Example: module_names="user_websites"
    """
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(out):
        try:
            modules = [m.strip() for m in module_names.split(",") if m.strip()]
            for mod_name in modules:
                try:
                    test_module = importlib.import_module(
                        f"odoo.addons.{mod_name}.tests"
                    )
                    importlib.reload(test_module)
                    print(f"Reloaded tests for {mod_name}")
                except ImportError:
                    print(f"Failed to reload tests for {mod_name}")
        except Exception:  # audit-ignore-catch-all
            # Same real bug as run_tests/update_modules above.
            _logger.exception("Error reloading test files:")
            out.write(f"\n[MCP SERVER ERROR] reload_test_files crashed:\n{traceback.format_exc()}")
    return out.getvalue()


def _kill_own_process_group():
    # SIGKILL against a process's own group can't be caught, deferred, or
    # cleaned up after -- it lands mid-instruction with no chance for any
    # caller further up the stack to run again. Doing this synchronously
    # inside the tool call means the MCP transport never gets to flush the
    # "Killed" response before the process delivering it is gone.
    try:
        os.killpg(os.getpgid(os.getpid()), signal.SIGKILL)
    except OSError as e:
        _logger.error("Error killing process group: %s", e)
        os._exit(1)


@mcp.tool()
def kill_server() -> str:
    """
    Kill the MCP server and Odoo processes entirely.
    Example: (no arguments)
    """
    print("Shutting down MCP server and all its subprocesses...")
    # Real fix, 2026-09-12 (hams_shared/tools/ adversarial pass): the kill used to
    # run synchronously here, before `return "Killed"` -- since the MCP server is
    # spawned in its own session/process group by test.py, that SIGKILL took down
    # this process too, before it ever returned. The caller got a dropped
    # connection instead of the tool's declared string response. Fire the kill
    # from a background thread on a short delay instead, so this call can return
    # "Killed" and let the MCP transport flush it first.
    threading.Thread(target=lambda: (time.sleep(0.5), _kill_own_process_group()), daemon=True).start()
    return "Killed"


def main():
    setup_odoo()
    mcp.run()


if __name__ == "__main__":
    main()
