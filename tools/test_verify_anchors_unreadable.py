#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for verify_anchors.py's handling of files the user running it may not read: one finding per file naming the
file and the fix, exit status UNREADABLE_EXIT_CODE, never a traceback or an "anchor violations" verdict.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import verify_anchors as va  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def _write(path, content, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    os.chmod(path, mode)


@unittest.skipIf(os.geteuid() == 0, "root reads mode-000 files")
class UnreadableFileTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        _write(os.path.join(self.root, "mod", "README.md"), "# mod\n")
        self.locked_doc = os.path.join(self.root, "docs", "HANDOVER.md")
        self.locked_code = os.path.join(self.root, "mod", "models", "thing.py")
        _write(self.locked_doc, "notes\n")
        _write(self.locked_code, "x = 1\n")
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        # git records the files; only then does the mode drop (a Write-tool file is mode 600, git says 644).
        os.chmod(self.locked_doc, 0o000)
        os.chmod(self.locked_code, 0o000)
        va.UNREADABLE_FILES.clear()
        self.addCleanup(va.UNREADABLE_FILES.clear)

    def test_the_doc_and_code_scans_record_each_file_instead_of_raising(self):
        va.find_anchors_in_docs(self.root, self.root)
        va.find_anchors_in_code(self.root, self.root)
        recorded = sorted(path for path, _error in va.UNREADABLE_FILES)
        self.assertEqual(recorded, sorted([self.locked_doc, self.locked_code]))

    def test_the_command_line_scan_names_the_files_and_exits_with_the_distinct_status(self):
        result = subprocess.run(
            [sys.executable, os.path.join(HERE, "verify_anchors.py"), self.root], capture_output=True, text=True
        )
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(result.returncode, va.UNREADABLE_EXIT_CODE, result.stdout + result.stderr)
        for path in (self.locked_doc, self.locked_code):
            self.assertIn(f"chmod 644 {path}", result.stdout)
        self.assertIn("not anchor violations", result.stdout)

    def test_the_preflight_lists_every_unreadable_tracked_file(self):
        found = va.unreadable_tracked_files([self.root, os.path.join(self.root, "not-a-checkout")])
        self.assertEqual(found, sorted([self.locked_doc, self.locked_code]))
        os.chmod(self.locked_doc, 0o644)
        self.assertEqual(va.unreadable_tracked_files([self.root]), [self.locked_code])

    def test_the_message_names_file_user_and_fix(self):
        message = va.unreadable_message("/a/b.md", "odoo")
        self.assertIn("/a/b.md", message)
        self.assertIn("'odoo'", message)
        self.assertIn("chmod 644 /a/b.md", message)


if __name__ == "__main__":
    unittest.main()
