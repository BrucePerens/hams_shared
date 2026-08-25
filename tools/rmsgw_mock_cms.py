#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
rmsgw mock CMS
-------------------------------------------------------------------------------------------
Minimal mock Winlink CMS server implementing just enough of rmsgw's own "Secure Gateway
Login" (SGL) handshake (see nwdigitalradio/rmsgw's lib/cmslogin.c sglProcess()) to let a
real, unmodified rmsgw binary complete cmsConnect() and reach gateway()'s byte-relay phase --
with no real Winlink network access and no real Winlink credentials.

Real rmsgw sessions bridge an RF client (fed to rmsgw's own stdin/stdout -- rmsgw never
touches an AX.25 socket directly, ax25d/inetd normally does that job) to a live outbound
socket connection to a real Winlink CMS host. This script stands in for that CMS host so
rmsgw's real login and relay logic can be exercised and verified without depending on
Winlink's live production network or a real operator's real account credentials --
see docs/proposals/ARDOP_MERCURY_IMPLEMENTATION_PLAN.md's Phase 4 status for the full
writeup of what this proved.

Usage: point rmsgw's hosts file (/usr/local/etc/rmsgw/hosts, format
"hostname:port:password") at 127.0.0.1:<port>, run this script first, then run
`rmsgw -g <gwcall> -P <channel-name> <usercall>` with stdin/stdout as the RF client side.
"""

import socket
import sys

HOST = "127.0.0.1"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8772

srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind((HOST, PORT))
srv.listen(1)
print(f"mock CMS listening on {HOST}:{PORT}", flush=True)

conn, addr = srv.accept()
print(f"accepted connection from {addr}", flush=True)

def send(s):
    print(f"SEND: {s!r}", flush=True)
    conn.sendall(s.encode())

def recv_line():
    buf = b""
    while not buf.endswith(b"\r"):
        chunk = conn.recv(1)
        if not chunk:
            print("RECV: peer closed connection (EOF)", flush=True)
            raise ConnectionError("peer closed before sending expected line")
        buf += chunk
    print(f"RECV: {buf!r}", flush=True)
    return buf.decode(errors="replace")

# Step 1: send the login prompt rmsgw's sglProcess() looks for.
# Needs a trailing \r -- rmsgw's readln() reads until it sees the
# terminator byte and blocks (never processing the line) without one.
send("Callsign :\r")

# Step 2: rmsgw sends "{usercall} {gwcall}\r"
login_line = recv_line()

# Step 3: send a challenge -- rmsgw parses ";SQ: XXXXXXXX" (8 chars).
send(";SQ: ABCD1234\r")

# Step 4: rmsgw sends ";SR: {response} 25000001 20\r" -- sglProcess()
# returns SUCCEED right after sending this, without waiting for our
# reply, so we don't need to validate it for the login to "succeed" on
# rmsgw's side.
response_line = recv_line()

print("SGL login handshake complete -- entering gateway() byte-relay phase", flush=True)

# Step 5: now rmsgw's gateway() is a pure byte relay between this socket
# and the RF client's stdin/stdout. Send a distinctive marker so we can
# confirm it really arrives at the RF-client side through rmsgw's relay.
send("MOCK_CMS_HELLO_12345\r")

# Read whatever the RF client (via rmsgw's relay) sends back, to prove
# the relay works in both directions.
reply = recv_line()
print(f"got reply via rmsgw's relay: {reply!r}", flush=True)

# End the session cleanly (FF/FQ turnaround gateway.c watches for).
send("FF\r")
end_line = recv_line()
print(f"final line from client: {end_line!r}", flush=True)

conn.close()
srv.close()
print("mock CMS done", flush=True)
