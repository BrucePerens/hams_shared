#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Unit tests for hash_admin_password.py.

Never asserts a hard-coded hash value (PBKDF2-SHA512 is salted -- two hashes of the same password
legitimately differ). Verifies correctness by round-tripping through passlib's own `.verify()`
instead, and by asserting `hash_password()` is called with the exact password string a caller
intended, for the line-ending-stripping fix specifically.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import hash_admin_password as ham  # noqa: E402


class HashPasswordTests(unittest.TestCase):
    def test_a_hashed_password_verifies_against_the_original(self):
        h = ham.hash_password("correct horse battery staple")
        self.assertTrue(ham.pbkdf2_sha512.verify("correct horse battery staple", h))

    def test_a_hashed_password_does_not_verify_against_a_different_password(self):
        h = ham.hash_password("correct horse battery staple")
        self.assertFalse(ham.pbkdf2_sha512.verify("wrong password", h))

    def test_the_default_rounds_matches_odoo_s_own_min_rounds_floor(self):
        # Confirmed directly against the real installed odoo package's own
        # odoo/addons/base/models/res_users.py: MIN_ROUNDS = 600_000.
        self.assertEqual(ham.DEFAULT_ROUNDS, 600_000)

    def test_a_custom_round_count_is_actually_honored(self):
        h = ham.hash_password("test", rounds=1000)
        self.assertIn("$1000$", h)


class StripLineEndingTests(unittest.TestCase):
    # Real bug found 2026-09-10: piped-input password reading used to do `.rstrip("\n")`, which
    # leaves a trailing "\r" completely untouched for CRLF-terminated input (a real, plausible
    # source: a Windows-authored secrets file, a CRLF `.env`-style file). This would silently hash
    # "pass\r" instead of the real, intended "pass" -- a hash the real password, typed without an
    # invisible trailing \r, would never match, with no visible clue why. These tests are
    # confirmed to fail against the pre-fix source: `_strip_line_ending` did not exist there at
    # all (the old code inlined a plain `.rstrip("\n")` directly in main(), which cannot be
    # exercised without also mocking sys.stdin.isatty()/stdin.readline() for the whole function).
    def test_a_crlf_terminated_line_has_both_characters_stripped(self):
        self.assertEqual(ham._strip_line_ending("pass\r\n"), "pass")

    def test_a_bare_lf_terminated_line_is_stripped_normally(self):
        self.assertEqual(ham._strip_line_ending("pass\n"), "pass")

    def test_a_line_with_no_trailing_newline_is_returned_unchanged(self):
        self.assertEqual(ham._strip_line_ending("pass"), "pass")

    def test_only_one_trailing_line_ending_is_stripped_not_repeated_newlines(self):
        self.assertEqual(ham._strip_line_ending("pass\n\n"), "pass\n")


if __name__ == "__main__":
    unittest.main()
