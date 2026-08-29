#!/usr/bin/env python3
# flake8: noqa
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Dependency-free CLI client for mcp_watchdog.py's shared SSE instance, for use by anything that
can shell out but can't reliably reach the MCP tool-calling layer itself. Found necessary
2026-08-28: a subagent's `watchdog` MCP server connected at the transport level (confirmed via its
own connection log) but never surfaced usable tool schemas, so `wait_for_inbox` etc. were simply
unavailable to it as MCP tools -- a real, observed flakiness in MCP tool registration, not
something this script works around by guessing, just by not depending on it at all. Talks the exact
same SSE protocol mcp_watchdog.py's own `wait_for_inbox`/`daemon_utils.py`/`usability_audit_daemon.py`
already use to reach the shared instance, so it exercises the real server, not a parallel mechanism.

Usage:
    python3 mcp_watchdog_client.py send <queue_name> <content>
    python3 mcp_watchdog_client.py status <queue_name>
    python3 mcp_watchdog_client.py wait <queue_name> [--timeout-mins N] [--reconnect-after-secs N]

Prints the tool's raw text result to stdout and exits 0 on success; prints an error to stderr and
exits 1 on failure (including a PROXY ERROR/RECONNECT_HINT/Timeout string from the server itself --
those are valid tool results, not client failures, so they print to stdout and exit 0; only a
genuine client-side failure to reach the server at all is an exit 1).
"""
import argparse
import asyncio
import logging
import os
import sys
import threading

from mcp.client.session import ClientSession
from mcp.client.sse import sse_client

_logger = logging.getLogger("mcp_watchdog_client")


def _default_sse_url():
    return os.environ.get("MCP_WATCHDOG_SSE_URL", "http://127.0.0.1:8767/sse")


async def _call_tool(tool_name, arguments, sse_url):
    for k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"]:
        os.environ.pop(k, None)
    async with sse_client(sse_url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            res = await session.call_tool(tool_name, arguments=arguments)
            return res.content[0].text if res.content else str(res)


def _run_in_own_thread(coro):
    """Same reasoning as usability_audit_daemon.py's own _send_ipc_message: a bare asyncio.run()
    breaks if the calling thread already has a running event loop (e.g. this script invoked from
    inside another asyncio-driven tool). A dedicated thread sidesteps that regardless of caller."""
    result = {}

    def _run():
        try:
            result["value"] = asyncio.run(coro)
        except Exception as e:  # audit-ignore-catch-all: captured here only to
            # cross the thread boundary -- fully re-raised in the calling
            # thread below, not swallowed. Must be unconditional: the caller
            # needs whatever the coroutine actually raised, not a guess at
            # which exception types an arbitrary awaited coroutine can produce.
            _logger.warning("Worker thread caught %s, re-raising in caller: %s", type(e).__name__, e)
            result["error"] = e

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join()
    if "error" in result:
        raise result["error"]
    return result["value"]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sse-url", default=None, help="Override the shared instance URL (default: %s or $MCP_WATCHDOG_SSE_URL)" % _default_sse_url())
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_send = sub.add_parser("send", help="send_ipc_message")
    p_send.add_argument("queue_name")
    p_send.add_argument("content")

    p_status = sub.add_parser("status", help="queue_status")
    p_status.add_argument("queue_name")

    p_wait = sub.add_parser("wait", help="wait_for_inbox")
    p_wait.add_argument("queue_name")
    p_wait.add_argument("--timeout-mins", type=int, default=15)
    p_wait.add_argument("--reconnect-after-secs", type=int, default=None)

    args = parser.parse_args()
    sse_url = args.sse_url or _default_sse_url()

    if args.cmd == "send":
        coro = _call_tool("send_ipc_message", {"queue_name": args.queue_name, "content": args.content}, sse_url)
    elif args.cmd == "status":
        coro = _call_tool("queue_status", {"queue_name": args.queue_name}, sse_url)
    elif args.cmd == "wait":
        wait_args = {"queue_name": args.queue_name, "timeout_mins": args.timeout_mins}
        if args.reconnect_after_secs is not None:
            wait_args["reconnect_after_secs"] = args.reconnect_after_secs
        coro = _call_tool("wait_for_inbox", wait_args, sse_url)
    else:
        parser.error("unknown command")
        return

    try:
        result = _run_in_own_thread(coro)
    except Exception as e:  # audit-ignore-catch-all: this is the CLI's own
        # top-level error boundary -- it must report whatever the SSE/MCP
        # client call actually raised (connection, timeout, or protocol
        # errors alike) as a clean CLIENT ERROR message and a nonzero exit
        # code, not let a raw traceback escape to the caller's shell.
        _logger.warning("Failed to reach %s: %s", sse_url, e)
        print(f"CLIENT ERROR: could not reach {sse_url}: {e}", file=sys.stderr)
        sys.exit(1)

    print(result)
    sys.exit(0)


if __name__ == "__main__":
    main()
