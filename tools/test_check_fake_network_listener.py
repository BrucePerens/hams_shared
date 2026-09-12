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

from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_fake_network_listener as chk  # noqa: E402


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


# 8 distinct, genuinely-matching fake-network type names (one real prefix + real suffix
# combination each) for property tests generating N independent type definitions -- NOT an
# f-string with a numeric index glued onto the name (e.g. "FakeStream0"): the trailing digit
# breaks _FAKE_TYPE_RE's own trailing `\b` requirement after the suffix word ("Stream0" doesn't
# end in a word boundary right after "Stream"), which is correct regex behavior, not a bug in
# check_fake_network_listener.py -- discovered by an earlier, broken version of these very
# property tests.
_FAKE_TYPE_NAMES = [
    "FakeSocket", "MockListener", "DummyStream", "StubConnection",
    "FakeWebSocket", "MockWs", "DummyChannel", "StubTransport",
]


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

    @given(num_types=st.integers(min_value=0, max_value=8))
    @settings(max_examples=50)
    def test_n_fake_types_with_no_real_bind_produce_exactly_n_findings(self, num_types):
        # Generalizes test_a_fake_websocket_struct_with_no_real_bind_anywhere_is_flagged (fixed
        # at 1) across an arbitrary count of independently-named fake network types stacked in
        # one file, including zero (no fake types at all -> no findings).
        content = "".join(
            f"struct {_FAKE_TYPE_NAMES[i]} {{ data: Vec<u8> }}\n" for i in range(num_types)
        )
        path = _write(os.path.join(self.tmp, "src", "lib.rs"), content or "// empty\n")
        findings = chk.check_file(path)
        self.assertEqual(
            len(findings),
            num_types,
            f"expected exactly {num_types} findings for {num_types} unignored fake network "
            f"types with no real bind evidence, got {len(findings)}: {findings}",
        )

    @given(
        num_types=st.integers(min_value=0, max_value=5),
        bind_position=st.integers(min_value=0, max_value=5),
    )
    @settings(max_examples=50)
    def test_real_bind_evidence_anywhere_in_the_file_exempts_every_fake_type(
        self, num_types, bind_position
    ):
        # Generalizes test_a_real_tcplistener_bind_anywhere_in_the_file_exempts_it: check_file's
        # own real-bind check runs once over the WHOLE file content, so its position relative to
        # however many fake types are also present must never matter -- before all of them,
        # after all of them, or interleaved partway through.
        lines = [f"struct {_FAKE_TYPE_NAMES[i]} {{ data: Vec<u8> }}\n" for i in range(num_types)]
        lines.insert(
            min(bind_position, len(lines)),
            'async fn spawn() { TcpListener::bind("127.0.0.1:0").await.unwrap(); }\n',
        )
        path = _write(os.path.join(self.tmp, "src", "lib.rs"), "".join(lines))
        self.assertEqual(
            chk.check_file(path),
            [],
            f"expected zero findings for {num_types} fake types with a real bind at position "
            f"{bind_position}",
        )

    @given(ignored_flags=st.lists(st.booleans(), min_size=0, max_size=8))
    @settings(max_examples=50)
    def test_ignore_tagged_types_are_excluded_but_untagged_siblings_still_flag(self, ignored_flags):
        # Generalizes test_a_suppression_comment_exempts_that_line: an arbitrary mix of
        # tagged/untagged fake-type definitions in the same file must suppress exactly the
        # tagged ones, never more (an untagged sibling must still be caught) and never fewer
        # (a tagged one must never leak through).
        lines = []
        for i, is_ignored in enumerate(ignored_flags):
            suffix = " // fake-network-ignore: reviewed, not real" if is_ignored else ""
            lines.append(f"struct {_FAKE_TYPE_NAMES[i]} {{ data: Vec<u8> }}{suffix}\n")
        path = _write(os.path.join(self.tmp, "src", "lib.rs"), "".join(lines) or "// empty\n")
        findings = chk.check_file(path)
        expected = sum(1 for f in ignored_flags if not f)
        self.assertEqual(
            len(findings),
            expected,
            f"expected {expected} findings (one per untagged fake type) for ignore pattern "
            f"{ignored_flags}, got {len(findings)}: {findings}",
        )


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
