#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Property tests (hypothesis) for small pure parsers: range header, domain, slot count, size, bool, lint stamp."""

import unittest

from hypothesis import given, settings
from hypothesis import strategies as st

import agent_procs
import known_red
import lint_stamp_gate
import parking_ctl
import runner_slots
import static_site_server
import tenant_lib


class ParseRangeProperties(unittest.TestCase):
    @settings(max_examples=300, deadline=None)
    @given(st.text(max_size=40), st.integers(min_value=0, max_value=10**6))
    def test_never_raises_and_result_is_inside_the_file(self, header, size):
        result = static_site_server.parse_range(header, size)
        if isinstance(result, tuple):
            start, end = result
            self.assertTrue(0 <= start <= end < size)
        else:
            self.assertIn(result, (None, "bad"))

    @settings(max_examples=300, deadline=None)
    @given(st.integers(0, 10**20), st.integers(0, 10**20), st.integers(0, 10**6))
    def test_well_formed_ranges_are_satisfiable_or_bad(self, first, last, size):
        result = static_site_server.parse_range(f"bytes={first}-{last}", size)
        if first >= size or first > last:
            self.assertEqual(result, "bad")
        else:
            self.assertEqual(result, (first, min(last, size - 1)))

    @settings(max_examples=300, deadline=None)
    @given(st.integers(1, 10**20), st.integers(0, 10**6))
    def test_suffix_range(self, length, size):
        result = static_site_server.parse_range(f"bytes=-{length}", size)
        if size:
            self.assertEqual(result, (max(0, size - length), size - 1))


class NormalizeDomainProperties(unittest.TestCase):
    @settings(max_examples=300, deadline=None)
    @given(st.text(max_size=60))
    def test_only_raises_spec_error_and_result_is_idempotent(self, raw):
        try:
            result = tenant_lib.normalize_domain(raw)
        except tenant_lib.SpecError:
            return
        self.assertEqual(result, result.lower())
        self.assertTrue(result.isascii())
        self.assertEqual(tenant_lib.normalize_domain(result), result)
        self.assertLessEqual(len(result), 253)
        self.assertGreaterEqual(len(result.split(".")), 2)


class SlotCountProperties(unittest.TestCase):
    @settings(max_examples=300, deadline=None)
    @given(st.text(max_size=12))
    def test_only_slot_config_error_and_in_range(self, text):
        try:
            count = runner_slots.parse_slot_count(text, "x")
        except runner_slots.SlotConfigError:
            return
        self.assertTrue(1 <= count <= runner_slots.MAX_SLOTS)

    @given(st.integers(1, runner_slots.MAX_SLOTS), st.sampled_from(["", " ", "\n"]))
    def test_round_trip(self, count, pad):
        self.assertEqual(runner_slots.parse_slot_count(f"{pad}{count}{pad}", "x"), count)


class ParseSizeProperties(unittest.TestCase):
    @settings(max_examples=300, deadline=None)
    @given(st.text(max_size=12))
    def test_only_proc_error_and_non_negative(self, text):
        try:
            value = agent_procs.parse_size(text)
        except agent_procs.ProcError:
            return
        self.assertGreaterEqual(value, 0)

    @given(st.integers(0, 10**6), st.sampled_from(["", "k", "m", "g", "t"]), st.sampled_from(["", "b"]))
    def test_units(self, number, unit, suffix):
        factor = 1024 ** ("kmgt".index(unit) + 1) if unit else 1
        self.assertEqual(agent_procs.parse_size(f"{number}{unit.upper()}{suffix}"), number * factor)


class ParseBoolProperties(unittest.TestCase):
    @settings(max_examples=300, deadline=None)
    @given(st.text(max_size=8))
    def test_only_input_error(self, text):
        try:
            self.assertIsInstance(parking_ctl.parse_bool(text), bool)
        except parking_ctl.InputError:
            pass


class ParseStampProperties(unittest.TestCase):
    @settings(max_examples=300, deadline=None)
    @given(st.text(max_size=80))
    def test_never_raises(self, text):
        mode, count, names = lint_stamp_gate.parse_stamp(text)
        self.assertIsInstance(mode, str)
        self.assertTrue(count is None or count >= 0)
        self.assertTrue(names is None or isinstance(names, set))

    @given(st.sampled_from(["full", "changed"]), st.integers(0, 500),
           st.sets(st.from_regex(r"[a-z_]{1,12}", fullmatch=True), max_size=6))
    def test_round_trip(self, mode, count, names):
        text = f"{mode} steps={count} names={','.join(sorted(names))}"
        self.assertEqual(lint_stamp_gate.parse_stamp(text), (mode, count, names or set()))


class ParseDomainFileProperties(unittest.TestCase):
    @settings(max_examples=300, deadline=None)
    @given(st.text(max_size=200))
    def test_never_raises_and_entries_are_normalized(self, text):
        entries, problems = parking_ctl.parse_domain_file(text)
        self.assertIsInstance(problems, list)
        for domain, values in entries.items():
            self.assertEqual(tenant_lib.normalize_domain(domain), domain)
            self.assertIn(values["behavior"], parking_ctl.BEHAVIORS)
            self.assertTrue(0 <= values["cache_ttl"] <= 31536000)


class KnownRedLineProperties(unittest.TestCase):
    @settings(max_examples=300, deadline=None)
    @given(st.text(max_size=200))
    def test_line_parsers_never_raise(self, line):
        record = known_red.parse_failure_line(line)
        self.assertTrue(record is None or ":" in record.test_id)
        started = known_red.parse_started_line(line)
        self.assertTrue(started is None or ":" in started)


if __name__ == "__main__":
    unittest.main()
