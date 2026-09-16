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
import unittest
import urllib.error
from unittest import mock

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

    def test_a_jsonrpc_level_server_error_surfaces_the_real_message_not_an_empty_dict(self):
        # Real bug found 2026-09-12, confirmed against Odoo core's own jsonrpc dispatcher: an
        # unhandled server-side exception returns HTTP 200 with a top-level {"error": {...}}
        # envelope, not {"result": {...}} -- so `except urllib.error.HTTPError` never fires, and
        # the old code's `result.get("result", {})` silently discarded the real error, raising
        # SystemExit with an empty, useless "{}" instead of the actual message.
        fake_resp = mock.MagicMock()
        fake_resp.read.return_value = json.dumps(
            {
                "jsonrpc": "2.0",
                "error": {"code": 200, "message": "Odoo Server Error", "data": {"debug": "AccessError: ..."}},
                "id": None,
            }
        ).encode()
        fake_resp.__enter__ = mock.Mock(return_value=fake_resp)
        fake_resp.__exit__ = mock.Mock(return_value=False)
        with mock.patch.object(mirror.urllib.request, "urlopen", return_value=fake_resp):
            with self.assertRaises(SystemExit) as ctx:
                mirror.publish("https://hams.com", "key", "pat", "linux", "v1.0.0", b"data", "pat.tar.gz", "https://x")
            self.assertIn("Odoo Server Error", str(ctx.exception))
            self.assertIn("AccessError", str(ctx.exception))


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


SHA_A = "a" * 64
SHA_B = "b" * 64


class VerifyPinnedSha256Tests(unittest.TestCase):
    """Neither upstream publishes checksums, so the committed pins file is the only integrity check.
    Before it existed the script printed a sha256 and published regardless."""

    def test_a_matching_pin_passes(self):
        pins = {mirror.pin_key("pat", "v1.0.0", "linux", "pat.tar.gz"): SHA_A}
        mirror.verify_pinned_sha256(pins, "pat", "v1.0.0", "linux", "pat.tar.gz", SHA_A, "pins.json")

    def test_a_pin_comparison_ignores_hex_case(self):
        pins = {mirror.pin_key("pat", "v1.0.0", "linux", "pat.tar.gz"): SHA_A.upper()}
        mirror.verify_pinned_sha256(pins, "pat", "v1.0.0", "linux", "pat.tar.gz", SHA_A, "pins.json")

    def test_a_mismatched_hash_refuses_and_shows_both_hashes(self):
        pins = {mirror.pin_key("pat", "v1.0.0", "linux", "pat.tar.gz"): SHA_A}
        with self.assertRaises(SystemExit) as ctx:
            mirror.verify_pinned_sha256(pins, "pat", "v1.0.0", "linux", "pat.tar.gz", SHA_B, "pins.json")
        self.assertIn("MISMATCH", str(ctx.exception))
        self.assertIn(SHA_A, str(ctx.exception))
        self.assertIn(SHA_B, str(ctx.exception))

    def test_a_missing_pin_refuses_and_prints_the_line_to_add(self):
        with self.assertRaises(SystemExit) as ctx:
            mirror.verify_pinned_sha256({}, "pat", "v1.0.0", "linux", "pat.tar.gz", SHA_B, "pins.json")
        self.assertIn(f'"pat/v1.0.0/linux/pat.tar.gz": "{SHA_B}"', str(ctx.exception))

    def test_a_pin_for_another_tag_does_not_cover_this_one(self):
        pins = {mirror.pin_key("pat", "v0.9.0", "linux", "pat.tar.gz"): SHA_A}
        with self.assertRaises(SystemExit):
            mirror.verify_pinned_sha256(pins, "pat", "v1.0.0", "linux", "pat.tar.gz", SHA_A, "pins.json")


class LoadPinsTests(unittest.TestCase):
    def test_the_committed_pins_file_loads(self):
        self.assertIsInstance(mirror.load_pins(mirror.DEFAULT_PINS_PATH), dict)

    def test_a_missing_pins_file_is_an_error_not_an_empty_pin_set(self):
        with self.assertRaises(SystemExit):
            mirror.load_pins("/nonexistent/relay_tool_release_pins.json")

    def test_a_pins_file_without_a_pins_object_is_an_error(self):
        with mock.patch("builtins.open", mock.mock_open(read_data='{"other": {}}')):
            with self.assertRaises(SystemExit):
                mirror.load_pins("pins.json")


class MainPublishesOnlyVerifiedReleasesTests(unittest.TestCase):
    """main() must verify every platform before publishing any of them."""

    PAT_ASSETS = {
        "pat_1.0.0_windows_i386.zip": "https://x/win",
        "pat_1.0.0_darwin_amd64.pkg": "https://x/mac",
        "pat_1.0.0_linux_amd64.tar.gz": "https://x/linux",
    }
    BODIES = {"https://x/win": b"win bytes", "https://x/mac": b"mac bytes", "https://x/linux": b"linux bytes"}

    def _pins_for(self, bodies):
        names = {url: name for name, url in self.PAT_ASSETS.items()}
        platform_of = {"https://x/win": "windows", "https://x/mac": "macos", "https://x/linux": "linux"}
        return {
            mirror.pin_key("pat", "v1.0.0", platform_of[url], names[url]): mirror.hashlib.sha256(body).hexdigest()
            for url, body in bodies.items()
        }

    def _run_main(self, pins):
        argv = ["mirror", "--tool", "pat", "--tag", "v1.0.0", "--odoo-url", "https://hams.test",
                "--publish-key", "key", "--pins-file", "pins.json"]
        with mock.patch.object(mirror.sys, "argv", argv), \
                mock.patch.object(mirror, "load_pins", return_value=pins), \
                mock.patch.object(mirror, "find_release_assets", return_value=dict(self.PAT_ASSETS)), \
                mock.patch.object(mirror, "fetch", side_effect=lambda url: self.BODIES[url]), \
                mock.patch.object(mirror, "publish") as publish_mock, \
                mock.patch("builtins.print"):
            try:
                result = mirror.main()
            except SystemExit as exc:
                result = exc
        return result, publish_mock

    def test_all_platforms_pinned_and_matching_are_published(self):
        result, publish_mock = self._run_main(self._pins_for(self.BODIES))
        self.assertEqual(result, 0)
        self.assertEqual(publish_mock.call_count, 3)

    def test_one_tampered_platform_publishes_nothing(self):
        pins = self._pins_for(self.BODIES)
        tampered = dict(self.BODIES, **{"https://x/linux": b"original reviewed bytes"})
        pins.update(self._pins_for({"https://x/linux": tampered["https://x/linux"]}))
        result, publish_mock = self._run_main(pins)
        self.assertIsInstance(result, SystemExit)
        self.assertIn("MISMATCH", str(result))
        publish_mock.assert_not_called()

    def test_an_unpinned_release_publishes_nothing(self):
        result, publish_mock = self._run_main({})
        self.assertIsInstance(result, SystemExit)
        self.assertIn("No reviewed sha256 pin", str(result))
        publish_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
