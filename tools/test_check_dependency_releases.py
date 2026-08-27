#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_dependency_releases.py.

Every real GitHub API call goes through _github_get(), the one I/O boundary in this script --
mocked throughout, matching the boundary-mocking approach test_check_pip_audit.py/
test_check_cargo_deny.py already use for their own network/subprocess calls. No test here makes
a real network call. main()'s manifest-loading is exercised against real temp-file JSON
fixtures, the same style test_check_dependency_cycles.py uses for real __manifest__.py fixtures.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_dependency_releases as chk  # noqa: E402


class CheckReleaseModeTests(unittest.TestCase):
    def test_a_newer_upstream_tag_is_reported_stale(self):
        with patch.object(chk, "_github_get", return_value={"tag_name": "v2.0.0"}) as mock_get:
            latest, stale = chk.check_release_mode("owner/repo", "v1.0.0")
            self.assertEqual(latest, "v2.0.0")
            self.assertTrue(stale)
            mock_get.assert_called_once_with("/repos/owner/repo/releases/latest")

    def test_a_matching_pinned_tag_is_not_stale(self):
        with patch.object(chk, "_github_get", return_value={"tag_name": "v1.0.0"}):
            latest, stale = chk.check_release_mode("owner/repo", "v1.0.0")
            self.assertEqual(latest, "v1.0.0")
            self.assertFalse(stale)


class CheckBranchModeTests(unittest.TestCase):
    def test_a_newer_upstream_commit_is_reported_stale(self):
        with patch.object(chk, "_github_get", return_value={"sha": "newsha123"}) as mock_get:
            latest, stale = chk.check_branch_mode("owner/repo", "develop", "oldsha456")
            self.assertEqual(latest, "newsha123")
            self.assertTrue(stale)
            mock_get.assert_called_once_with("/repos/owner/repo/commits/develop")

    def test_a_matching_pinned_commit_is_not_stale(self):
        with patch.object(chk, "_github_get", return_value={"sha": "samesha"}):
            latest, stale = chk.check_branch_mode("owner/repo", "develop", "samesha")
            self.assertFalse(stale)


class CheckOneTests(unittest.TestCase):
    def test_release_mode_dispatches_to_check_release_mode(self):
        entry = {"repo": "owner/repo", "mode": "release", "pinned": "v1.0.0"}
        with patch.object(chk, "_github_get", return_value={"tag_name": "v1.0.0"}):
            result = chk.check_one("thing", entry)
        self.assertEqual(
            result,
            {"name": "thing", "repo": "owner/repo", "pinned": "v1.0.0", "latest": "v1.0.0", "stale": False},
        )

    def test_branch_mode_dispatches_to_check_branch_mode(self):
        entry = {"repo": "owner/repo", "mode": "branch", "branch": "develop", "pinned": "abc123"}
        with patch.object(chk, "_github_get", return_value={"sha": "abc123"}):
            result = chk.check_one("thing", entry)
        self.assertEqual(result["latest"], "abc123")
        self.assertFalse(result["stale"])

    def test_an_unknown_mode_reports_an_error_without_calling_github(self):
        entry = {"repo": "owner/repo", "mode": "nonsense", "pinned": "v1.0.0"}
        with patch.object(chk, "_github_get") as mock_get:
            result = chk.check_one("thing", entry)
        mock_get.assert_not_called()
        self.assertEqual(result, {"name": "thing", "error": "unknown mode 'nonsense'"})

    def test_an_http_error_from_github_is_reported_not_raised(self):
        entry = {"repo": "owner/repo", "mode": "release", "pinned": "v1.0.0"}
        with patch.object(
            chk, "_github_get", side_effect=HTTPError("url", 404, "Not Found", {}, None)
        ):
            result = chk.check_one("thing", entry)
        self.assertIn("error", result)
        self.assertIn("404", result["error"])
        self.assertIn("owner/repo", result["error"])

    def test_a_network_error_from_github_is_reported_not_raised(self):
        entry = {"repo": "owner/repo", "mode": "release", "pinned": "v1.0.0"}
        with patch.object(chk, "_github_get", side_effect=URLError("no route to host")):
            result = chk.check_one("thing", entry)
        self.assertIn("error", result)
        self.assertIn("no route to host", result["error"])


class MainIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.manifest_path = os.path.join(self.tmp, "dependency_watch.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_manifest(self, entries):
        manifest = {"_comment": "ignored, starts with underscore"}
        manifest.update(entries)
        with open(self.manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f)

    def _run(self):
        with patch.object(sys, "argv", ["check_dependency_releases.py", self.manifest_path]):
            with self.assertRaises(SystemExit) as ctx:
                chk.main()
            return ctx.exception.code

    def test_everything_up_to_date_exits_zero(self):
        self._write_manifest(
            {"thing_a": {"repo": "owner/a", "mode": "release", "pinned": "v1.0.0"}}
        )
        with patch.object(chk, "_github_get", return_value={"tag_name": "v1.0.0"}):
            code = self._run()
        self.assertEqual(code, 0)

    def test_one_stale_dependency_exits_one(self):
        self._write_manifest(
            {"thing_a": {"repo": "owner/a", "mode": "release", "pinned": "v1.0.0"}}
        )
        with patch.object(chk, "_github_get", return_value={"tag_name": "v2.0.0"}):
            code = self._run()
        self.assertEqual(code, 1)

    def test_one_unreachable_dependency_exits_one_even_if_others_are_current(self):
        self._write_manifest(
            {
                "thing_a": {"repo": "owner/a", "mode": "release", "pinned": "v1.0.0"},
                "thing_b": {"repo": "owner/b", "mode": "release", "pinned": "v1.0.0"},
            }
        )
        with patch.object(
            chk,
            "_github_get",
            side_effect=[
                {"tag_name": "v1.0.0"},
                HTTPError("url", 500, "Server Error", {}, None),
            ],
        ):
            code = self._run()
        self.assertEqual(code, 1)

    def test_the_underscore_prefixed_comment_key_is_never_treated_as_a_dependency(self):
        self._write_manifest(
            {"thing_a": {"repo": "owner/a", "mode": "release", "pinned": "v1.0.0"}}
        )
        with patch.object(chk, "_github_get", return_value={"tag_name": "v1.0.0"}) as mock_get:
            self._run()
        # Only one real dependency entry -- the _comment key must never reach check_one()/
        # _github_get() at all (it has no "repo"/"mode" keys and would KeyError if it did).
        mock_get.assert_called_once()

    def test_a_branch_mode_entry_is_checked_via_the_commits_endpoint(self):
        self._write_manifest(
            {
                "thing_a": {
                    "repo": "owner/a",
                    "mode": "branch",
                    "branch": "develop",
                    "pinned": "abc123",
                }
            }
        )
        with patch.object(chk, "_github_get", return_value={"sha": "abc123"}) as mock_get:
            code = self._run()
        self.assertEqual(code, 0)
        mock_get.assert_called_once_with("/repos/owner/a/commits/develop")

    def test_default_manifest_path_is_dependency_watch_json_next_to_this_script(self):
        expected = os.path.join(os.path.dirname(os.path.abspath(chk.__file__)), "dependency_watch.json")
        with patch.object(sys, "argv", ["check_dependency_releases.py"]):
            with patch("builtins.open", side_effect=FileNotFoundError) as mock_open:
                with self.assertRaises(FileNotFoundError):
                    chk.main()
                mock_open.assert_called_once_with(expected, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
