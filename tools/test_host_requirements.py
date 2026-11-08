# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for host_requirements.py: what a HAMS_ISOLATED_NS=1 test run needs from the host."""
import io
import unittest
import unittest.mock
from contextlib import redirect_stderr
from unittest.mock import patch

import host_requirements as hr


def _missing(**overrides):
    probes = dict(environ={hr.CRYPTO_KEY_VARIABLE: "k"}, account_exists=lambda name: True, is_directory=lambda p: True,
                  login_keys=lambda: {"DB_USER", "DB_PASS", "DB_HOST"}, git_trusts_all=lambda: True,
                  resolves=lambda name: True, account_names=["hamsd_a", "hamsd_b"])
    probes.update(overrides)
    return hr.missing_requirements(**probes)


class MissingRequirementsTests(unittest.TestCase):
    # Tests [@ANCHOR: host_requirements:missing_requirements]
    # Tests [@ANCHOR: host_requirements:constants]

    def test_a_complete_host_lacks_nothing(self):
        self.assertEqual(_missing(), [])

    def test_each_gap_is_reported_with_its_own_code(self):
        cases = {
            "crypto_key": dict(environ={}),
            "accounts": dict(account_exists=lambda name: name != "hamsd_b"),
            "backups_directory": dict(is_directory=lambda p: False),
            "cache_manager_login": dict(login_keys=lambda: None),
            "git_safe_directory": dict(git_trusts_all=lambda: False),
            "service_host": dict(resolves=lambda name: False),
        }
        for code, override in cases.items():
            with self.subTest(code=code):
                self.assertEqual([c for c, _text in _missing(**override)], [code])

    def test_the_accounts_line_names_only_the_missing_accounts(self):
        (_code, text), = _missing(account_exists=lambda name: name == "hamsd_a")
        self.assertIn("hamsd_b", text)
        self.assertNotIn("hamsd_a", text)

    def test_a_login_file_without_a_password_is_missing(self):
        (code, text), = _missing(login_keys=lambda: {"DB_USER"})
        self.assertEqual(code, "cache_manager_login")
        self.assertIn("DB_USER or DB_PASS", text)

    def test_provisioning_skips_the_crypto_key_check(self):
        self.assertEqual(_missing(environ={}, check_crypto_key=False), [])

    def test_an_empty_crypto_key_counts_as_unset(self):
        self.assertEqual([c for c, _t in _missing(environ={hr.CRYPTO_KEY_VARIABLE: ""})], ["crypto_key"])

    def test_the_account_list_is_the_manifests_test_accounts(self):
        names = hr.test_account_names()
        self.assertIn("hamsd_cache_manager", names)
        self.assertNotIn("", names)


class TestAccountNamesTests(unittest.TestCase):
    # Tests [@ANCHOR: host_requirements:test_account_names]

    def test_only_accounts_for_the_test_environment_are_listed(self):
        manifest = {"system_accounts": [{"user": "a", "environments": ["prod", "test"]},
                                        {"user": "b", "environments": ["prod"]},
                                        {"user": "c"}]}
        with patch.object(hr.infrastructure, "_spec_selected", return_value=True):
            self.assertEqual(hr.test_account_names(manifest), ["a", "c"])

    def test_an_account_for_another_host_class_is_left_out(self):
        manifest = {"system_accounts": [{"user": "a", "environments": ["test"]}]}
        with patch.object(hr.infrastructure, "_spec_selected", return_value=False):
            self.assertEqual(hr.test_account_names(manifest), [])


class RefusalTests(unittest.TestCase):
    # Tests [@ANCHOR: host_requirements:refusal_text]
    # Tests [@ANCHOR: host_requirements:require_test_host]

    def test_provisioning_gaps_name_the_one_command(self):
        text = hr.refusal_text([("accounts", "system account(s) missing: hamsd_a")])
        self.assertIn("hamsd_a", text)
        self.assertIn(hr.FIX_COMMAND, text)
        self.assertNotIn("source", text)

    def test_a_missing_key_explains_sudo_and_does_not_send_the_user_to_provisioning(self):
        text = hr.refusal_text([("crypto_key", "HAMS_CRYPTO_KEY is not set")])
        self.assertIn(hr.PROFILE_FILE, text)
        self.assertIn('sudo -n -u odoo env HAMS_ISOLATED_NS=1 HAMS_CRYPTO_KEY="$HAMS_CRYPTO_KEY"', text)
        self.assertNotIn(hr.FIX_COMMAND, text)

    def test_a_host_with_gaps_exits_1_and_says_so_on_stderr(self):
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            hr.require_test_host(missing=[("backups_directory", "/var/lib/odoo/backups does not exist")])
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("/var/lib/odoo/backups", err.getvalue())

    def test_a_complete_host_returns_quietly(self):
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertIsNone(hr.require_test_host(missing=[]))
        self.assertEqual(err.getvalue(), "")

    def test_without_an_explicit_list_it_probes_the_host(self):
        with patch.object(hr, "missing_requirements", return_value=[("service_host", "no redis")]) as probe, \
                redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            hr.require_test_host({"X": "1"})
        probe.assert_called_once_with({"X": "1"})


class TestPyWiringTests(unittest.TestCase):
    """test.py stops first, under HAMS_ISOLATED_NS=1 only (the namespaced path provisions these itself)."""
    # Tests [@ANCHOR: host_requirements:require_test_host]

    @classmethod
    def setUpClass(cls):
        import importlib.util
        import os
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test.py")
        spec = importlib.util.spec_from_file_location("_hams_test_runner_for_host_requirements", path)
        cls.runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.runner)

    def test_the_isolated_run_checks_the_host_before_selecting_modules(self):
        with patch.dict("os.environ", {"HAMS_ISOLATED_NS": "1"}), \
                patch.object(self.runner.host_requirements, "require_test_host", side_effect=SystemExit(1)) as check, \
                patch.object(self.runner, "select_modules") as select:
            with self.assertRaises(SystemExit):
                self.runner.run_preflight(unittest.mock.Mock(config="ignore_list.txt"))
        check.assert_called_once_with()
        select.assert_not_called()

    def test_the_namespaced_run_does_not_check_the_host(self):
        env = {k: v for k, v in __import__("os").environ.items() if k != "HAMS_ISOLATED_NS"}
        with patch.dict("os.environ", env, clear=True), \
                patch.object(self.runner.host_requirements, "require_test_host") as check, \
                patch.object(self.runner, "select_modules", side_effect=RuntimeError("past the check")), \
                patch.object(self.runner, "load_ignore_file"):
            with self.assertRaises(RuntimeError):
                self.runner.run_preflight(unittest.mock.Mock(config="ignore_list.txt"))
        check.assert_not_called()
