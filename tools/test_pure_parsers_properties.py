#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Hypothesis property tests for pure parsers and formatters in agent_procs.py, lock_queue.py and overnight_common.py.
Few examples (CI default), no I/O, no real processes.

Tests [@ANCHOR: lock_queue:format_duration]
Tests [@ANCHOR: agent_procs:parse_size]
Tests [@ANCHOR: agent_procs:parse_start_time]
Tests [@ANCHOR: agent_procs:chrome_data_dir]
Tests [@ANCHOR: overnight_common:slug]
"""
import unittest

from hypothesis import given, settings
from hypothesis import strategies as st

import agent_procs
import lock_queue
import overnight_common

FEW = settings(max_examples=30, deadline=None)
NUMBERS = st.integers(min_value=0, max_value=10 ** 12)


class LockQueueProperties(unittest.TestCase):
    @given(NUMBERS, NUMBERS, NUMBERS)
    @FEW
    def test_ticket_name_round_trips(self, seq, pid, start):
        self.assertEqual(lock_queue._parse_name("%d-%d-%d" % (seq, pid, start)), (seq, pid, start))

    @given(st.integers(min_value=-10 ** 6, max_value=10 ** 9))
    @FEW
    def test_format_duration_is_one_of_three_shapes(self, seconds):
        self.assertRegex(lock_queue.format_duration(seconds), r"\A(\d+ s|\d+ min|\d+ h \d\d min)\Z")

    @given(st.integers(min_value=0, max_value=10 ** 9))
    @FEW
    def test_format_duration_never_goes_backwards_in_unit(self, seconds):
        order = {"s": 0, "min": 1, "h": 2}
        first = order[lock_queue.format_duration(seconds).split()[1]]
        second = order[lock_queue.format_duration(seconds + 1).split()[1]]
        self.assertLessEqual(first, second)


class AgentProcsProperties(unittest.TestCase):
    @given(NUMBERS, st.sampled_from(["", "k", "m", "g", "t"]), st.sampled_from(["", "b"]), st.booleans())
    @FEW
    def test_parse_size_matches_binary_units(self, number, unit, suffix, upper):
        text = "%d%s%s" % (number, unit, suffix)
        text = text.upper() if upper else text
        self.assertEqual(agent_procs.parse_size(text), number * (1024 ** ("kmgt".index(unit) + 1) if unit else 1))

    @given(st.text())
    @FEW
    def test_parse_size_only_raises_proc_error(self, text):
        try:
            agent_procs.parse_size(text)
        except agent_procs.ProcError:
            pass

    @given(NUMBERS, st.sampled_from(["", "ticks="]))
    @FEW
    def test_parse_start_time_ticks_is_a_one_tick_range(self, ticks, prefix):
        self.assertEqual(agent_procs.parse_start_time("%s%d" % (prefix, ticks)), (ticks, ticks + 1))

    @given(st.integers(min_value=0, max_value=10 ** 15))
    @FEW
    def test_format_start_always_returns_text(self, ticks):
        self.assertIsInstance(agent_procs.format_start(ticks), str)

    @given(st.lists(st.text(max_size=20), max_size=6), st.text(max_size=20))
    @FEW
    def test_chrome_data_dir_finds_the_value_after_the_first_flag(self, args, value):
        args = [a for a in args if not a.startswith("--user-data-dir=")]
        self.assertIsNone(agent_procs.chrome_data_dir(args))
        self.assertEqual(agent_procs.chrome_data_dir(args + ["--user-data-dir=" + value, "--user-data-dir=other"]), value)


class OvernightCommonProperties(unittest.TestCase):
    @given(st.text(), st.integers(min_value=1, max_value=80))
    @FEW
    def test_slug_is_safe_non_empty_and_bounded(self, text, limit):
        result = overnight_common.slug(text, limit)
        self.assertRegex(result, r"\A[A-Za-z0-9._-]+\Z")
        self.assertLessEqual(len(result), limit)

    @given(st.text(alphabet="abcXYZ019._-", min_size=1, max_size=48))
    @FEW
    def test_slug_of_a_clean_name_keeps_it(self, text):
        clean = text.strip("-._")
        if clean:
            self.assertEqual(overnight_common.slug(clean), clean)


if __name__ == "__main__":
    unittest.main()
