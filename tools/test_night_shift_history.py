#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for night_shift_history.py: the one-file-per-entry layout of the night-shift log, its entry splitter, its
linter and its command line, against real temporary directories.
"""

import datetime
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

import night_shift_history as nsh

_TOOL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "night_shift_history.py")


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


GOOD_NAME = "2026-11-09-101500-fix-the-thing-0a1b2c.md"
GOOD_TEXT = "### 2026-11-09 -- Fix the thing\n\n- changed it\n- verified it\n"


class SplitEntriesTests(unittest.TestCase):
    """Tests [@ANCHOR: tools:night_shift_history_split_entries]
    Tests [@ANCHOR: tools:night_shift_history_heading_text]"""

    def test_the_parts_always_add_up_to_the_original_text(self):
        text = (
            "### 2026-11-08 -- One\n\nbody one\n\n"
            "## Old style heading\n\nbody two\n\n\n"
            "### 2026-11-07 -- Three\n\nbody three\n"
        )
        preamble, entries = nsh.split_entries(text)
        self.assertEqual(preamble, "")
        self.assertEqual(len(entries), 3)
        self.assertEqual(preamble + "".join(entries), text)
        self.assertTrue(entries[0].startswith("### 2026-11-08 -- One"))
        self.assertTrue(entries[1].endswith("body two\n\n\n"))

    def test_a_heading_wrapped_over_two_lines_is_one_entry(self):
        text = "## A long title that was\n## wrapped by hand\n\nbody\n\n## Next\n\nbody\n"
        _, entries = nsh.split_entries(text)
        self.assertEqual(len(entries), 2)
        self.assertEqual(nsh.heading_text(entries[0]), "A long title that was wrapped by hand")
        self.assertEqual(nsh.heading_text(entries[1]), "Next")

    def test_sub_headings_and_headings_inside_code_fences_are_body(self):
        text = (
            "## Entry\n\n### Verification\n\n# Handoff\n\n```\n## not an entry\n### 2026-01-01 -- nor this\n```\n\n"
            "### Undated sub heading\n"
        )
        _, entries = nsh.split_entries(text)
        self.assertEqual(len(entries), 1)

    def test_text_before_the_first_entry_is_returned_as_the_preamble(self):
        preamble, entries = nsh.split_entries("intro line\n\n## Entry\n\nbody\n")
        self.assertEqual(preamble, "intro line\n\n")
        self.assertEqual(len(entries), 1)
        self.assertEqual(nsh.split_entries("no headings at all\n"), ("no headings at all\n", []))

    def test_heading_text_of_text_that_is_not_an_entry_is_empty(self):
        self.assertEqual(nsh.heading_text("plain text\n"), "")
        self.assertEqual(nsh.heading_text(""), "")


class SlugTests(unittest.TestCase):
    """Tests [@ANCHOR: tools:night_shift_history_slug]"""

    def test_plain_titles_become_lower_case_hyphenated_words(self):
        self.assertEqual(nsh.slugify("Fix the `foo()` bug (to-do 1a2b)"), "fix-the-foo-bug-to-do-1a2b")

    def test_a_leading_date_and_non_ascii_text_are_dropped(self):
        self.assertEqual(nsh.slugify("2026-11-08 -- Premise check"), "premise-check")
        self.assertEqual(nsh.slugify("éè"), "entry")
        self.assertEqual(nsh.slugify("---"), "entry")

    def test_a_long_title_is_cut_at_a_hyphen_within_the_limit(self):
        slug = nsh.slugify("word " * 40)
        self.assertLessEqual(len(slug), nsh.SLUG_MAX)
        self.assertFalse(slug.endswith("-"))
        self.assertTrue(nsh.NAME_PATTERN.match(f"2026-11-09-101500-{slug}-0a1b2c.md"))
        self.assertLessEqual(len(nsh.slugify("x" * 200)), nsh.SLUG_MAX)


class EntryProblemsTests(unittest.TestCase):
    """Tests [@ANCHOR: tools:night_shift_history_entry_format]
    Tests [@ANCHOR: tools:night_shift_history_entry_problems]"""

    def test_a_well_formed_entry_has_no_problems(self):
        self.assertEqual(nsh.entry_problems(GOOD_NAME, GOOD_TEXT), [])

    def test_a_bad_file_name_is_refused(self):
        for name in (
            "night_shift_history.md",
            "2026-11-09-1015-fix-0a1b2c.md",
            "2026-11-09-101500-Fix-0a1b2c.md",
            "2026-11-09-101500-fix-0A1B2C.md",
            "2026-11-09-101500-fix-0a1b2c.txt",
            "2026-11-09-101500-fix.md",
        ):
            with self.subTest(name=name):
                self.assertEqual(len(nsh.entry_problems(name, GOOD_TEXT)), 1)
        self.assertIn("calendar date", nsh.entry_problems("2026-13-45-101500-fix-0a1b2c.md", GOOD_TEXT)[0])

    def test_a_new_entry_needs_a_real_time_of_day(self):
        problems = nsh.entry_problems("2026-11-09-256100-fix-0a1b2c.md", GOOD_TEXT)
        self.assertEqual(len(problems), 1)
        self.assertIn("time of day", problems[0])

    def test_a_new_entry_must_start_with_the_dated_heading_and_the_date_must_match_the_name(self):
        self.assertTrue(nsh.entry_problems(GOOD_NAME, "## Fix the thing\n\nbody\n"))
        self.assertTrue(nsh.entry_problems(GOOD_NAME, "### 2026-11-10 -- Fix the thing\n\nbody\n"))
        self.assertTrue(nsh.entry_problems(GOOD_NAME, "### 2026-11-09 -- Fix the thing\nbody right under it\n"))
        self.assertTrue(nsh.entry_problems(GOOD_NAME, "### 2026-11-09 -- Fix the thing\n\n"))

    def test_newline_rules_and_forbidden_bytes(self):
        self.assertTrue(nsh.entry_problems(GOOD_NAME, GOOD_TEXT.rstrip("\n")))
        self.assertTrue(nsh.entry_problems(GOOD_NAME, GOOD_TEXT + "\n"))
        self.assertTrue(nsh.entry_problems(GOOD_NAME, GOOD_TEXT.replace("\n", "\r\n")))
        self.assertTrue(nsh.entry_problems(GOOD_NAME, GOOD_TEXT + "\0\n"))

    def test_one_file_is_one_entry(self):
        problems = nsh.entry_problems(GOOD_NAME, GOOD_TEXT + "\n## A second entry\n\nbody\n")
        self.assertEqual(len(problems), 1)
        self.assertIn("holds 2 entries", problems[0])
        self.assertTrue(nsh.entry_problems(GOOD_NAME, "text first\n" + GOOD_TEXT))

    def test_an_entry_migrated_from_the_old_file_keeps_any_heading_it_had(self):
        legacy = "2026-09-05-000001-stale-header-sweep-aaaaaa.md"
        self.assertEqual(nsh.entry_problems(legacy, "## Fortieth addendum: something\n## wrapped\n\nbody\n"), [])
        self.assertTrue(nsh.entry_problems(legacy, "body without any heading\n"))


class CheckTreeTests(unittest.TestCase):
    """Tests [@ANCHOR: tools:night_shift_history_check_tree]
    Tests [@ANCHOR: tools:night_shift_history_stub_text]"""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)

    def _entry(self, name=GOOD_NAME, text=GOOD_TEXT):
        _write(os.path.join(self.root, nsh.DIRECTORY, name), text)

    def test_the_old_single_file_layout_is_accepted_whatever_it_holds(self):
        self.assertEqual(nsh.check_tree(self.root), [])
        _write(os.path.join(self.root, nsh.OLD_FILE), "### 2026-11-08 -- anything\n\nbody\n")
        _write(os.path.join(self.root, nsh.OLD_ARCHIVE, "part_01", "night_shift_history.md"), "## old\n")
        self.assertEqual(nsh.check_tree(self.root), [])

    def test_the_directory_layout_with_the_stub_and_good_entries_passes(self):
        _write(os.path.join(self.root, nsh.OLD_FILE), nsh.STUB_TEXT)
        self._entry()
        _write(os.path.join(self.root, nsh.DIRECTORY, nsh.DIRECTORY_README), "# How this directory works\n")
        self.assertEqual(nsh.check_tree(self.root), [])

    def test_appending_to_the_old_file_after_the_move_fails_loudly(self):
        self._entry()
        _write(os.path.join(self.root, nsh.OLD_FILE), "### 2026-11-09 -- stale entry\n\nbody\n" + nsh.STUB_TEXT)
        problems = nsh.check_tree(self.root)
        self.assertEqual(len(problems), 1)
        self.assertIn("stub", problems[0])
        self.assertIn("night_shift_history.py new", problems[0])

    def test_a_missing_old_file_or_a_surviving_archive_is_a_problem(self):
        self._entry()
        self.assertEqual(len(nsh.check_tree(self.root)), 1)
        _write(os.path.join(self.root, nsh.OLD_FILE), nsh.STUB_TEXT)
        _write(os.path.join(self.root, nsh.OLD_ARCHIVE, "part_01", "night_shift_history.md"), "## old\n")
        problems = nsh.check_tree(self.root)
        self.assertEqual(len(problems), 1)
        self.assertIn(nsh.OLD_ARCHIVE, problems[0])

    def test_every_bad_entry_file_is_reported_with_its_path(self):
        _write(os.path.join(self.root, nsh.OLD_FILE), nsh.STUB_TEXT)
        self._entry()
        self._entry("2026-11-09-101501-bad-0a1b2c.md", "no heading\n")
        self._entry("notes.txt", "x\n")
        os.makedirs(os.path.join(self.root, nsh.DIRECTORY, "subdir"))
        os.symlink(GOOD_NAME, os.path.join(self.root, nsh.DIRECTORY, "2026-11-09-101502-link-0a1b2c.md"))
        with open(os.path.join(self.root, nsh.DIRECTORY, "2026-11-09-101503-binary-0a1b2c.md"), "wb") as handle:
            handle.write(b"\xff\xfe\n")
        problems = nsh.check_tree(self.root)
        self.assertTrue(all(p.startswith(nsh.DIRECTORY + "/") for p in problems))
        reported = {p.split(": ")[0] for p in problems}
        self.assertEqual(
            reported,
            {f"{nsh.DIRECTORY}/{n}" for n in (
                "2026-11-09-101501-bad-0a1b2c.md", "2026-11-09-101502-link-0a1b2c.md",
                "2026-11-09-101503-binary-0a1b2c.md", "notes.txt", "subdir",
            )},
        )


class LatestEntriesTests(unittest.TestCase):
    """Tests [@ANCHOR: tools:night_shift_history_latest]"""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_the_directory_layout_returns_the_newest_file_names_first(self):
        for day in ("09", "10", "11"):
            _write(
                os.path.join(self.root, nsh.DIRECTORY, f"2026-11-{day}-100000-entry-{day}-0a1b2c.md"),
                f"### 2026-11-{day} -- Entry {day}\n\nbody\n",
            )
        _write(os.path.join(self.root, nsh.DIRECTORY, nsh.DIRECTORY_README), "# readme\n")
        found = nsh.latest_entries(self.root, 2)
        self.assertEqual([name[:10] for name, _ in found], ["2026-11-11", "2026-11-10"])
        self.assertEqual(len(nsh.latest_entries(self.root, 50)), 3)

    def test_the_old_layout_returns_the_top_of_the_single_file(self):
        _write(os.path.join(self.root, nsh.OLD_FILE), "### 2026-11-08 -- A\n\nx\n\n### 2026-11-07 -- B\n\ny\n")
        found = nsh.latest_entries(self.root, 1)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0][1].startswith("### 2026-11-08 -- A"))

    def test_nothing_to_read_and_a_bad_count_are_errors(self):
        with self.assertRaises(FileNotFoundError):
            nsh.latest_entries(self.root, 3)
        with self.assertRaises(ValueError):
            nsh.latest_entries(self.root, 0)


class NewEntryTests(unittest.TestCase):
    """Tests [@ANCHOR: tools:night_shift_history_new_entry]"""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        os.makedirs(os.path.join(self.root, nsh.DIRECTORY))
        self.now = datetime.datetime(2026, 11, 9, 14, 3, 22)

    def test_it_writes_a_well_formed_file_named_for_the_time_and_title(self):
        _write(os.path.join(self.root, nsh.OLD_FILE), nsh.STUB_TEXT)
        path = nsh.new_entry(self.root, "Fix  the thing\n", "- changed it\n\n", now=self.now)
        name = os.path.basename(path)
        self.assertTrue(name.startswith("2026-11-09-140322-fix-the-thing-"), name)
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "### 2026-11-09 -- Fix the thing\n\n- changed it\n")
        self.assertEqual(nsh.check_tree(self.root), [])

    def test_two_entries_made_in_the_same_second_get_different_names(self):
        first = nsh.new_entry(self.root, "Same", "a", now=self.now)
        second = nsh.new_entry(self.root, "Same", "b", now=self.now)
        self.assertNotEqual(first, second)
        self.assertEqual(len(os.listdir(os.path.join(self.root, nsh.DIRECTORY))), 2)

    def test_a_body_that_would_start_a_second_entry_or_is_empty_is_refused_and_nothing_is_written(self):
        with self.assertRaises(ValueError):
            nsh.new_entry(self.root, "Title", "text\n\n## Another heading\n\nmore", now=self.now)
        with self.assertRaises(ValueError):
            nsh.new_entry(self.root, "Title", "   \n", now=self.now)
        self.assertEqual(os.listdir(os.path.join(self.root, nsh.DIRECTORY)), [])

    def test_the_date_can_be_given_and_the_old_layout_is_refused(self):
        path = nsh.new_entry(self.root, "Title", "body", now=self.now, date="2026-11-12")
        self.assertTrue(os.path.basename(path).startswith("2026-11-12-140322-title-"))
        empty = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, empty, True)
        with self.assertRaises(FileNotFoundError):
            nsh.new_entry(empty, "Title", "body")


class CommandLineTests(unittest.TestCase):
    """Tests [@ANCHOR: tools:night_shift_history_main]"""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        _write(os.path.join(self.root, nsh.OLD_FILE), nsh.STUB_TEXT)
        os.makedirs(os.path.join(self.root, nsh.DIRECTORY))

    def _run(self, *args, stdin=""):
        return subprocess.run([sys.executable, _TOOL, *args], input=stdin, capture_output=True, text=True)

    def test_new_then_latest_then_check_round_trip(self):
        made = self._run("new", "--title", "Round trip", "--root", self.root, stdin="- it works\n")
        self.assertEqual(made.returncode, 0, made.stderr)
        relative = made.stdout.strip()
        self.assertTrue(relative.startswith(nsh.DIRECTORY + os.sep), relative)
        self.assertTrue(os.path.isfile(os.path.join(self.root, relative)))
        shown = self._run("latest", "-n", "1", "--root", self.root)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("-- Round trip", shown.stdout)
        self.assertIn("- it works", shown.stdout)
        titles = self._run("latest", "--titles", "--root", self.root)
        self.assertEqual(titles.stdout.strip().split("\t")[1], f"{datetime.date.today().isoformat()} -- Round trip")
        self.assertEqual(self._run("check", self.root).returncode, 0)

    def test_check_fails_with_a_message_on_standard_error(self):
        _write(os.path.join(self.root, nsh.OLD_FILE), "appended\n")
        result = self._run("check", self.root)
        self.assertEqual(result.returncode, 1)
        self.assertIn("stub", result.stderr)

    def test_new_without_a_directory_and_latest_without_anything_exit_with_one(self):
        empty = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, empty, True)
        self.assertEqual(self._run("new", "--title", "x", "--root", empty, stdin="b").returncode, 1)
        self.assertEqual(self._run("latest", "--root", empty).returncode, 1)

    def test_main_called_in_process_returns_the_status(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(nsh.main(["check", self.root]), 0)
            _write(os.path.join(self.root, nsh.OLD_FILE), "appended\n")
            self.assertEqual(nsh.main(["check", self.root]), 1)


if __name__ == "__main__":
    unittest.main()
