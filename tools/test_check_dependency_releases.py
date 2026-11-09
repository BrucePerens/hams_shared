#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_dependency_releases.py.

Every real GitHub API call goes through _github_get(), the one I/O boundary in this script --
mocked throughout, matching the boundary-mocking approach test_check_pip_audit.py/
test_check_cargo_deny.py already use for their own network/subprocess calls. No test here makes
a real network call. main()'s manifest-loading is exercised against real temp-file JSON
fixtures, the same style test_check_dependency_cycles.py uses for real __manifest__.py fixtures.
"""

import contextlib
import datetime
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch
from urllib.error import HTTPError, URLError

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

    def test_a_response_missing_the_expected_key_is_reported_not_raised(self):
        # GitHub's API returning valid JSON with an unexpected
        # schema (missing "tag_name") raised an uncaught KeyError from inside check_one(),
        # rather than being isolated to this one entry's own error report the way HTTPError/
        # URLError already are.
        entry = {"repo": "owner/repo", "mode": "release", "pinned": "v1.0.0"}
        with patch.object(chk, "_github_get", return_value={"unexpected": "schema"}):
            result = chk.check_one("thing", entry)
        self.assertIn("error", result)
        self.assertIn("owner/repo", result["error"])

    def test_a_non_json_response_body_is_reported_not_raised(self):
        # Same underlying bug, the other real trigger: a non-JSON body (an HTML error/
        # maintenance page GitHub can return with a 200 status) makes json.load() itself
        # raise json.JSONDecodeError inside _github_get(), also previously uncaught.
        entry = {"repo": "owner/repo", "mode": "release", "pinned": "v1.0.0"}
        with patch.object(
            chk,
            "_github_get",
            side_effect=json.JSONDecodeError("Expecting value", "<html>oops</html>", 0),
        ):
            result = chk.check_one("thing", entry)
        self.assertIn("error", result)


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

    def test_a_malformed_response_for_one_dependency_does_not_abort_the_whole_run(self):
        # Before the fix, an uncaught KeyError from ONE
        # dependency's malformed API response propagated out of the list comprehension in
        # main(), crashing the whole scheduled run with a traceback before "thing_b" (a
        # perfectly healthy, up-to-date dependency) was ever checked or reported on.
        self._write_manifest(
            {
                "thing_a": {"repo": "owner/a", "mode": "release", "pinned": "v1.0.0"},
                "thing_b": {"repo": "owner/b", "mode": "release", "pinned": "v1.0.0"},
            }
        )
        with patch.object(
            chk,
            "_github_get",
            side_effect=[{"unexpected": "schema"}, {"tag_name": "v1.0.0"}],
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


TODAY = datetime.date(2026, 11, 4)


def _entry(**extra):
    entry = {"repo": "o/r", "mode": "release", "pinned": "v1", "notes": "n"}
    entry.update(extra)
    return entry


def _hold(until="2026-11-18", reason="rebuild of all five platform builds needed"):
    return {"reason": reason, "until": until}


class HoldStatusTests(unittest.TestCase):
    # Tests [@ANCHOR: dependency_hold_status]
    def test_no_hold_is_none(self):
        self.assertIsNone(chk.hold_status(_entry(), TODAY))

    # Tests [@ANCHOR: dependency_hold_status]
    def test_active_hold_carries_reason_and_date(self):
        state, message = chk.hold_status(_entry(hold=_hold()), TODAY)
        self.assertEqual(state, "active")
        self.assertIn("rebuild of all five platform builds needed", message)
        self.assertIn("2026-11-18", message)

    # Tests [@ANCHOR: dependency_hold_status]
    def test_a_hold_is_active_on_its_last_day_and_expired_the_day_after(self):
        entry = _entry(hold=_hold(until="2026-11-04"))
        self.assertEqual(chk.hold_status(entry, TODAY)[0], "active")
        self.assertEqual(chk.hold_status(entry, TODAY + datetime.timedelta(days=1))[0], "expired")

    # Tests [@ANCHOR: dependency_hold_status]
    def test_malformed_holds_are_invalid(self):
        for bad in (
            "yes",
            {"until": "2026-11-18"},
            {"reason": "   ", "until": "2026-11-18"},
            {"reason": "x"},
            {"reason": "x", "until": "next week"},
            {"reason": "x", "until": None},
        ):
            with self.subTest(hold=bad):
                self.assertEqual(chk.hold_status(_entry(hold=bad), TODAY)[0], "invalid")

    # Tests [@ANCHOR: dependency_watch_hold_marker]
    def test_a_hold_cannot_be_set_further_ahead_than_the_maximum(self):
        limit = TODAY + datetime.timedelta(days=chk.MAX_HOLD_DAYS)
        self.assertEqual(chk.hold_status(_entry(hold=_hold(until=limit.isoformat())), TODAY)[0], "active")
        beyond = limit + datetime.timedelta(days=1)
        self.assertEqual(chk.hold_status(_entry(hold=_hold(until=beyond.isoformat())), TODAY)[0], "invalid")


class RealWatchFileTests(unittest.TestCase):
    # Tests [@ANCHOR: dependency_hold_status]
    def test_no_hold_in_the_real_watch_file_is_malformed(self):
        path = os.path.join(os.path.dirname(chk.__file__), "dependency_watch.json")
        with open(path, encoding="utf-8") as f:
            entries = json.load(f)
        for name, entry in entries.items():
            if name.startswith("_"):
                continue
            with self.subTest(dependency=name):
                status = chk.hold_status(entry, datetime.date.today())
                self.assertTrue(status is None or status[0] != "invalid", status)


class CheckOneWithHoldTests(unittest.TestCase):
    def _check(self, entry, latest):
        with patch.object(chk, "check_release_mode", return_value=(latest, latest != entry["pinned"])):
            return chk.check_one("dep", entry, today=TODAY)

    # Tests [@ANCHOR: dependency_hold_status]
    def test_stale_pin_without_hold_is_stale(self):
        self.assertTrue(self._check(_entry(), "v2")["stale"])

    # Tests [@ANCHOR: dependency_hold_status]
    def test_stale_pin_with_active_hold_is_not_stale_and_says_why(self):
        result = self._check(_entry(hold=_hold()), "v2")
        self.assertFalse(result["stale"])
        self.assertIn("2026-11-18", result["held"])

    # Tests [@ANCHOR: dependency_hold_status]
    def test_stale_pin_with_expired_hold_is_stale_again(self):
        result = self._check(_entry(hold=_hold(until="2026-11-01")), "v2")
        self.assertTrue(result["stale"])
        self.assertIn("expired", result["hold_note"])

    # Tests [@ANCHOR: dependency_hold_status]
    def test_invalid_hold_is_an_error_not_a_silent_pass(self):
        result = self._check(_entry(hold={"reason": "x"}), "v2")
        self.assertIn("error", result)

    # Tests [@ANCHOR: dependency_hold_status]
    def test_current_pin_with_a_hold_is_fine_and_noted(self):
        result = self._check(_entry(hold=_hold()), "v1")
        self.assertFalse(result["stale"])
        self.assertIn("not needed", result["hold_note"])


class MainExitStatusTests(unittest.TestCase):
    def _run_main(self, entries):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "watch.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(entries, f)
            out = io.StringIO()
            with patch.object(chk.sys, "argv", ["check_dependency_releases.py", path]), \
                    patch.object(chk, "check_release_mode", return_value=("v2", True)), \
                    patch.object(chk.datetime, "date", wraps=datetime.date) as fake_date, \
                    contextlib.redirect_stdout(out):
                fake_date.today.return_value = TODAY
                with self.assertRaises(SystemExit) as raised:
                    chk.main()
            return raised.exception.code, out.getvalue()

    # Tests [@ANCHOR: dependency_hold_status]
    def test_exit_zero_when_the_only_stale_pin_is_held(self):
        code, out = self._run_main({"a": _entry(hold=_hold())})
        self.assertEqual(code, 0)
        self.assertIn("HELD", out)

    # Tests [@ANCHOR: dependency_hold_status]
    def test_exit_one_when_another_pin_is_stale_even_if_one_is_held(self):
        code, _ = self._run_main({"a": _entry(hold=_hold()), "b": _entry()})
        self.assertEqual(code, 1)


class _RedirectProbe:
    """Two real local servers: `url` answers every request with a 302 to the second, `sink`, which records the headers of
    whatever reaches it. A credential header that follows the redirect shows up in `sink_headers`; `first_headers` holds
    what the first server received, which proves the request did carry the credential."""

    def __init__(self):
        outer = self
        self.first_headers: list[dict[str, str]] = []
        self.sink_headers: list[dict[str, str]] = []

        class Sink(BaseHTTPRequestHandler):
            def _answer(self):
                outer.sink_headers.append({k.lower(): v for k, v in self.headers.items()})
                body = b"{}"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_PUT = do_PATCH = _answer

            def log_message(self, *args):
                pass

        self.sink = HTTPServer(("127.0.0.1", 0), Sink)  # burn-ignore-test-local-server: hermetic stand-in for the host a redirect points at
        sink_url = f"http://127.0.0.1:{self.sink.server_port}/elsewhere"  # burn-ignore-test-local-server: hermetic stand-in for the host a redirect points at

        class First(BaseHTTPRequestHandler):
            def _answer(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                outer.first_headers.append({k.lower(): v for k, v in self.headers.items()})
                self.send_response(302)
                self.send_header("Location", sink_url)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = do_POST = do_PUT = do_PATCH = _answer

            def log_message(self, *args):
                pass

        self.first = HTTPServer(("127.0.0.1", 0), First)  # burn-ignore-test-local-server: hermetic stand-in for the credentialed API that answers with a redirect
        self.url = f"http://127.0.0.1:{self.first.server_port}"  # burn-ignore-test-local-server: hermetic stand-in for the credentialed API
        self._threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (self.sink, self.first)]  # burn-ignore-test-daemon-thread: two daemon threads of a hermetic fixture, both shut down in __exit__

    def __enter__(self):
        for thread in self._threads:
            thread.start()
        return self

    def __exit__(self, *exc):
        for server in (self.first, self.sink):
            server.shutdown()
            server.server_close()


class GithubRedirectTests(unittest.TestCase):
    """A token goes to api.github.com only: a redirect to another host is refused, a same-host https redirect is followed."""

    def test_a_redirect_to_another_host_is_refused_and_the_token_never_reaches_it(self):
        with _RedirectProbe() as probe, patch.object(chk, "GITHUB_API", probe.url), patch.dict(os.environ, {"GITHUB_TOKEN": "gh-token"}):
            with self.assertRaises(HTTPError) as raised:
                chk._github_get("/repos/o/r/releases/latest")
        self.assertEqual(raised.exception.code, 302)
        self.assertEqual(probe.first_headers[0]["authorization"], "Bearer gh-token")
        self.assertEqual(probe.sink_headers, [], "the redirect target must never be asked")

    def test_only_a_same_host_https_redirect_is_followed(self):
        handler = chk._SameHostRedirect()
        request = chk.urllib.request.Request("https://api.github.com/repos/o/old/releases/latest", headers={"Authorization": "Bearer t"})
        follow = lambda target: handler.redirect_request(request, None, 301, "Moved", {}, target)  # noqa: E731
        self.assertIsNotNone(follow("https://api.github.com/repositories/42/releases/latest"))
        self.assertIsNone(follow("https://evil.example/repositories/42"))
        self.assertIsNone(follow("http://api.github.com/repositories/42"))
        self.assertIsNone(follow("https://api.github.com.evil.example/x"))


if __name__ == "__main__":
    unittest.main()
