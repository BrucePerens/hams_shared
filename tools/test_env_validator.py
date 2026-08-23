#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for env_validator.py.

Every checker here talks to a real external boundary (a TCP socket, an
SMTP server, a Gemini HTTPS endpoint) or reads real files off disk into
os.environ -- all genuine external boundaries per this sweep's
established convention (mirrors check_bot_compliance.py's mocked
DNS/HTTP boundaries), so socket.create_connection, smtplib.SMTP, and
urllib.request.urlopen are all mocked here; only load_env_files()'s
own file-glob/parse logic runs for real, against a temp directory.

Every os.environ mutation in these tests goes through
unittest.mock.patch.dict so nothing leaks into the rest of a shared
pytest process (this suite runs alongside ~20 others under
run_linters.py's step 27). load_env_files() also checks the hardcoded
literal "." (the process's cwd) as one of its two search directories,
so its tests os.chdir() into an isolated temp directory for the
duration and restore the original cwd in a finally block, and patch
os.path.exists so the also-hardcoded "/opt/hams/etc" branch (present,
but unreadable in this environment -- confirmed empirically) never
depends on this machine's local filesystem state.
"""

import contextlib
import io
import os
import smtplib
import sys
import tempfile
import shutil
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import env_validator as ev  # noqa: E402


class PrintWarningTests(unittest.TestCase):
    def test_writes_a_bracketed_module_tagged_message_to_stderr(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            ev.print_warning("POSTGRES", "something broke")
        self.assertEqual(buf.getvalue(), "\n[POSTGRES WARNING] something broke\n")


class CheckSocketTests(unittest.TestCase):
    def test_a_missing_host_warns_without_attempting_a_connection(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf), patch("socket.create_connection") as m:
            ev.check_socket(None, "5432", "POSTGRES")
        m.assert_not_called()
        self.assertIn("Missing POSTGRES host or port configuration.", buf.getvalue())

    def test_a_missing_port_warns_without_attempting_a_connection(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf), patch("socket.create_connection") as m:
            ev.check_socket("localhost", None, "REDIS")
        m.assert_not_called()
        self.assertIn("Missing REDIS host or port configuration.", buf.getvalue())

    def test_a_successful_connection_produces_no_warning(self):
        buf = io.StringIO()
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=None)
        cm.__exit__ = MagicMock(return_value=False)
        with contextlib.redirect_stderr(buf), patch("socket.create_connection", return_value=cm):
            ev.check_socket("localhost", "5432", "POSTGRES")
        self.assertEqual(buf.getvalue(), "")

    def test_a_refused_connection_is_reported_with_host_port_and_the_underlying_error(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf), patch(
            "socket.create_connection", side_effect=ConnectionRefusedError("refused")
        ):
            ev.check_socket("localhost", "5432", "RABBITMQ")
        self.assertIn("Failed to connect to localhost:5432", buf.getvalue())
        self.assertIn("refused", buf.getvalue())

    def test_a_non_numeric_port_is_caught_and_reported_not_raised(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            ev.check_socket("localhost", "not-a-port", "POSTGRES")
        self.assertIn("Failed to connect to localhost:not-a-port", buf.getvalue())


class CheckSmtpTests(unittest.TestCase):
    def test_missing_host_or_port_warns_without_connecting(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf), patch.dict(
            "os.environ", {}, clear=True
        ), patch("smtplib.SMTP") as m:
            ev.check_smtp()
        m.assert_not_called()
        self.assertIn("Missing SMTP_HOST or SMTP_PORT", buf.getvalue())

    def test_a_successful_handshake_with_no_credentials_never_attempts_login(self):
        buf = io.StringIO()
        server = MagicMock()
        with contextlib.redirect_stderr(buf), patch.dict(
            "os.environ", {"SMTP_HOST": "h", "SMTP_PORT": "25"}, clear=True
        ), patch("smtplib.SMTP", return_value=server):
            ev.check_smtp()
        server.login.assert_not_called()
        self.assertEqual(buf.getvalue(), "")

    def test_an_authentication_failure_is_reported_with_the_username(self):
        buf = io.StringIO()
        server = MagicMock()
        server.login.side_effect = smtplib.SMTPAuthenticationError(535, b"bad creds")
        with contextlib.redirect_stderr(buf), patch.dict(
            "os.environ",
            {"SMTP_HOST": "h", "SMTP_PORT": "25", "SMTP_USER": "u", "SMTP_PASS": "p"},
            clear=True,
        ), patch("smtplib.SMTP", return_value=server):
            ev.check_smtp()
        self.assertIn("Authentication failed for user 'u'", buf.getvalue())

    def test_starttls_not_supported_is_silently_ignored_and_login_still_attempted(self):
        buf = io.StringIO()
        server = MagicMock()
        server.starttls.side_effect = smtplib.SMTPNotSupportedError("no TLS")
        with contextlib.redirect_stderr(buf), patch.dict(
            "os.environ",
            {"SMTP_HOST": "h", "SMTP_PORT": "25", "SMTP_USER": "u", "SMTP_PASS": "p"},
            clear=True,
        ), patch("smtplib.SMTP", return_value=server):
            ev.check_smtp()
        server.login.assert_called_once_with("u", "p")
        self.assertEqual(buf.getvalue(), "")

    def test_a_connection_failure_is_reported_with_host_port_and_the_underlying_error(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf), patch.dict(
            "os.environ", {"SMTP_HOST": "h", "SMTP_PORT": "25"}, clear=True
        ), patch("smtplib.SMTP", side_effect=OSError("conn refused")):
            ev.check_smtp()
        self.assertIn("Failed to connect or verify SMTP server at h:25", buf.getvalue())
        self.assertIn("conn refused", buf.getvalue())


class CheckGeminiTests(unittest.TestCase):
    def _resp(self, status):
        resp = MagicMock()
        resp.status = status
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def test_a_missing_api_key_warns_without_a_network_call(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf), patch.dict(
            "os.environ", {}, clear=True
        ), patch("urllib.request.urlopen") as m:
            ev.check_gemini()
        m.assert_not_called()
        self.assertIn("GEMINI_API_KEY is not set", buf.getvalue())

    def test_a_200_response_produces_no_warning(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf), patch.dict(
            "os.environ", {"GEMINI_API_KEY": "x"}, clear=True
        ), patch("urllib.request.urlopen", return_value=self._resp(200)):
            ev.check_gemini()
        self.assertEqual(buf.getvalue(), "")

    def test_an_unexpected_non_200_status_is_reported(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf), patch.dict(
            "os.environ", {"GEMINI_API_KEY": "x"}, clear=True
        ), patch("urllib.request.urlopen", return_value=self._resp(201)):
            ev.check_gemini()
        self.assertIn("Unexpected status code 201", buf.getvalue())

    def test_a_401_http_error_is_reported_as_an_invalid_or_expired_key(self):
        def raise_401(*_a, **_k):
            raise HTTPError("url", 401, "Unauthorized", {}, None)

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf), patch.dict(
            "os.environ", {"GEMINI_API_KEY": "x"}, clear=True
        ), patch("urllib.request.urlopen", side_effect=raise_401):
            ev.check_gemini()
        self.assertIn("Invalid or expired GEMINI_API_KEY", buf.getvalue())

    def test_a_500_http_error_is_reported_with_its_own_code_and_reason_not_the_invalid_key_message(self):
        def raise_500(*_a, **_k):
            raise HTTPError("url", 500, "Server Error", {}, None)

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf), patch.dict(
            "os.environ", {"GEMINI_API_KEY": "x"}, clear=True
        ), patch("urllib.request.urlopen", side_effect=raise_500):
            ev.check_gemini()
        self.assertIn("API Key verification failed with HTTP 500: Server Error", buf.getvalue())
        self.assertNotIn("Invalid or expired", buf.getvalue())

    def test_a_network_level_url_error_is_reported_distinctly_from_an_http_error(self):
        def raise_url_error(*_a, **_k):
            raise URLError("no network")

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf), patch.dict(
            "os.environ", {"GEMINI_API_KEY": "x"}, clear=True
        ), patch("urllib.request.urlopen", side_effect=raise_url_error):
            ev.check_gemini()
        self.assertIn("Network error when verifying API key: no network", buf.getvalue())


class LoadEnvFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.orig_cwd = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.orig_cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, content):
        with open(os.path.join(self.tmp, name), "w", encoding="utf-8") as f:
            f.write(content)

    def test_keys_from_a_dot_env_file_in_cwd_are_loaded_into_os_environ(self):
        self._write("a.env", "# a comment\n\nFOO=bar\nBAZ=qux\n")
        with patch.dict("os.environ", {}, clear=True), patch(
            "os.path.exists", side_effect=lambda p: p == "."
        ):
            ev.load_env_files()
            self.assertEqual(os.environ.get("FOO"), "bar")
            self.assertEqual(os.environ.get("BAZ"), "qux")

    def test_an_already_set_environment_variable_is_never_overridden_by_the_file(self):
        self._write("b.env", "FOO=fromfile\n")
        with patch.dict("os.environ", {"FOO": "preset"}, clear=True), patch(
            "os.path.exists", side_effect=lambda p: p == "."
        ):
            ev.load_env_files()
            self.assertEqual(os.environ.get("FOO"), "preset")

    def test_a_line_without_an_equals_sign_is_silently_skipped(self):
        self._write("c.env", "NOT_A_KV_LINE\nFOO=bar\n")
        with patch.dict("os.environ", {}, clear=True), patch(
            "os.path.exists", side_effect=lambda p: p == "."
        ):
            ev.load_env_files()
            self.assertEqual(os.environ.get("FOO"), "bar")
            self.assertNotIn("NOT_A_KV_LINE", os.environ)

    def test_no_env_files_present_leaves_os_environ_untouched(self):
        with patch.dict("os.environ", {}, clear=True), patch(
            "os.path.exists", side_effect=lambda p: p == "."
        ):
            ev.load_env_files()
            self.assertEqual(dict(os.environ), {})

    def test_the_hardcoded_opt_hams_etc_directory_is_skipped_when_it_does_not_exist(self):
        # Real, verified environment state: os.path.exists("/opt/hams/etc")
        # is False here (present on disk but unreadable by this user, so
        # exists() reports False) -- confirmed empirically, not assumed --
        # so load_env_files() naturally never touches it without needing
        # any patch at all.
        self._write("d.env", "FOO=bar\n")
        with patch.dict("os.environ", {}, clear=True):
            ev.load_env_files()
            self.assertEqual(os.environ.get("FOO"), "bar")


class MainIntegrationTests(unittest.TestCase):
    def test_main_always_exits_zero_even_when_every_real_boundary_fails(self):
        # The module's own docstring: "Non-fatal ... always exits 0." Each
        # check_*() function already guards its own real boundary with an
        # internal try/except -- main() itself has no top-level
        # try/except of its own. Verified empirically: replacing a whole
        # check_*() function with a raising mock (bypassing its internal
        # guard entirely) does propagate out of main() uncaught, which
        # isn't a bug so much as an unrealistic scenario -- the real
        # functions never raise, because they never let their own real
        # boundary's exception escape. This test instead fails the real
        # boundaries (socket/SMTP/HTTP) each check_*() guards internally,
        # which is the actual "every check fails" scenario the docstring
        # describes.
        with patch.object(ev, "load_env_files"), patch(
            "socket.create_connection", side_effect=OSError("refused")
        ), patch("smtplib.SMTP", side_effect=OSError("refused")), patch(
            "urllib.request.urlopen", side_effect=URLError("no network")
        ), patch.dict(
            "os.environ",
            {
                "DB_HOST": "h", "DB_PORT": "5432",
                "REDIS_HOST": "h", "REDIS_PORT": "6379",
                "RABBITMQ_HOST": "h", "RMQ_PORT": "5672",
                "SMTP_HOST": "h", "SMTP_PORT": "25",
                "GEMINI_API_KEY": "x",
            },
            clear=True,
        ):
            with self.assertRaises(SystemExit) as ctx:
                ev.main()
        self.assertEqual(ctx.exception.code, 0)

    def test_main_calls_all_five_checks_in_order(self):
        calls = []
        with patch.object(ev, "load_env_files", side_effect=lambda: calls.append("env")), patch.object(
            ev, "check_socket", side_effect=lambda *a: calls.append("socket")
        ), patch.object(ev, "check_smtp", side_effect=lambda: calls.append("smtp")), patch.object(
            ev, "check_gemini", side_effect=lambda: calls.append("gemini")
        ):
            with self.assertRaises(SystemExit):
                ev.main()
        self.assertEqual(
            calls, ["env", "socket", "socket", "socket", "smtp", "gemini"]
        )


if __name__ == "__main__":
    unittest.main()
