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
import datetime
import logging
import os
import sys
import threading

import httpx
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.shared.exceptions import McpError

_logger = logging.getLogger("mcp_watchdog_client")

# Seconds allowed for the MCP handshake (`initialize`) and for any tool call
# that is meant to answer promptly. `send` and `status` both do server-side
# work measured in milliseconds; anything past this is a wedged server, not a
# slow one.
HANDSHAKE_TIMEOUT_SECS = 30
QUICK_CALL_TIMEOUT_SECS = 30

# Added on top of a `wait`'s own server-side `timeout_mins` before the client
# gives up. The server is supposed to return its own timeout result at
# `timeout_mins`; the client's deadline exists only to catch a server that
# never answers at all, so it must sit strictly outside the legitimate one or
# it would cut off real waits.
WAIT_TIMEOUT_MARGIN_SECS = 60

# Added on top of the per-request deadline for the whole-call deadline. It has
# to be nonzero so the per-request timeout, which can name the request it gave
# up on, always fires first when the server is merely unresponsive.
OVERALL_TIMEOUT_MARGIN_SECS = 15


class WatchdogClientTimeout(Exception):
    """The whole call exceeded its deadline before any single request did.

    Raised by the outer deadline in `_call_tool`, which exists because the
    per-request deadline cannot see the legs that come before a request:
    `sse_client()` only returns once the server has sent its `endpoint` event,
    so a server that accepts the TCP connection, returns a `text/event-stream`
    response, and then says nothing at all never reaches `initialize()` -- and
    was bounded only by the transport's own `sse_read_timeout`.
    """


def _default_sse_url():
    return os.environ.get("MCP_WATCHDOG_SSE_URL", "http://127.0.0.1:8767/sse")


async def _call_tool(tool_name, arguments, sse_url, call_timeout_secs=QUICK_CALL_TIMEOUT_SECS):
    """Call one watchdog tool, with a real deadline on every leg of the call.

    Every timeout here is explicit because each default is wrong for this
    client:

    * `ClientSession(read_timeout_seconds=...)` and
      `call_tool(read_timeout_seconds=...)` both default to `None`, which
      means "never time out". A server that keeps the SSE stream alive with
      traffic but never answers this particular request hung the client
      forever -- the exact failure a CLI called from an unattended agent
      session must not have. The session-level value covers `initialize`;
      the per-request value passed to `call_tool` takes precedence over it
      (see `mcp.shared.session.BaseSession.send_request`), so a long `wait`
      does not also give the handshake a long deadline.
    * `sse_client(sse_read_timeout=...)` defaults to 300s, which is SHORTER
      than a legitimate `wait --timeout-mins 15` (900s). Left alone, a long
      wait died on the transport's own read timeout well before the server
      was due to answer. It is raised to sit outside the call deadline for
      the same reason the call deadline sits outside the server's own.

    The outer `asyncio.wait_for` is not redundant with the per-request
    deadline. `sse_client()` only returns once the server has sent its
    `endpoint` event, so a server that accepts the connection, returns a
    `text/event-stream` response and then says nothing never reaches
    `initialize()` at all -- no request is in flight, and nothing the
    `ClientSession` knows about is being waited on. Its margin is deliberately
    the larger of the two so that when a request IS in flight, the per-request
    timeout fires first and its message can name which one.
    """
    for k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"]:
        os.environ.pop(k, None)
    overall_timeout_secs = call_timeout_secs + OVERALL_TIMEOUT_MARGIN_SECS

    async def _do():
        async with sse_client(sse_url, sse_read_timeout=overall_timeout_secs) as (read, write):
            async with ClientSession(
                read,
                write,
                read_timeout_seconds=datetime.timedelta(seconds=HANDSHAKE_TIMEOUT_SECS),
            ) as session:
                await session.initialize()
                res = await session.call_tool(
                    tool_name,
                    arguments=arguments,
                    read_timeout_seconds=datetime.timedelta(seconds=call_timeout_secs),
                )
                return res.content[0].text if res.content else str(res)

    try:
        return await asyncio.wait_for(_do(), timeout=overall_timeout_secs)
    except asyncio.TimeoutError:
        raise WatchdogClientTimeout(
            f"no response from {sse_url} within {overall_timeout_secs}s "
            f"(tool {tool_name!r})"
        )


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
    parser.add_argument(
        "--timeout-secs",
        type=int,
        default=None,
        help=(
            "Override the client's own deadline for this call, in seconds. "
            "Defaults to %ds for 'send' and 'status', and to --timeout-mins "
            "plus %ds for 'wait'. Raise it on a slow link; lower it when a "
            "caller would rather fail fast than block."
            % (QUICK_CALL_TIMEOUT_SECS, WAIT_TIMEOUT_MARGIN_SECS)
        ),
    )
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
        coro = _call_tool(
            "send_ipc_message",
            {"queue_name": args.queue_name, "content": args.content},
            sse_url,
            call_timeout_secs=args.timeout_secs or QUICK_CALL_TIMEOUT_SECS,
        )
    elif args.cmd == "status":
        coro = _call_tool(
            "queue_status",
            {"queue_name": args.queue_name},
            sse_url,
            call_timeout_secs=args.timeout_secs or QUICK_CALL_TIMEOUT_SECS,
        )
    elif args.cmd == "wait":
        wait_args = {"queue_name": args.queue_name, "timeout_mins": args.timeout_mins}
        if args.reconnect_after_secs is not None:
            wait_args["reconnect_after_secs"] = args.reconnect_after_secs
        # Derived from the caller's own `--timeout-mins` rather than a fixed
        # constant: the server is expected to answer at `timeout_mins` with
        # its own timeout result (a valid result, exit 0), so the client's
        # deadline only has to be later than that.
        coro = _call_tool(
            "wait_for_inbox",
            wait_args,
            sse_url,
            call_timeout_secs=(
                args.timeout_secs
                or args.timeout_mins * 60 + WAIT_TIMEOUT_MARGIN_SECS
            ),
        )
    else:
        parser.error("unknown command")
        return

    try:
        result = _run_in_own_thread(coro)
    except WatchdogClientTimeout as e:  # The whole-call deadline (see _call_tool).
        _logger.warning("Timed out on %s at %s: %s", args.cmd, sse_url, e)
        print(f"CLIENT ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    except McpError as e:  # A request deadline expired (see _call_tool).
        # Split out from the catch-all below only to say something true: on a
        # timeout the server WAS reachable and did answer the handshake, it
        # just never answered this request. "could not reach" would send a
        # reader looking for a network fault that isn't there. Any other
        # McpError still exits 1 with the generic message -- this branch
        # never re-raises, because a raw traceback escaping to the caller's
        # shell is exactly what this boundary exists to prevent.
        if e.error.code == httpx.codes.REQUEST_TIMEOUT:
            _logger.warning("Timed out waiting for %s at %s: %s", args.cmd, sse_url, e)
            print(
                f"CLIENT ERROR: {sse_url} accepted the connection but did not answer "
                f"'{args.cmd}' in time: {e}",
                file=sys.stderr,
            )
        else:
            _logger.warning("Failed to reach %s: %s", sse_url, e)
            print(f"CLIENT ERROR: could not reach {sse_url}: {e}", file=sys.stderr)
        sys.exit(1)
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
