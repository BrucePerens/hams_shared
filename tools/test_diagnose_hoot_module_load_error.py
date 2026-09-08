#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for diagnose_hoot_module_load_error.py.

Only the pure, deterministic helpers are covered here (patch content
construction, stack-capture parsing, free-port allocation) -- main()'s own
orchestration shells out to a real sudo-launched Odoo process against a real
systemd service and is meant to be run by hand against a live dev box, the
same way verify_full_pipeline_end_to_end.py isn't wrapped in its own pytest
suite either.
"""

import os
import socket
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import diagnose_hoot_module_load_error as diag  # noqa: E402


class PatchModuleLoaderContentTests(unittest.TestCase):
    def test_injects_a_stack_capture_console_error_right_after_the_catch_target(self):
        original = (
            "            try {\n"
            "                module = factory.fn(require);\n"
            "            } catch (error) {\n"
            "                this.failed.add(name);\n"
            "                throw new Error(`Error while loading \"${name}\":\\n${error}`);\n"
            "            }\n"
        )
        patched = diag.patch_module_loader_content(original)
        self.assertIn("STACK_CAPTURE_FOR_", patched)
        self.assertIn("this.failed.add(name);", patched)
        # The injected line must come after the real add(name) call, not before it.
        self.assertLess(
            patched.index("this.failed.add(name);"), patched.index("STACK_CAPTURE_FOR_")
        )
        # The original throw must still be present and unmodified.
        self.assertIn('throw new Error(`Error while loading "${name}":\\n${error}`);', patched)

    def test_raises_when_the_catch_target_is_missing_entirely(self):
        with self.assertRaises(ValueError):
            diag.patch_module_loader_content("some completely different file content")

    def test_raises_when_the_catch_target_appears_more_than_once(self):
        original = "this.failed.add(name);\n" * 2
        with self.assertRaises(ValueError):
            diag.patch_module_loader_content(original)


class ExtractStackCapturesTests(unittest.TestCase):
    def test_no_markers_returns_an_empty_list(self):
        self.assertEqual(diag.extract_stack_captures("nothing to see here\nall clean\n"), [])

    def test_a_single_capture_with_stack_frames_is_extracted(self):
        output = (
            "2026-09-08 16:25:27,050 ERROR browser: STACK_CAPTURE_FOR_@barcodes/barcode_service: "
            "TypeError: Illegal invocation\n"
            "    at isIOS (http://127.0.0.1:8070/web/assets/x/bundle.min.js:5357:131)\n"
            "    at isMobileOS (http://127.0.0.1:8070/web/assets/x/bundle.min.js:5361:75)\n"
            "2026-09-08 16:25:27,051 INFO something unrelated\n"
        )
        captures = diag.extract_stack_captures(output)
        self.assertEqual(len(captures), 1)
        self.assertIn("STACK_CAPTURE_FOR_@barcodes/barcode_service", captures[0])
        self.assertIn("at isIOS", captures[0])
        self.assertIn("at isMobileOS", captures[0])
        self.assertNotIn("unrelated", captures[0])

    def test_two_separate_captures_are_both_extracted(self):
        output = (
            "STACK_CAPTURE_FOR_@a/mod: Error: first\n"
            "    at f (bundle.js:1:1)\n"
            "log line in between\n"
            "STACK_CAPTURE_FOR_@b/mod: Error: second\n"
            "    at g (bundle.js:2:2)\n"
        )
        captures = diag.extract_stack_captures(output)
        self.assertEqual(len(captures), 2)
        self.assertIn("@a/mod", captures[0])
        self.assertIn("@b/mod", captures[1])

    def test_a_capture_with_no_stack_frames_still_yields_its_header_line(self):
        output = "STACK_CAPTURE_FOR_@x/mod: TypeError: no frames follow\nnext unrelated line\n"
        captures = diag.extract_stack_captures(output)
        self.assertEqual(captures, ["STACK_CAPTURE_FOR_@x/mod: TypeError: no frames follow"])


class GetFreePortTests(unittest.TestCase):
    def test_returns_a_port_that_is_actually_bindable_right_after(self):
        port = diag.get_free_port()
        self.assertGreater(port, 0)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))  # must not raise

    def test_two_calls_can_return_different_ports(self):
        # Not a strict guarantee (the OS could reuse one immediately after it's freed), but in
        # practice back-to-back calls return different ports -- real behavior, not assumed.
        ports = {diag.get_free_port() for _ in range(5)}
        self.assertGreater(len(ports), 1)


if __name__ == "__main__":
    unittest.main()
