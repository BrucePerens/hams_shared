#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
LinBPQ callsign-handoff wrapper for rmsgw
------------------------------------------------------------------------------
Real, buildable piece of `docs/proposals/SOFTWARE_AX25_STACK.md`'s decided
architecture: Direwolf (KISS-over-TCP) -> LinBPQ (real, off-the-shelf,
GPL-3.0, run as a separate process -- never linked) -> `inetd` (or an
equivalent local TCP listener/spawner) -> this wrapper -> unmodified `rmsgw`.

**The problem this bridges**: LinBPQ's own "Applications Interface" hands an
incoming node connection to an external program via `inetd` and sends the
connecting station's callsign as a single line on stdin before the session's
real traffic begins (confirmed against G8BPQ's own "LinBPQ Applications
Interface" documentation -- see `SOFTWARE_AX25_STACK.md`'s "RESOLVED,
2026-09-03" section). `rmsgw`'s own CLI contract instead takes the usercall
as a positional argv argument (`rmsgw -g <gwcall> -P <channel> <usercall>`,
confirmed directly against the real binary -- see
`hams_shared/tools/test_rmsgw_protocol.py`), and everything else about the
session (stdin/stdout, `\\r`-terminated lines, the SGL login handshake) is
otherwise exactly what `inetd`/`ax25d` already hand it unmodified. This
script is the one line of real glue needed: read LinBPQ's one callsign line,
validate it, then `execvp` straight into the real, unmodified `rmsgw`
binary with that value as `usercall`.

Usage (as an `inetd`/`bpq32.cfg` APPLICATION line target):

    rmsgw_callsign_handoff_wrapper.py <gateway-callsign> <channel-name>

`<gateway-callsign>` and `<channel-name>` are this deployment's own fixed
configuration (matching `-g`/`-P` in the existing `rmsgw` integration test);
only the per-connection `usercall` comes from LinBPQ's stdin handoff.
"""

import os
import re
import sys

RMSGW_BINARY = "rmsgw"

# Real amateur-radio callsign shape: a 1-2 letter/digit prefix, at least one
# digit, and a 1-3 letter suffix, with an optional "-SSID" (1-2 digits).
# Deliberately permissive enough for real-world calls (including special-
# event and foreign prefixes) while rejecting anything that isn't
# callsign-shaped. `os.execvp` below passes this as a literal argv element,
# never through a shell, so there is no command-injection surface as such --
# but a malformed, empty, or garbage line here would otherwise reach
# rmsgw's own `usercall` argument completely unchecked, and LinBPQ's stdin
# handoff is the one place in this whole chain an untrusted remote RF
# station's own input reaches this wrapper before rmsgw itself takes over.
_CALLSIGN_RE = re.compile(r"^[A-Z0-9]{2,8}(-[0-9]{1,2})?$")


def main(argv):
    if len(argv) != 3:
        sys.stderr.write(f"usage: {argv[0]} <gateway-callsign> <channel-name>\n")
        return 2
    gwcall, channel = argv[1], argv[2]

    # [@ANCHOR: linbpq_callsign_handoff]
    # This one line is LinBPQ's own handoff of the connecting station's
    # callsign (see module docstring). Everything LinBPQ/inetd hands this
    # process's stdin afterward is the real session's own AX.25 traffic and
    # must reach rmsgw's stdin completely unconsumed -- so this must read
    # exactly one line, no more.
    line = sys.stdin.readline()
    usercall = line.strip().upper()

    if not _CALLSIGN_RE.match(usercall):
        sys.stderr.write(
            f"rejecting connection: {usercall!r} does not look like a callsign\n"
        )
        return 1

    # execvp, not subprocess.run: this process's stdin/stdout are already
    # the live AX.25 session LinBPQ/inetd handed us -- rmsgw must inherit
    # them directly, not through a new pipe, and this wrapper has nothing
    # left to do afterward. execvp only returns on failure.
    os.execvp(RMSGW_BINARY, [RMSGW_BINARY, "-g", gwcall, "-P", channel, usercall])
    sys.stderr.write(f"failed to exec {RMSGW_BINARY}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
