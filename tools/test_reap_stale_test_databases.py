#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for reap_stale_test_databases.py.

This is a destructive tool (drops real PostgreSQL databases and deletes real filestore
directories), had NO test coverage at all before this file was written, and the review that
added it found a real, live-data-corrupting bug in `_drop_one` (formerly inlined in `main()`):
the filestore directory was removed unconditionally, with no check on whether `dropdb` itself
actually succeeded. Every real subprocess/filesystem call is mocked here -- this suite must never
touch a real database or a real directory.
"""

import logging
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import reap_stale_test_databases as reaper  # noqa: E402


class _FakeCompletedProcess:
    def __init__(self, returncode):
        self.returncode = returncode


class _FakeCompletedProcessWithStdout:
    def __init__(self, stdout):
        self.stdout = stdout


class DropOneTests(unittest.TestCase):
    # Real bug found 2026-09-10: `_drop_one` (formerly inlined directly in main()'s loop) used to
    # call shutil.rmtree() on the filestore directory unconditionally after calling dropdb, with
    # no check on dropdb's own exit code. A real dropdb failure (a connection racing in, a
    # transient sudo/permission failure) would leave the database alive but its filestore
    # deleted anyway -- corrupting a real, still-existing database's attachments while leaving it
    # around to be used. These tests are confirmed to fail against the pre-fix source: the old
    # code had no `_drop_one` function to call in isolation at all.
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def test_a_successful_drop_removes_the_filestore_dir(self):
        with mock.patch.object(
            reaper.subprocess, "run", return_value=_FakeCompletedProcess(0)
        ) as mock_run, mock.patch.object(
            reaper.os.path, "isdir", return_value=True
        ), mock.patch.object(
            reaper.shutil, "rmtree"
        ) as mock_rmtree:
            result = reaper._drop_one("tmp_foo")
            self.assertTrue(result)
            mock_run.assert_called_once()
            mock_rmtree.assert_called_once_with(
                os.path.join(reaper.ODOO_FILESTORE_BASE, "tmp_foo"), ignore_errors=True
            )

    def test_a_dropdb_failure_leaves_the_filestore_dir_alone(self):
        # The exact real bug: dropdb exits nonzero (e.g. a connection raced in), so the database
        # is still alive -- its filestore must NOT be touched.
        with mock.patch.object(
            reaper.subprocess, "run", return_value=_FakeCompletedProcess(1)
        ), mock.patch.object(
            reaper.os.path, "isdir", return_value=True
        ), mock.patch.object(
            reaper.shutil, "rmtree"
        ) as mock_rmtree:
            result = reaper._drop_one("tmp_foo")
            self.assertFalse(result)
            mock_rmtree.assert_not_called()

    def test_a_dry_run_never_calls_subprocess_or_rmtree(self):
        with mock.patch.object(reaper.subprocess, "run") as mock_run, mock.patch.object(
            reaper.os.path, "isdir", return_value=True
        ), mock.patch.object(reaper.shutil, "rmtree") as mock_rmtree:
            result = reaper._drop_one("tmp_foo", dry_run=True)
            self.assertTrue(result)
            mock_run.assert_not_called()
            mock_rmtree.assert_not_called()

    def test_a_successful_drop_with_no_filestore_dir_does_not_call_rmtree(self):
        with mock.patch.object(
            reaper.subprocess, "run", return_value=_FakeCompletedProcess(0)
        ), mock.patch.object(
            reaper.os.path, "isdir", return_value=False
        ), mock.patch.object(
            reaper.shutil, "rmtree"
        ) as mock_rmtree:
            result = reaper._drop_one("tmp_foo")
            self.assertTrue(result)
            mock_rmtree.assert_not_called()


class ListScratchDatabasesTests(unittest.TestCase):
    def test_only_prefix_matching_names_are_returned(self):
        with mock.patch.object(
            reaper,
            "_run_psql",
            return_value="tmp_a|100\nhams_dev|200\ntmp_b|300\n",
        ):
            result = reaper._list_scratch_databases()
            self.assertEqual(result, {"tmp_a": 100, "tmp_b": 300})

    def test_blank_lines_are_skipped(self):
        with mock.patch.object(reaper, "_run_psql", return_value="tmp_a|1\n\n"):
            self.assertEqual(reaper._list_scratch_databases(), {"tmp_a": 1})


class DirTreeMaxMtimeTests(unittest.TestCase):
    # Real bug found 2026-09-12, confirmed empirically against a real throwaway PostgreSQL
    # database: a directory's own mtime does NOT advance when an existing file inside it is
    # merely written to (only when an entry is added/removed/renamed) -- standard POSIX
    # semantics, but this reaper's own module docstring claimed the opposite ("a database still
    # receiving real writes keeps advancing that mtime") without ever verifying it. Real UPDATE/
    # INSERT activity into an already-created table left a real database's base OID directory
    # mtime completely unchanged while the table's own relation file mtime correctly advanced.
    # These tests use real temp directories/files (not mocked) since this is pure filesystem
    # behavior -- the same thing that made the original bug unverified-by-reasoning-alone.
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_write_into_an_existing_file_advances_the_reported_mtime(self):
        # This is the exact real bug: the OLD code (bare os.stat(base_dir).st_mtime) would have
        # reported the directory's own mtime, which this test's own setup below deliberately
        # keeps frozen while only mutating the file's contents -- proving the fix actually looks
        # inside the tree rather than only at the top-level directory.
        existing_file = os.path.join(self.tmp, "relation_file")
        with open(existing_file, "w") as f:
            f.write("initial content\n")
        dir_mtime_before = os.stat(self.tmp).st_mtime
        time.sleep(1.1)
        with open(existing_file, "w") as f:
            f.write("much longer replacement content that is definitely different\n")
        dir_mtime_after = os.stat(self.tmp).st_mtime
        self.assertEqual(
            dir_mtime_before,
            dir_mtime_after,
            "test setup assumption violated: the directory's own mtime moved on a plain file "
            "write, so this test no longer isolates the bug it's meant to catch",
        )
        reported = reaper._dir_tree_max_mtime(self.tmp)
        self.assertAlmostEqual(reported, os.stat(existing_file).st_mtime, delta=0.5)
        self.assertGreater(reported, dir_mtime_before)

    def test_a_nested_subdirectory_file_is_also_considered(self):
        nested = os.path.join(self.tmp, "sub", "deeper")
        os.makedirs(nested)
        nested_file = os.path.join(nested, "f")
        with open(nested_file, "w") as f:
            f.write("x\n")
        reported = reaper._dir_tree_max_mtime(self.tmp)
        self.assertAlmostEqual(reported, os.stat(nested_file).st_mtime, delta=0.5)

    def test_an_empty_directory_returns_its_own_mtime(self):
        reported = reaper._dir_tree_max_mtime(self.tmp)
        self.assertAlmostEqual(reported, os.stat(self.tmp).st_mtime, delta=0.5)

    def test_a_nonexistent_directory_raises_oserror(self):
        with self.assertRaises(OSError):
            reaper._dir_tree_max_mtime(os.path.join(self.tmp, "does_not_exist"))

    def test_an_unreadable_subdirectory_raises_rather_than_silently_under_reporting(self):
        # Real risk this test guards against: os.walk()'s own default onerror silently SKIPS an
        # unlistable subdirectory instead of raising, which would let _dir_tree_max_mtime return
        # a plausible-looking but silently-incomplete (too-old) mtime instead of correctly
        # signaling "go use the sudo fallback" to its caller.
        blocked = os.path.join(self.tmp, "blocked")
        os.makedirs(blocked)
        os.chmod(blocked, 0o000)
        try:
            if os.geteuid() == 0:
                self.skipTest("running as root -- permission bits don't block root's own access")
            with self.assertRaises(OSError):
                reaper._dir_tree_max_mtime(self.tmp)
        finally:
            os.chmod(blocked, 0o755)


class SudoDirTreeMaxMtimeTests(unittest.TestCase):
    def test_the_max_of_several_reported_mtimes_is_returned(self):
        with mock.patch.object(
            reaper.subprocess,
            "run",
            return_value=_FakeCompletedProcessWithStdout(
                "1000000000.5\n1000000050.25\n1000000010.0\n"
            ),
        ):
            self.assertEqual(reaper._sudo_dir_tree_max_mtime("/some/dir"), 1000000050.25)

    def test_empty_output_raises_value_error(self):
        with mock.patch.object(
            reaper.subprocess, "run", return_value=_FakeCompletedProcessWithStdout("")
        ):
            with self.assertRaises(ValueError):
                reaper._sudo_dir_tree_max_mtime("/some/dir")


class DatabaseAgeHoursFallbackTests(unittest.TestCase):
    def test_the_sudo_fallback_is_used_when_the_direct_walk_raises_oserror(self):
        with mock.patch.object(
            reaper, "_dir_tree_max_mtime", side_effect=OSError("permission denied")
        ), mock.patch.object(
            reaper, "_sudo_dir_tree_max_mtime", return_value=1000000000.0
        ) as mock_sudo:
            result = reaper._database_age_hours(42)
            mock_sudo.assert_called_once()
            self.assertIsNotNone(result)

    def test_a_sudo_fallback_failure_returns_none_rather_than_raising(self):
        with mock.patch.object(
            reaper, "_dir_tree_max_mtime", side_effect=OSError("permission denied")
        ), mock.patch.object(
            reaper, "_sudo_dir_tree_max_mtime", side_effect=ValueError("no mtimes")
        ):
            self.assertIsNone(reaper._database_age_hours(42))


class OpenDatabaseNamesTests(unittest.TestCase):
    def test_parses_distinct_open_database_names(self):
        with mock.patch.object(reaper, "_run_psql", return_value="tmp_a\ntmp_b\n"):
            self.assertEqual(reaper._open_database_names(), {"tmp_a", "tmp_b"})


class FindReapableDatabasesTests(unittest.TestCase):
    def test_an_open_database_is_never_reapable_regardless_of_age(self):
        with mock.patch.object(
            reaper, "_list_scratch_databases", return_value={"tmp_a": 1}
        ), mock.patch.object(
            reaper, "_open_database_names", return_value={"tmp_a"}
        ), mock.patch.object(
            reaper, "_database_age_hours", return_value=1000.0
        ):
            self.assertEqual(reaper.find_reapable_databases(6), [])

    def test_a_database_younger_than_the_threshold_is_not_reapable(self):
        with mock.patch.object(
            reaper, "_list_scratch_databases", return_value={"tmp_a": 1}
        ), mock.patch.object(
            reaper, "_open_database_names", return_value=set()
        ), mock.patch.object(
            reaper, "_database_age_hours", return_value=1.0
        ):
            self.assertEqual(reaper.find_reapable_databases(6), [])

    def test_a_database_whose_age_cannot_be_determined_is_not_reapable(self):
        with mock.patch.object(
            reaper, "_list_scratch_databases", return_value={"tmp_a": 1}
        ), mock.patch.object(
            reaper, "_open_database_names", return_value=set()
        ), mock.patch.object(
            reaper, "_database_age_hours", return_value=None
        ):
            self.assertEqual(reaper.find_reapable_databases(6), [])

    def test_a_closed_sufficiently_old_database_is_reapable(self):
        with mock.patch.object(
            reaper, "_list_scratch_databases", return_value={"tmp_a": 1}
        ), mock.patch.object(
            reaper, "_open_database_names", return_value=set()
        ), mock.patch.object(
            reaper, "_database_age_hours", return_value=100.0
        ):
            self.assertEqual(reaper.find_reapable_databases(6), ["tmp_a"])


if __name__ == "__main__":
    unittest.main()
