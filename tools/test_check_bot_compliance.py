#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for check_bot_compliance.py.

Unlike the repo-scanning checkers elsewhere in this sweep, this is a live
host diagnostic tool: get_public_ip() calls a real external HTTP endpoint
(api.ipify.org) and check_fcrdns() calls real DNS resolution. These tests
never make a real network or DNS call -- every external boundary is mocked
via unittest.mock.patch, which is the correct way to unit-test this script's
own branching logic without depending on live network state or a specific
host's DNS configuration.
"""

import json
import os
import socket
import sys
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check_bot_compliance as chk  # noqa: E402


class GetPublicIpTests(unittest.TestCase):
    @patch("check_bot_compliance.urllib.request.urlopen")
    def test_a_successful_response_returns_the_ip(self, mock_urlopen):
        mock_urlopen.return_value.__enter__ = MagicMock(return_value=mock_urlopen.return_value)
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value.read.return_value = json.dumps({"ip": "203.0.113.5"}).encode()
        self.assertEqual(chk.get_public_ip(), "203.0.113.5")

    @patch("check_bot_compliance.urllib.request.urlopen")
    def test_a_url_error_returns_none(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("network unreachable")
        self.assertIsNone(chk.get_public_ip())

    @patch("check_bot_compliance.urllib.request.urlopen")
    def test_malformed_json_returns_none(self, mock_urlopen):
        mock_urlopen.return_value.read.return_value = b"not json"
        self.assertIsNone(chk.get_public_ip())

    @patch("check_bot_compliance.urllib.request.urlopen")
    def test_a_timeout_returns_none(self, mock_urlopen):
        mock_urlopen.side_effect = OSError("timed out")
        self.assertIsNone(chk.get_public_ip())


class CheckFcrdnsTests(unittest.TestCase):
    @patch("check_bot_compliance.socket.gethostbyname")
    @patch("check_bot_compliance.socket.gethostbyaddr")
    def test_matching_forward_and_reverse_dns_passes(self, mock_reverse, mock_forward):
        mock_reverse.return_value = ("host.example.com", [], ["203.0.113.5"])
        mock_forward.return_value = "203.0.113.5"
        self.assertTrue(chk.check_fcrdns("203.0.113.5"))

    @patch("check_bot_compliance.socket.gethostbyname")
    @patch("check_bot_compliance.socket.gethostbyaddr")
    def test_forward_dns_resolving_to_a_different_ip_fails(self, mock_reverse, mock_forward):
        mock_reverse.return_value = ("host.example.com", [], ["203.0.113.5"])
        mock_forward.return_value = "198.51.100.9"
        self.assertFalse(chk.check_fcrdns("203.0.113.5"))

    @patch("check_bot_compliance.socket.gethostbyname")
    @patch("check_bot_compliance.socket.gethostbyaddr")
    def test_forward_lookup_raising_gaierror_fails(self, mock_reverse, mock_forward):
        mock_reverse.return_value = ("host.example.com", [], ["203.0.113.5"])
        mock_forward.side_effect = socket.gaierror("no such host")
        self.assertFalse(chk.check_fcrdns("203.0.113.5"))

    @patch("check_bot_compliance.socket.gethostbyaddr")
    def test_reverse_lookup_raising_herror_fails(self, mock_reverse):
        mock_reverse.side_effect = socket.herror("no PTR record")
        self.assertFalse(chk.check_fcrdns("203.0.113.5"))

    @patch("check_bot_compliance.socket.gethostbyaddr")
    def test_reverse_lookup_raising_a_generic_oserror_fails(self, mock_reverse):
        mock_reverse.side_effect = OSError("dns server unreachable")
        self.assertFalse(chk.check_fcrdns("203.0.113.5"))


class MainTests(unittest.TestCase):
    @patch("check_bot_compliance.check_fcrdns")
    @patch("check_bot_compliance.get_public_ip")
    def test_main_exits_1_when_no_public_ip_is_found(self, mock_get_ip, mock_fcrdns):
        mock_get_ip.return_value = None
        with self.assertRaises(SystemExit) as ctx:
            chk.main()
        self.assertEqual(ctx.exception.code, 1)
        mock_fcrdns.assert_not_called()

    @patch("check_bot_compliance.check_fcrdns")
    @patch("check_bot_compliance.get_public_ip")
    def test_main_exits_1_when_fcrdns_fails(self, mock_get_ip, mock_fcrdns):
        mock_get_ip.return_value = "203.0.113.5"
        mock_fcrdns.return_value = False
        with self.assertRaises(SystemExit) as ctx:
            chk.main()
        self.assertEqual(ctx.exception.code, 1)

    @patch("check_bot_compliance.check_fcrdns")
    @patch("check_bot_compliance.get_public_ip")
    def test_main_does_not_exit_nonzero_when_everything_passes(self, mock_get_ip, mock_fcrdns):
        mock_get_ip.return_value = "203.0.113.5"
        mock_fcrdns.return_value = True
        try:
            chk.main()
        except SystemExit as e:
            self.fail(f"main() should not call sys.exit on success, got exit({e.code})")


if __name__ == "__main__":
    unittest.main()
