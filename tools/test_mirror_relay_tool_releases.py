#!/usr/bin/env python3
# Copyright © Bruce Perens K6BP. All Rights Reserved. This software is proprietary and confidential.

"""
Unit tests for mirror_relay_tool_releases.py.

This is a maintainer-run tool that fetches a real binary from GitHub and publishes it to a real
production endpoint with a real credential -- no real network call is ever made here. Every
urllib call is mocked.
"""

import io
import json
import os
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mirror_relay_tool_releases as mirror  # noqa: E402


class FindReleaseAssetsTests(unittest.TestCase):
    def test_returns_filename_to_download_url_mapping(self):
        fake_body = json.dumps(
            {"assets": [{"name": "pat_1.0.0_linux_amd64.tar.gz", "browser_download_url": "https://x/1"}]}
        ).encode("utf-8")
        with mock.patch.object(mirror, "fetch", return_value=fake_body):
            result = mirror.find_release_assets("pat", "v1.0.0")
            self.assertEqual(result, {"pat_1.0.0_linux_amd64.tar.gz": "https://x/1"})

    def test_a_bad_tag_raises_a_clear_system_exit(self):
        err = urllib.error.HTTPError("https://x", 404, "Not Found", {}, io.BytesIO(b""))
        with mock.patch.object(mirror, "fetch", side_effect=err):
            with self.assertRaises(SystemExit) as ctx:
                mirror.find_release_assets("pat", "vNoSuchTag")
            self.assertIn("404", str(ctx.exception))


class PublishTests(unittest.TestCase):
    # Real bug found 2026-09-10: publish() had NO handling at all for a real, plausible failure
    # (a wrong/expired publish key, the endpoint down, a server error) -- any non-2xx response
    # crashed with a raw, unhandled urllib.error.HTTPError instead of a clear, actionable
    # SystemExit matching find_release_assets' own already-established error-reporting style.
    def test_a_successful_publish_does_not_raise(self):
        fake_resp = mock.MagicMock()
        fake_resp.read.return_value = json.dumps({"result": {"status": "success"}}).encode()
        fake_resp.__enter__ = mock.Mock(return_value=fake_resp)
        fake_resp.__exit__ = mock.Mock(return_value=False)
        with mock.patch.object(mirror.urllib.request, "urlopen", return_value=fake_resp):
            mirror.publish("https://hams.com", "key", "pat", "linux", "v1.0.0", b"data", "pat.tar.gz", "https://x")

    def test_an_http_error_during_publish_raises_a_clear_system_exit_not_a_raw_traceback(self):
        # Confirmed to fail against the pre-fix source: the old code let HTTPError propagate
        # unhandled straight out of publish(), not as a SystemExit at all.
        err = urllib.error.HTTPError(
            "https://hams.com/api/relay_bridge/tool/publish", 401, "Unauthorized", {},
            io.BytesIO(b"invalid api key"),
        )
        with mock.patch.object(mirror.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(SystemExit) as ctx:
                mirror.publish("https://hams.com", "badkey", "pat", "linux", "v1.0.0", b"data", "pat.tar.gz", "https://x")
            self.assertIn("401", str(ctx.exception))

    def test_a_non_success_status_still_raises_system_exit(self):
        fake_resp = mock.MagicMock()
        fake_resp.read.return_value = json.dumps({"result": {"status": "error", "message": "boom"}}).encode()
        fake_resp.__enter__ = mock.Mock(return_value=fake_resp)
        fake_resp.__exit__ = mock.Mock(return_value=False)
        with mock.patch.object(mirror.urllib.request, "urlopen", return_value=fake_resp):
            with self.assertRaises(SystemExit):
                mirror.publish("https://hams.com", "key", "pat", "linux", "v1.0.0", b"data", "pat.tar.gz", "https://x")


class AssetPatternTests(unittest.TestCase):
    def test_pat_linux_pattern_matches_a_real_shaped_filename(self):
        pattern = mirror.ASSET_PATTERNS[("pat", "linux")]
        self.assertTrue(pattern.match("pat_1.0.0_linux_amd64.tar.gz"))

    def test_direwolf_windows_pattern_matches_a_git_hash_suffixed_filename(self):
        pattern = mirror.ASSET_PATTERNS[("direwolf", "windows")]
        self.assertTrue(pattern.match("direwolf-1.8.1-a231971_x86_64.zip"))

    def test_an_unrelated_filename_does_not_match(self):
        pattern = mirror.ASSET_PATTERNS[("pat", "linux")]
        self.assertFalse(pattern.match("README.md"))

    def test_direwolf_has_no_linux_or_macos_pattern(self):
        self.assertNotIn(("direwolf", "linux"), mirror.ASSET_PATTERNS)
        self.assertNotIn(("direwolf", "macos"), mirror.ASSET_PATTERNS)


if __name__ == "__main__":
    unittest.main()
