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
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import reap_stale_test_databases as reaper  # noqa: E402


class _FakeCompletedProcess:
    def __init__(self, returncode):
        self.returncode = returncode


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
