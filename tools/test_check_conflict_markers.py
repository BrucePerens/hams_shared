#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_conflict_markers.py.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import check_conflict_markers as chk  # noqa: E402

_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "check_conflict_markers.py")

CONFLICT_BLOCK = "\n".join([
    "keep this line",
    "<<<<<<< HEAD",
    "ours",
    "=======",
    "theirs",
    ">>>>>>> topic",
    "end",
]) + "\n"
MARKDOWN_HEADING = "\n".join(["Title", "=======", "", "Body text.", ""])
CODE_SPAN_MENTION = "Use `<<<<<<< ` and `>>>>>>> ` to spot a conflict in a diff.\n"


class FindConflictsTests(unittest.TestCase):
    def test_real_conflict_block_is_found(self):
        found = chk.find_conflicts(CONFLICT_BLOCK.splitlines(True))
        self.assertEqual(
            found,
            [(2, "<<<<<<< HEAD"), (4, "======="), (6, ">>>>>>> topic")],
        )

    def test_markdown_heading_underlined_with_equals_passes(self):
        self.assertEqual(chk.find_conflicts(MARKDOWN_HEADING.splitlines(True)), [])

    def test_marker_text_inside_a_code_span_passes(self):
        self.assertEqual(chk.find_conflicts(CODE_SPAN_MENTION.splitlines(True)), [])


class CheckConflictMarkersTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", self.tmp], check=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _track(self, name, content):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        subprocess.run(["git", "-C", self.tmp, "add", name], check=True)

    def test_tracked_conflict_block_fails(self):
        self._track("notes.txt", CONFLICT_BLOCK)
        violations = chk.check_conflict_markers(self.tmp)
        self.assertEqual(len(violations), 3)
        self.assertTrue(violations[0].startswith("notes.txt:2 <<<<<<< HEAD"))

    def test_tracked_markdown_heading_passes(self):
        self._track("README.md", MARKDOWN_HEADING)
        self.assertEqual(chk.check_conflict_markers(self.tmp), [])

    def test_tracked_code_span_mention_passes(self):
        self._track("docs.md", CODE_SPAN_MENTION)
        self.assertEqual(chk.check_conflict_markers(self.tmp), [])

    def test_binary_tracked_file_is_skipped(self):
        path = os.path.join(self.tmp, "blob.bin")
        with open(path, "wb") as f:
            f.write(b"\xff\xfe\x00<<<<<<< \x81")
        subprocess.run(["git", "-C", self.tmp, "add", "blob.bin"], check=True)
        self.assertEqual(chk.check_conflict_markers(self.tmp), [])

    def test_command_line_exit_status(self):
        self._track("notes.txt", CONFLICT_BLOCK)
        bad = subprocess.run([sys.executable, _SCRIPT, self.tmp], capture_output=True, text=True, check=False)
        self.assertEqual(bad.returncode, 1)
        self.assertIn("notes.txt:2 <<<<<<< HEAD", bad.stdout)
        subprocess.run(["git", "-C", self.tmp, "rm", "-q", "-f", "notes.txt"], check=True)
        self._track("README.md", MARKDOWN_HEADING)
        good = subprocess.run([sys.executable, _SCRIPT, self.tmp], capture_output=True, text=True, check=False)
        self.assertEqual(good.returncode, 0, good.stdout + good.stderr)


if __name__ == "__main__":
    unittest.main()
