#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Fake-network-object-in-tests gate (Rust and Python).

Born 2026-09-11 from the same night's real-network-integration-test work: `hams_relay_bridge`
and `dx_firehose` both had zero tests that actually exercised their own WebSocket routing --
every prior test either checked pure extracted logic directly, or mocked Odoo's HTTP responses,
never the daemon's OWN network layer. The fix, both times, was the same pattern: bind a REAL
listener to an ephemeral port (`TcpListener::bind("127.0.0.1:0")` / `websockets.serve(...)`),
connect a REAL client to it, and mock ONLY genuine external boundaries (a fake Odoo HTTP server,
`unittest.mock.patch` on `get_odoo_client`) -- never the daemon's own routing/serialization/
framing code via a duck-typed stand-in object. A hand-rolled fake that never binds a real socket
can silently diverge from the real wire protocol (message framing, auth handshake ordering, JSON
shape) in exactly the way a real bound listener plus a real client cannot.

This gate flags the anti-pattern textually: a struct/class defined inside test code whose name
looks like it's standing in for a network primitive (Fake/Mock/Dummy/Stub + Socket/Listener/
Stream/Connection/WebSocket/Channel/Transport), in a FILE that contains no evidence anywhere of
a real bound listener (`TcpListener::bind`, `.bind(`, `axum::serve`, `HTTPServer`/`socketserver`,
`websockets.serve`, `serve_forever`). A fake type that DOES wrap a real bound socket (e.g.
ham_shack/tests/test_shack_sw_behavior_tour.py's own `_FakeLocalRelayHandler`, a real
`http.server.BaseHTTPRequestHandler` standing in for a real device that only ever exists on the
operator's own LAN) is exactly the encouraged pattern, not this one -- the real-bind-evidence
check exists specifically to tell the two apart.

Purely textual, not a real AST/type analysis -- same tradeoff as check_burn_list.py's own
GENERAL_ERROR_RULES throughout this codebase. False positives are possible (a "Fake"-named type
that has nothing to do with networking at all, e.g. a `FakeClock`) -- suppress a specific
definition with a same-line `// fake-network-ignore: <reason>` (Rust) or `# fake-network-ignore:
<reason>` (Python) comment rather than disabling the whole file.
"""

import os
import re
import sys

IGNORE_DIR_NAMES = {
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    "target",
    ".git",
    "worktrees",
}

_FAKE_TYPE_RE = re.compile(
    r"\b(?:struct|class)\s+((?:Fake|Mock|Dummy|Stub)\w*"
    r"(?:Socket|Listener|Stream|Connection|WebSocket|Ws|Channel|Transport))\b"
)
_REAL_BIND_RE = re.compile(
    r"TcpListener::bind|axum::serve|\.bind\(\s*[\"'(]|HTTPServer\(|socketserver\.|"
    r"websockets\.serve\(|serve_forever\("
)
_IGNORE_RE = re.compile(r"fake-network-ignore\s*:\s*\S")
_TEST_PATH_RE = re.compile(r"(?:^|/)tests?/|(?:^|/)test_[^/]*\.py$|_test\.py$")


def find_candidate_files(repo_root):
    """Rust files anywhere (a fake network type usually lives in a `#[cfg(test)]` module
    inside the same file as the code it tests, not a separate tests/ directory) and Python
    files that look like tests by path or name."""
    found = []
    for root, dirs, filenames in os.walk(repo_root):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIR_NAMES and not d.startswith(".")]
        for fname in filenames:
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, repo_root)
            if fname.endswith(".rs"):
                found.append(full)
            elif fname.endswith(".py") and _TEST_PATH_RE.search(rel.replace(os.sep, "/")):
                found.append(full)
    return sorted(found)


def check_file(path):
    """Returns a list of (line_num, message) findings for one file."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()

    if _REAL_BIND_RE.search(content):
        return []

    findings = []
    for i, line in enumerate(content.splitlines()):
        if _IGNORE_RE.search(line):
            continue
        m = _FAKE_TYPE_RE.search(line)
        if m:
            findings.append((
                i + 1,
                f"`{m.group(1)}` looks like a hand-rolled fake standing in for a network "
                f"primitive, but this file contains no real bound listener anywhere "
                f"(TcpListener::bind / axum::serve / websockets.serve / HTTPServer / "
                f"socketserver / .bind() / serve_forever). Mocking a daemon's OWN network "
                f"layer via a duck-typed object hides real wire-protocol bugs (framing, auth "
                f"handshake ordering, JSON shape) that only a real bound listener plus a real "
                f"connecting client can catch -- see hams_relay_bridge's own "
                f"real_network_integration_tests / dx_firehose's RealWebsocketServerTests for "
                f"the established pattern. If this fake genuinely isn't standing in for real "
                f"wire traffic, add `// fake-network-ignore: <reason>` (or `#` in Python) on "
                f"this line.",
            ))
    return findings


def main():
    if len(sys.argv) < 2:
        print("Usage: check_fake_network_listener.py <repo_root>")
        sys.exit(1)

    repo_root = os.path.abspath(sys.argv[1])
    if os.path.basename(repo_root) == "hams_shared":
        repo_root = os.path.dirname(repo_root)

    any_found = False
    for path in find_candidate_files(repo_root):
        for line_num, msg in check_file(path):
            any_found = True
            rel = os.path.relpath(path, repo_root)
            print(f"❌ {rel}:{line_num}: {msg}")

    if any_found:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
