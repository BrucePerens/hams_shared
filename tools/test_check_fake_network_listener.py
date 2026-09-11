#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_fake_network_listener.py, exercised against real temp-directory fixtures.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_fake_network_listener as chk  # noqa: E402


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


class CheckFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_fake_websocket_struct_with_no_real_bind_anywhere_is_flagged(self):
        path = _write(
            os.path.join(self.tmp, "src", "lib.rs"),
            "struct FakeWebSocketConnection { responses: Vec<String> }\n",
        )
        findings = chk.check_file(path)
        self.assertEqual(len(findings), 1)
        self.assertIn("FakeWebSocketConnection", findings[0][1])

    def test_a_fake_socket_class_with_no_real_bind_anywhere_is_flagged_in_python(self):
        path = _write(
            os.path.join(self.tmp, "test_thing.py"),
            "class MockSocketConnection:\n    def send(self, data):\n        pass\n",
        )
        findings = chk.check_file(path)
        self.assertEqual(len(findings), 1)

    def test_a_fake_type_in_a_file_that_also_binds_a_real_listener_is_not_flagged(self):
        # The encouraged pattern (e.g. ham_shack's own _FakeLocalRelayHandler): the "fake"
        # part is a real HTTPServer request handler, standing in for a real device on the
        # operator's own LAN, not a duck-typed object with no real socket at all.
        path = _write(
            os.path.join(self.tmp, "test_thing.py"),
            "class FakeSocketHandler(BaseHTTPRequestHandler):\n"
            "    pass\n"
            "server = HTTPServer(('127.0.0.1', 0), FakeSocketHandler)\n",
        )
        self.assertEqual(chk.check_file(path), [])

    def test_a_real_tcplistener_bind_anywhere_in_the_file_exempts_it(self):
        path = _write(
            os.path.join(self.tmp, "src", "lib.rs"),
            "struct MockStream { data: Vec<u8> }\n"
            "async fn spawn() { TcpListener::bind(\"127.0.0.1:0\").await.unwrap(); }\n",
        )
        self.assertEqual(chk.check_file(path), [])

    def test_a_websockets_serve_call_anywhere_in_the_file_exempts_it(self):
        path = _write(
            os.path.join(self.tmp, "test_thing.py"),
            "class FakeChannel:\n    pass\n"
            "async def run():\n    async with websockets.serve(handler, 'localhost', 0):\n        pass\n",
        )
        self.assertEqual(chk.check_file(path), [])

    def test_a_suppression_comment_exempts_that_line(self):
        path = _write(
            os.path.join(self.tmp, "src", "lib.rs"),
            "struct FakeListener { } // fake-network-ignore: not actually network-related\n",
        )
        self.assertEqual(chk.check_file(path), [])

    def test_a_fake_type_unrelated_to_networking_is_not_matched(self):
        # "FakeClock" doesn't end in any network-primitive-shaped suffix -- deliberately
        # narrow to avoid flooding on the many legitimate non-network fakes/mocks/stubs any
        # real test suite has.
        path = _write(
            os.path.join(self.tmp, "src", "lib.rs"),
            "struct FakeClock { now: u64 }\n",
        )
        self.assertEqual(chk.check_file(path), [])


class FindCandidateFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rust_files_are_always_candidates(self):
        _write(os.path.join(self.tmp, "src", "main.rs"), "// x\n")
        found = chk.find_candidate_files(self.tmp)
        self.assertEqual(len(found), 1)

    def test_a_non_test_shaped_python_file_is_not_a_candidate(self):
        _write(os.path.join(self.tmp, "models", "thing.py"), "# x\n")
        self.assertEqual(chk.find_candidate_files(self.tmp), [])

    def test_a_test_prefixed_python_file_is_a_candidate(self):
        _write(os.path.join(self.tmp, "tests", "test_thing.py"), "# x\n")
        found = chk.find_candidate_files(self.tmp)
        self.assertEqual(len(found), 1)


if __name__ == "__main__":
    unittest.main()
