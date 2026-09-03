#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Real subprocess test for `rmsgw_callsign_handoff_wrapper.py`.

Spawns the real wrapper script as an actual subprocess (matching this
directory's own `test_rmsgw_protocol.py` convention of testing real
subprocess behavior rather than mocking Python internals), feeds it a
callsign line on stdin exactly the way LinBPQ's Applications Interface
would, and stands in for the real `rmsgw` binary with a tiny fake script on
`PATH` that just records the argv it was `execvp`'d with -- so this test
proves the wrapper's real, observable `execvp` behavior rather than the
wrapper's own internal function calls.
"""

import os
import stat
import subprocess
import sys
import tempfile
import unittest

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
WRAPPER_SCRIPT = os.path.join(TOOLS_DIR, "rmsgw_callsign_handoff_wrapper.py")

_FAKE_RMSGW = """#!/usr/bin/env python3
import os
import sys
with open(os.environ["ARGV_CAPTURE_FILE"], "w") as f:
    f.write("\\n".join(sys.argv[1:]))
"""


class RmsgwCallsignHandoffWrapperTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        fake_rmsgw_path = os.path.join(self._tmpdir.name, "rmsgw")
        with open(fake_rmsgw_path, "w") as f:
            f.write(_FAKE_RMSGW)
        os.chmod(fake_rmsgw_path, os.stat(fake_rmsgw_path).st_mode | stat.S_IEXEC)
        self._argv_capture_file = os.path.join(self._tmpdir.name, "argv_capture")
        self._env = dict(os.environ)
        self._env["PATH"] = self._tmpdir.name + os.pathsep + self._env.get("PATH", "")
        self._env["ARGV_CAPTURE_FILE"] = self._argv_capture_file

    def _run_wrapper(self, gwcall, channel, stdin_line):
        argv = [sys.executable, WRAPPER_SCRIPT]
        if gwcall is not None:
            argv.append(gwcall)
        if channel is not None:
            argv.append(channel)
        return subprocess.run(
            argv,
            input=stdin_line,
            capture_output=True,
            text=True,
            env=self._env,
            timeout=10,
        )

    def test_a_real_callsign_line_is_handed_to_rmsgw_as_usercall(self):
        result = self._run_wrapper("N0CALL-10", "radio", "N0CALL2\r\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(self._argv_capture_file) as f:
            captured = f.read()
        self.assertEqual(captured, "-g\nN0CALL-10\n-P\nradio\nN0CALL2")

    def test_a_callsign_with_an_ssid_survives_intact(self):
        result = self._run_wrapper("N0CALL-10", "radio", "W1AW-5\r\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(self._argv_capture_file) as f:
            captured = f.read()
        self.assertIn("W1AW-5", captured.splitlines())

    def test_a_lowercase_callsign_is_uppercased(self):
        result = self._run_wrapper("N0CALL-10", "radio", "n0call2\r\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(self._argv_capture_file) as f:
            captured = f.read()
        self.assertIn("N0CALL2", captured.splitlines())

    def test_a_malformed_stdin_line_is_rejected_and_rmsgw_is_never_invoked(self):
        result = self._run_wrapper("N0CALL-10", "radio", "'; rm -rf /\r\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(os.path.exists(self._argv_capture_file))

    def test_an_empty_stdin_line_is_rejected_and_rmsgw_is_never_invoked(self):
        result = self._run_wrapper("N0CALL-10", "radio", "\r\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(os.path.exists(self._argv_capture_file))

    def test_missing_arguments_are_a_usage_error_and_rmsgw_is_never_invoked(self):
        result = self._run_wrapper("N0CALL-10", None, "N0CALL2\r\n")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(os.path.exists(self._argv_capture_file))


if __name__ == "__main__":
    unittest.main()
