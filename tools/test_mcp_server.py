#!/usr/bin/env python3
import sys
import os
import logging
import io
import unittest
import importlib
from contextlib import redirect_stdout, redirect_stderr

from mcp.server.fastmcp import FastMCP, Context
import odoo
from odoo.tools import config
from odoo.cli import server
import odoo.service.server
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
            _logger.exception("Error running tests:")

    return out.getvalue()

import subprocess

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
        except Exception as e:
            ctx.info(f"Failed to detect modified files via git: {e}")

    cmd = [sys.executable, os.path.join(dir_path, "tools", "run_linters.py"), module_names]
    
    ctx.info(f"Starting linters: {' '.join(cmd)}")
    
    # Run the linter subprocess, streaming output to intercept violations
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=dir_path)
    
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
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(out):
        try:
            registry = odoo.registry(db_name)
            with registry.cursor() as cr:
                env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})
                modules = [m.strip() for m in module_names.split(",") if m.strip()]

                mod_records = env["ir.module.module"].search([("name", "in", modules)])
                if mod_records:
                    mod_records.button_immediate_upgrade()
                    print(
                        f"Successfully triggered update for: {', '.join(mod_records.mapped('name'))}"
                    )
                else:
                    print(f"Modules not found in database: {module_names}")
        except Exception:  # audit-ignore-catch-all
            _logger.exception("Error updating modules:")

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
            _logger.exception("Error reloading test files:")
    return out.getvalue()


@mcp.tool()
def kill_server() -> str:
    """
    Kill the MCP server and Odoo processes entirely.
    Example: (no arguments)
    """
    import os
    import signal

    print("Shutting down MCP server and all its subprocesses...")
    try:
        # Since the MCP server is spawned in its own session/process group by test.py,
        # killing the process group will reliably take down Chrome, Odoo, and the server itself.
        os.killpg(os.getpgid(os.getpid()), signal.SIGKILL)
    except OSError as e:
        _logger.error("Error killing process group: %s", e)
        os._exit(1)
    return "Killed"


def main():
    setup_odoo()
    mcp.run()


if __name__ == "__main__":
    main()
