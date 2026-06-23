---
name: mcp-test-runner
description: Explains how to use the MCP test server to rapidly iterate on Odoo tests without waiting for full environment teardowns.
---

# Odoo MCP Test Runner

The Hams.com repository includes an MCP (Model Context Protocol) server designed to dramatically speed up test-driven development. Instead of restarting the entire Odoo registry (which takes over a minute), you can connect to the MCP server and run tests or update modules dynamically.

## How to Start the Server

If the server is not already running, you can start it by passing the `--mcp` flag to the test runner:

```bash
python3 tools/test.py --mcp
```

This will launch Odoo, load the registry, and then start the FastMCP server on `stdio`. Note that `tools/test.py` usually manages infrastructure; running with `--mcp` will do exactly the same but leave the environment running and expose the MCP tools.

## Available MCP Tools

When connected to the MCP server, you have access to the following tools:

### `run_tests`
Run the test suite for specific modules.
- **Parameters:** `module_names` (string, comma-separated list of modules like `"user_websites,zero_sudo"`).
- **Behavior:** This dynamically discovers `unittest` test suites within the `tests` folder of the specified modules and executes them, streaming the output back.

### `update_modules`
Update Odoo module definitions dynamically.
- **Parameters:** `module_names` (string, comma-separated list).
- **Behavior:** If you change Python models, XML views, or security files, run this tool to trigger `button_immediate_upgrade` on those modules in the database, refreshing the registry without restarting.

### `reload_test_files`
Hot-reload Python test files.
- **Parameters:** `module_names` (string, comma-separated list).
- **Behavior:** If you only modify a test file (e.g. `test_security_utils.py`), you do *not* need to update the whole module. Just run this tool to hot-reload the test modules into Python's `sys.modules`, and then run `run_tests` again.

## Standard Workflow

1. Modify an Odoo Model / View / Controller: Use `update_modules("my_module")`, then `run_tests("my_module")`.
2. Modify a Test File only: Use `reload_test_files("my_module")`, then `run_tests("my_module")`.

## Debugging UI Tours
If you are iterating on a Javascript Tour, you can pass the `--pause-on-fail` flag when starting `tools/test.py` (it works alongside `--mcp`). If a UI tour fails, the runner will freeze the headless Chrome browser and expose port `9222`. You can then activate your `chrome-devtools` MCP server to inspect the active DOM and execute scripts within the failed tour environment.

**Important**: This skill should be used whenever you are iteratively debugging tests and do not want to wait for the standard `tools/test.py` slow boot sequence.
