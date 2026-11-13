# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests [@ANCHOR: atomic_io:atomic_write]"""

import os
import tempfile
import unittest
import unittest.mock

import atomic_io


class AtomicWriteTests(unittest.TestCase):
    def test_writes_text_and_bytes(self):
        # Tests [@ANCHOR: atomic_io:atomic_write]
        with tempfile.TemporaryDirectory() as directory:
            target = os.path.join(directory, "state.json")
            atomic_io.atomic_write(target, "one")
            self.assertEqual(open(target, "rb").read(), b"one")
            atomic_io.atomic_write(target, b"two", mode=0o644)
            self.assertEqual(open(target, "rb").read(), b"two")
            self.assertEqual(os.stat(target).st_mode & 0o777, 0o644)
            self.assertEqual(os.listdir(directory), ["state.json"])

    def test_flushes_file_and_directory_to_disk(self):
        # Tests [@ANCHOR: atomic_io:atomic_write]
        with tempfile.TemporaryDirectory() as directory:
            with unittest.mock.patch("os.fsync") as fsync:
                atomic_io.atomic_write(os.path.join(directory, "a"), "x")
            self.assertEqual(fsync.call_count, 2)

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_failure_before_rename_keeps_old_file(self):
        # Tests [@ANCHOR: atomic_io:atomic_write]
        with tempfile.TemporaryDirectory() as directory:
            target = os.path.join(directory, "state.json")
            atomic_io.atomic_write(target, "old")
            os.chmod(directory, 0o555)
            try:
                with self.assertRaises(OSError):
                    atomic_io.atomic_write(target, "new")
            finally:
                os.chmod(directory, 0o755)
            self.assertEqual(open(target).read(), "old")


if __name__ == "__main__":
    unittest.main()
