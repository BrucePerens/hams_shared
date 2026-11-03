#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for lint_test_durations: the split is balanced by measured time, loses no file, repeats exactly, and
estimates a new file from its size."""

import os
import tempfile
import unittest

import lint_test_durations as ltd


class BalancedChunksTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def make(self, name, size):
        path = os.path.join(self._tmp.name, name)
        with open(path, "w") as handle:
            handle.write("x" * size)
        return path

    def test_every_file_is_in_exactly_one_chunk(self):
        files = [self.make(f"test_{i}.py", 10 + i) for i in range(11)]
        chunks = ltd.balanced_chunks(files, 4, {})
        self.assertEqual(sorted(sum(chunks, [])), sorted(files))

    def test_measured_time_beats_file_size(self):
        big_but_fast = self.make("test_big_fast.py", 100_000)
        small_but_slow = self.make("test_small_slow.py", 100)
        others = [self.make(f"test_o{i}.py", 1000) for i in range(2)]
        table = {"test_big_fast.py": 1.0, "test_small_slow.py": 300.0, "test_o0.py": 100.0, "test_o1.py": 100.0}
        chunks = ltd.balanced_chunks([big_but_fast, small_but_slow] + others, 2, table)
        seconds = ltd.estimate([big_but_fast, small_but_slow] + others, table)
        loads = sorted(sum(seconds[f] for f in chunk) for chunk in chunks)
        self.assertEqual(loads, [201.0, 300.0])  # slow alone against the two others plus the fast one

    def test_deterministic(self):
        files = [self.make(f"test_{i}.py", 50 + 7 * i) for i in range(9)]
        table = {os.path.basename(f): float(i) for i, f in enumerate(files)}
        self.assertEqual(ltd.balanced_chunks(files, 3, table), ltd.balanced_chunks(list(reversed(files)), 3, table))

    def test_unmeasured_file_is_estimated_from_its_size(self):
        known = self.make("test_known.py", 1000)
        new = self.make("test_new.py", 3000)
        seconds = ltd.estimate([known, new], {"test_known.py": 10.0})
        self.assertAlmostEqual(seconds[new], 30.0)

    def test_refresh_sums_the_tests_of_a_file(self):
        xml = os.path.join(self._tmp.name, "j.xml")
        with open(xml, "w") as handle:
            handle.write('<testsuites><testsuite>'
                         '<testcase classname="test_a" name="t1" time="1.5"/>'
                         '<testcase classname="tools.test_a.TestX" name="t2" time="2.0"/>'
                         '<testcase classname="test_b" name="t" time="0.25"/></testsuite></testsuites>')
        table = os.path.join(self._tmp.name, "t.json")
        ltd.refresh(xml, table)
        self.assertEqual(ltd.load_table(table), {"test_a.py": 3.5, "test_b.py": 0.25})

    def test_shipped_table_names_real_files(self):
        table = ltd.load_table()
        existing = {os.path.basename(f) for f in os.listdir(ltd.HERE) if f.startswith("test_")}
        self.assertTrue(set(table) <= existing | {"test.py"}, sorted(set(table) - existing))


if __name__ == "__main__":
    unittest.main()
