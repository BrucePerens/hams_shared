#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for scratch_janitor.py: real directories, real ages, a real live process."""

import os
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scratch_janitor as sj  # noqa: E402


def age(path, hours):
    past = time.time() - hours * 3600
    os.utime(path, (past, past), follow_symlinks=False)


class SweepStaleScratchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = os.path.join(self.tmp.name, "slot")
        os.mkdir(self.base)

    def make(self, name, hours, is_dir=True):
        path = os.path.join(self.base, name)
        if is_dir:
            os.mkdir(path)
            with open(os.path.join(path, "f"), "w") as handle:
                handle.write("x")
        else:
            open(path, "w").close()
        age(path, hours)
        return path

    # Tests [@ANCHOR: scratch_janitor:sweep_stale_scratch]
    def test_removes_old_entries_and_keeps_recent_ones(self):
        old, new, old_file = self.make("bst-old", 20), self.make("bst-new", 1), self.make("tmpold", 20, is_dir=False)
        removed = sj.sweep_stale_scratch(self.base, texts=[], say=lambda _m: None)
        self.assertEqual(sorted(removed), sorted([old, old_file]))
        self.assertFalse(os.path.exists(old) or os.path.exists(old_file))
        self.assertTrue(os.path.isdir(new))

    def test_keeps_an_old_entry_a_process_references_and_matches_literally(self):
        held, other = self.make("bst-held", 20), self.make("bst-held2", 20)
        removed = sj.sweep_stale_scratch(self.base, texts=[f"postgres -D {held}/live"], say=lambda _m: None)
        self.assertEqual(removed, [other])  # `bst-held` is a prefix of `bst-held2`'s name, not the other way round
        self.assertTrue(os.path.isdir(held))
        dotted, plain = self.make("a.c", 20), self.make("abc", 20)
        removed = sj.sweep_stale_scratch(self.base, texts=[plain], say=lambda _m: None)  # a pattern `a.c` would match `abc`
        self.assertEqual(sorted(removed), sorted([dotted, held]))
        self.assertTrue(os.path.isdir(plain))

    def test_sees_a_really_running_process_by_its_working_directory(self):
        held = self.make("cwd-held", 20)
        proc = subprocess.Popen(["sleep", "30"], cwd=held)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        removed = sj.sweep_stale_scratch(self.base, say=lambda _m: None)
        self.assertNotIn(held, removed)
        self.assertTrue(os.path.isdir(held))

    def test_leaves_other_slot_directories_and_never_follows_a_link(self):
        slot2 = self.make("slot-2", 20)
        outside = os.path.join(self.tmp.name, "outside")
        os.mkdir(outside)
        with open(os.path.join(outside, "keep"), "w"):
            pass
        link = os.path.join(self.base, "link")
        os.symlink(outside, link)
        age(link, 20)
        removed = sj.sweep_stale_scratch(self.base, texts=[], say=lambda _m: None)
        self.assertEqual(removed, [link])
        self.assertTrue(os.path.isdir(slot2))
        self.assertTrue(os.path.isfile(os.path.join(outside, "keep")))

    def test_ignores_a_missing_a_relative_and_a_linked_directory(self):
        self.assertEqual(sj.sweep_stale_scratch(os.path.join(self.base, "nope"), texts=[]), [])
        self.assertEqual(sj.sweep_stale_scratch("relative", texts=[]), [])
        self.assertEqual(sj.sweep_stale_scratch("/tmp/", texts=[]), [])
        link = os.path.join(self.tmp.name, "linkdir")
        os.symlink(self.base, link)
        old = self.make("bst-old", 20)
        self.assertEqual(sj.sweep_stale_scratch(link, texts=[]), [])
        self.assertTrue(os.path.isdir(old))


if __name__ == "__main__":
    unittest.main()
