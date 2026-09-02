#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Automated, assertion-based protocol test for the real, unmodified `rmsgw` binary
(nwdigitalradio/rmsgw, GPL -- see docs/proposals/ARDOP_MERCURY_IMPLEMENTATION_PLAN.md's
Phase 4 status in hams_com for the full history this builds on).

`rmsgw_mock_cms.py` (this same directory) already proved, manually and interactively,
that a real, unmodified `rmsgw` binary can complete its Secure Gateway Login (SGL)
handshake and relay bytes bidirectionally against a mock CMS with no real Winlink
network access, real credentials, or AX.25 kernel support. This file is the "real
automated test later" that script's own docstring said would reuse it: it spawns the
real `rmsgw` binary and the real `rmsgw_mock_cms.py` script as actual subprocesses
communicating over a real loopback TCP socket and real pipes -- nothing here is
simulated at the protocol level -- and asserts specific, checkable outcomes instead of
requiring a human to read printed log lines.

The one qualitatively new thing this file adds beyond the earlier manual proof: an
independent Python re-implementation of `lib/sglchallenge.c`'s `sgl_challenge_response()`
(the MD5-based challenge/response rmsgw computes to authenticate itself to a CMS), so
the test can assert rmsgw's real computed response is *exactly* the cryptographically
correct value for the channel's configured password -- not just "some 8-digit string
was sent and the mock didn't complain." Verified directly against the real C function
while writing this (a standalone C program linked against the real `sglchallenge.c` +
`md5.c` from a fresh clone of nwdigitalradio/rmsgw, called with challenge "ABCD1234"
and password "password", the values `rmsgw_mock_cms.py` and this dev box's installed
`channels.xml` actually use): both the real C function and this Python port return
"55687877" for that input, byte for byte.

**Real, non-obvious finding while re-deriving the algorithm**: `ChallengedPassword()`
in `sglchallenge.c` branches on `#ifdef __BIG_ENDIAN`, apparently intending a runtime
host-endianness check -- but `sglchallenge.c` itself directly `#include`s `<endian.h>`,
and on glibc that header always defines `__BIG_ENDIAN` as a numeric constant (`4321`),
regardless of the actual host's byte order (confirmed directly: `echo '#include
<endian.h>' | gcc -E -dM - | grep BIG_ENDIAN` on this box prints `#define __BIG_ENDIAN
4321` unconditionally), so `#ifdef __BIG_ENDIAN` is unconditionally true on any glibc
Linux build, including this x86_64 (little-endian) box. The code therefore always
takes the "big endian" branch in practice: `byteArr = [digest[0], digest[1], digest[2],
digest[3] & 0x3f]`, then reinterpreted as a native `uint32_t` on this little-endian
host, i.e. `value = digest[0] | (digest[1] << 8) | (digest[2] << 16) | ((digest[3] &
0x3f) << 24)`. This is what `_sgl_challenge_response()` below implements. Functionally
harmless (every real gateway and the real CMS presumably run the same glibc-derived
build, so both sides agree), but worth recording plainly rather than silently
"fixing" the intended-vs-actual behavior mismatch in this from-scratch reimplementation
-- this test exists to match rmsgw's real, shipped behavior, not its comment's intent.

**What this proves**: rmsgw's real SGL login handshake and byte-relay bridge are
protocol-correct against the actual compiled binary, exercised the same way a real
Winlink CMS and a real RF client would exercise them, with a cryptographically exact
assertion on the login response -- genuine, automatable interoperability confirmation
of rmsgw's own side of the RMS protocol.

**What this does NOT prove** (stated plainly, not overclaimed): this is not the full
Phase 4 round-trip test the implementation plan ultimately requires -- a real message
composed and sent through this codebase's own ardopcf/Mercury-backed Pat instance,
arriving intact at a second, independent endpoint. rmsgw never touches ardopcf, Mercury,
Pat, or this daemon's own AX.25/Direwolf path at all; it is a wholly separate, already-
existing open-source program being tested in isolation here, over AX.25's substitute
(stdin/stdout, since ax25d normally spawns it, and this sandbox's kernel has no AX.25
support at all -- confirmed separately). It also does not touch a real Winlink CMS or
real credentials -- `rmsgw_mock_cms.py` stands in for that side, same as before.

Opt-in only, not part of a routine `test.py` sweep: importing this module and running
its `SglChallengeResponseTests` class is always safe (pure function, no I/O), but
`RmsgwProtocolIntegrationTest` additionally requires `RMSGW_INTEGRATION_TEST=1` in the
environment before it will do anything beyond `unittest.skip`. Reason: `rmsgw`'s own
CMS host list file (`/usr/local/etc/rmsgw/hosts`) is a single compiled-in path with no
per-invocation override (`CMSHOSTFILE` is baked in via `pathnames.h` at configure time;
confirmed by reading `lib/getcms.c` -- `setcmsfile()` exists but nothing calls it from
config loading, unlike `CHANNELFILE`, which `session.c` does override via
`setchanfile()`), so this test must temporarily point that real, shared system file at
a local mock CMS and restore it in a cleanup handler. That's appropriate for a
deliberate, opt-in dev-box run -- not something an unattended `pytest`/`test.py` sweep
of every `tools/test_*.py` file should do to a shared system file without being asked.

**Deliberately contains no `sudo` call anywhere in this file**, per this codebase's own
zero-sudo policy (`hams_shared/docs/adrs/`'s standing "no `sudo`/privilege escalation in
committed code, use a narrowly-scoped account instead" rule) -- that rule is about
committed, repo-tracked code specifically, distinct from an operator's own interactive
`sudo` use setting up their own dev box, which this test deliberately doesn't attempt to
do on its own behalf. Instead, `setUp()` checks `os.access(HOSTS_FILE, os.W_OK)` and
calls `self.skipTest(...)` if this user can't write it. **One-time operator setup step,
not part of this test and not automated by it**: `sudo chown "$(whoami)" /usr/local/etc/
rmsgw/hosts` (or an equivalent group-write grant) once, on a box that already has
`rmsgw` installed and where this integration test is wanted -- the file holds only
synthetic test values (a CMS hostname/port/password triple) already meant to be hand-
edited per `rmsgw`'s own README, so ordinary-user ownership of it is not a real
privilege change. `unittest.skipUnless` also requires `shutil.which("rmsgw")` and the
real `/usr/local/etc/rmsgw/channels.xml` to exist, so this is inert on any machine that
doesn't already have rmsgw built and installed (matching this document family's other
opt-in dev-box-only tests).
"""

import hashlib
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import unittest
import xml.etree.ElementTree as ET
from typing import IO

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
MOCK_CMS_SCRIPT = os.path.join(TOOLS_DIR, "rmsgw_mock_cms.py")

HOSTS_FILE = "/usr/local/etc/rmsgw/hosts"
CHANNELS_FILE = "/usr/local/etc/rmsgw/channels.xml"

# The exact 64-byte "winlink salt" from lib/sglchallenge.c's salt[] array (transcribed
# by parsing the real source with a script, not by hand, to avoid a transposition
# error -- see this file's own docstring for how the byte-order branch was verified).
_SALT = bytes([
    77, 197, 101, 206, 190, 249, 93, 200, 51, 243, 93, 237, 71, 94, 239, 138,
    68, 108, 70, 185, 225, 137, 217, 16, 51, 122, 193, 48, 194, 195, 198, 175,
    172, 169, 70, 84, 61, 62, 104, 186, 114, 52, 61, 168, 66, 129, 192, 208,
    187, 249, 232, 193, 41, 113, 41, 45, 240, 16, 29, 228, 208, 228, 61, 20,
])


def sgl_challenge_response(challenge: str, password: str) -> str:
    """Python port of rmsgw's lib/sglchallenge.c sgl_challenge_response()/
    ChallengedPassword(): MD5(challenge + password + salt[:64]), first 4 digest bytes
    reassembled per the (always-taken, see module docstring) __BIG_ENDIAN branch as a
    native little-endian uint32_t with the top byte masked to 6 bits, formatted as a
    zero-padded 10-digit decimal string, and only the last 8 digits kept."""
    digest = hashlib.md5(challenge.encode() + password.encode() + _SALT).digest()
    value = digest[0] | (digest[1] << 8) | (digest[2] << 16) | ((digest[3] & 0x3F) << 24)
    return f"{value:010d}"[2:]


class SglChallengeResponseTests(unittest.TestCase):
    """Pure-function tests -- no rmsgw binary or subprocess needed, always run."""

    def test_matches_the_real_compiled_c_function(self):
        # Verified directly against a standalone C program linked against the real,
        # freshly-cloned lib/sglchallenge.c + lib/md5.c (see module docstring) and,
        # separately, against the real installed rmsgw binary's actual wire output
        # for this exact challenge/password pair.
        self.assertEqual(sgl_challenge_response("ABCD1234", "password"), "55687877")

    def test_response_is_always_eight_digits(self):
        for challenge, password in [("00000000", "x"), ("FFFFFFFF", "a-real-password"),
                                     ("ABCD1234", "")]:
            resp = sgl_challenge_response(challenge, password)
            self.assertEqual(len(resp), 8, resp)
            self.assertTrue(resp.isdigit(), resp)

    def test_different_passwords_give_different_responses(self):
        a = sgl_challenge_response("ABCD1234", "password-one")
        b = sgl_challenge_response("ABCD1234", "password-two")
        self.assertNotEqual(a, b)


def _read_channel_password(channel_name: str) -> str:
    """Read the real configured password for one channel out of the real,
    installed channels.xml -- so this test verifies against whatever this box is
    actually configured with, not a value hard-coded independently of it."""
    ns = {"c": "http://www.namespace.org"}
    root = ET.parse(CHANNELS_FILE).getroot()
    for chan in root.findall("c:channel", ns):
        if chan.get("name") == channel_name:
            password = chan.findtext("c:password", namespaces=ns)
            if password is None:
                raise AssertionError(
                    f"channel {channel_name!r} in {CHANNELS_FILE} has no <password>"
                )
            return password
    raise AssertionError(f"no channel named {channel_name!r} in {CHANNELS_FILE}")


def _pick_free_loopback_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _LineCapture:
    """Reads a subprocess's stdout in a background thread and accumulates it into a
    plain text buffer, so the test can both wait for a pattern to appear and inspect
    the whole transcript afterward -- without the test itself blocking on a partial
    read.

    Deliberately reads raw bytes via os.read() rather than a text-mode readline():
    rmsgw's own wire protocol (like AX.25/RF conventions generally) terminates lines
    with a bare '\\r', not '\\n' or '\\r\\n' -- confirmed directly with strace while
    building this test (`write(1, "MOCK_CMS_HELLO_12345\\r", 21) = 21`, a single,
    complete write with no trailing '\\n'). Python's io.TextIOWrapper, in universal-
    newlines mode, needs one lookahead byte after a bare '\\r' to tell it apart from
    '\\r\\n' -- and when no more bytes are available yet (rmsgw has gone back to
    select()-ing for the next input), `readline()` blocks waiting for that lookahead
    byte instead of returning the already-complete line. That blocking was reproduced
    directly (the marker line sat unreturned by readline() for 25+ seconds although
    strace showed rmsgw had already written it) and is the reason this class exists
    instead of a plain `for line in stream: ...` loop.
    """

    def __init__(self, stream: "IO[bytes]") -> None:
        self._buf = ""
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, args=(stream,), daemon=True)
        self._thread.start()

    def _run(self, stream: "IO[bytes]") -> None:
        fd = stream.fileno()
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            with self._lock:
                self._buf += chunk.decode("utf-8", errors="replace")

    def snapshot(self) -> str:
        with self._lock:
            return self._buf

    def wait_for(self, pattern: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if re.search(pattern, self.snapshot()):
                return True
            time.sleep(0.05)
        return False


@unittest.skipUnless(shutil.which("rmsgw"), "rmsgw binary not installed on this box")
@unittest.skipUnless(os.path.exists(CHANNELS_FILE), f"{CHANNELS_FILE} not installed")
@unittest.skipUnless(os.environ.get("RMSGW_INTEGRATION_TEST") == "1",
                      "opt-in only -- set RMSGW_INTEGRATION_TEST=1 (see module docstring: "
                      "this temporarily rewrites the real, shared "
                      "/usr/local/etc/rmsgw/hosts file, restored afterward)")
class RmsgwProtocolIntegrationTest(unittest.TestCase):
    """Spawns the real rmsgw binary and the real rmsgw_mock_cms.py script as real
    subprocesses, drives one full session between them, and asserts the protocol-level
    outcome at each stage. See module docstring for exactly what this does and does not
    prove about Phase 4 interoperability."""

    GWCALL = "N0CALL-10"
    CHANNEL_NAME = "radio"
    USERCALL = "N0CALL2"

    def setUp(self):
        if not os.access(HOSTS_FILE, os.W_OK):
            self.skipTest(
                f"{HOSTS_FILE} is not writable by this user -- see module docstring's "
                "one-time operator setup step"
            )
        self.password = _read_channel_password(self.CHANNEL_NAME)
        self.port = _pick_free_loopback_port()
        with open(HOSTS_FILE, "r") as f:
            self._original_hosts = f.read()
        self.addCleanup(self._restore_hosts_file)
        self._write_hosts_file(f"127.0.0.1:{self.port}:unused\n")

    def _write_hosts_file(self, content: str):
        with open(HOSTS_FILE, "w") as f:
            f.write(content)

    def _restore_hosts_file(self):
        self._write_hosts_file(self._original_hosts)
        with open(HOSTS_FILE, "r") as f:
            # Fail loudly rather than silently leaving the shared system file wrong --
            # a restore that "succeeds" but doesn't match must not go unnoticed.
            self.assertEqual(f.read(), self._original_hosts,
                              f"{HOSTS_FILE} was not correctly restored after the test")

    def test_full_sgl_login_and_relay_session_against_the_real_binary(self):
        challenge = "ABCD1234"  # hard-coded in rmsgw_mock_cms.py itself
        expected_response = sgl_challenge_response(challenge, self.password)

        mock = subprocess.Popen(
            [sys.executable, MOCK_CMS_SCRIPT, str(self.port)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )

        def _reap(proc):
            proc.kill()
            proc.wait(timeout=5)
            if proc.stdin:
                proc.stdin.close()
            proc.stdout.close()

        self.addCleanup(_reap, mock)
        assert mock.stdout is not None  # guaranteed by stdout=subprocess.PIPE above
        mock_out = _LineCapture(mock.stdout)
        self.assertTrue(mock_out.wait_for(r"mock CMS listening on", timeout=5),
                         f"mock CMS never came up:\n{mock_out.snapshot()}")

        rf_marker = f"RF_CLIENT_MARKER_{os.getpid()}_{self.port}"
        rmsgw = subprocess.Popen(
            ["rmsgw", "-g", self.GWCALL, "-P", self.CHANNEL_NAME, self.USERCALL],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        self.addCleanup(_reap, rmsgw)
        assert rmsgw.stdout is not None  # guaranteed by stdout=subprocess.PIPE above
        rmsgw_out = _LineCapture(rmsgw.stdout)
        assert rmsgw.stdin is not None  # guaranteed by stdin=subprocess.PIPE above
        rmsgw_stdin = rmsgw.stdin

        # 1. rmsgw sends its version/greeting banner to the RF client (stdout)
        #    unconditionally at startup, before any CMS interaction at all -- proven
        #    already in the earlier manual session; re-asserted here as a real,
        #    checkable precondition rather than assumed.
        self.assertTrue(rmsgw_out.wait_for(re.escape(self.GWCALL) + r".*Linux RMS Gateway",
                                            timeout=5),
                         f"no version banner from rmsgw:\n{rmsgw_out.snapshot()}")

        # 2. The real SGL login handshake completes against the mock CMS, and rmsgw's
        #    actual computed challenge response is *exactly* the cryptographically
        #    correct value for this channel's real configured password -- not just
        #    "the mock accepted whatever was sent."
        self.assertTrue(
            mock_out.wait_for(r";SR: \d+ 25000001 20", timeout=10),
            f"rmsgw never sent an SGL challenge response:\n{mock_out.snapshot()}",
        )
        response_match = re.search(r";SR: (\d+) 25000001 20", mock_out.snapshot())
        assert response_match is not None  # just proved above via wait_for()
        sent_response = response_match.group(1)
        self.assertEqual(sent_response, expected_response,
                          "rmsgw's real SGL challenge response did not match the "
                          "independently-computed expected value for this channel's "
                          "actual configured password")

        # 3. gateway()'s byte relay is genuinely bidirectional: a message from the
        #    "CMS" side (mock) arrives at the RF client's real stdout through rmsgw.
        self.assertTrue(mock_out.wait_for(r"entering gateway\(\) byte-relay phase", timeout=5),
                         f"mock CMS log:\n{mock_out.snapshot()}")
        self.assertTrue(rmsgw_out.wait_for("MOCK_CMS_HELLO_12345", timeout=5),
                         f"CMS->RF relay marker never reached rmsgw's stdout:\n"
                         f"{rmsgw_out.snapshot()}")

        # 4. ...and a reply typed by the "RF client" (this test, writing to rmsgw's
        #    stdin) arrives back at the mock CMS's socket through the same relay,
        #    proving the relay really is bidirectional and not a one-way echo.
        rmsgw_stdin.write((rf_marker + "\r").encode())
        rmsgw_stdin.flush()
        self.assertTrue(mock_out.wait_for(re.escape(rf_marker), timeout=5),
                         f"RF->CMS relay marker never reached the mock CMS:\n"
                         f"{mock_out.snapshot()}")

        # 4b. rmsgw_mock_cms.py's own scripted sequence (see that file) sends its own
        #     "FF" right after receiving our reply, then blocks on one more line from
        #     the RF client before it closes the CMS socket -- send that closing line
        #     (matching gateway.c's real FF/FQ turnaround: having seen the uplink's FF,
        #     the downlink replies FQ to end the conversation cleanly) so the mock can
        #     finish and close, which is what actually makes rmsgw's own recv() see a
        #     clean EOF and exit 0 below (confirmed directly while building this test:
        #     rmsgw's own real exit path here is "the far end closed the socket," not a
        #     strict FF/FQ state-machine match on rmsgw's own side).
        self.assertTrue(mock_out.wait_for(r"SEND: 'FF", timeout=5),
                         f"mock CMS log:\n{mock_out.snapshot()}")
        rmsgw_stdin.write(b"FQ\r")
        rmsgw_stdin.flush()

        # 5. Clean FF/FQ session teardown (gateway.c's turnaround sequence) and a
        #    clean process exit -- not a hang, crash, or timeout-driven kill.
        try:
            rc = rmsgw.wait(timeout=10)
        except subprocess.TimeoutExpired:
            rmsgw.kill()
            rmsgw.wait(timeout=5)
            self.fail(f"rmsgw did not exit after FF/FQ teardown:\n{rmsgw_out.snapshot()}")
        self.assertEqual(rc, 0, f"rmsgw exited {rc}, not 0:\n{rmsgw_out.snapshot()}")
        self.assertTrue(mock_out.wait_for(r"mock CMS done", timeout=5),
                         f"mock CMS log:\n{mock_out.snapshot()}")


if __name__ == "__main__":
    unittest.main()
