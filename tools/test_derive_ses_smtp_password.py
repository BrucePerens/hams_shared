#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for derive_ses_smtp_password.py."""
import unittest

from derive_ses_smtp_password import calculate_smtp_password


class TestDeriveSesSmtpPassword(unittest.TestCase):
    # [@ANCHOR: test_derive_ses_smtp_password_matches_the_documented_test_vector]
    def test_00_matches_a_fixed_reference_vector(self):
        # Tests [@ANCHOR: derive_ses_smtp_password]
        # Uses AWS's own well-known documentation example secret access key
        # (never a real credential) as a fixed, reproducible input, so this test
        # catches an accidental change to the derivation algorithm's constants
        # (DATE/SERVICE/MESSAGE/TERMINAL/VERSION) without needing a live AWS
        # account to compare against.
        password = calculate_smtp_password(
            "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "us-east-1"
        )
        self.assertEqual(password, "BLBM/9hSUELfq8Gw+rU1YcBjkOxGbhT2XG763xVLGWL9")

    def test_01_different_regions_produce_different_passwords(self):
        # Tests [@ANCHOR: derive_ses_smtp_password]
        # The SMTP password is region-scoped -- using the wrong region's derivation
        # for a key would produce a password SES rejects, so this must not collide.
        secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        self.assertNotEqual(
            calculate_smtp_password(secret, "us-east-1"),
            calculate_smtp_password(secret, "us-west-2"),
        )

    def test_02_output_is_deterministic(self):
        # Tests [@ANCHOR: derive_ses_smtp_password]
        secret = "anotherSecretAccessKeyExampleValue1234567890"
        self.assertEqual(
            calculate_smtp_password(secret, "us-east-1"),
            calculate_smtp_password(secret, "us-east-1"),
        )


if __name__ == "__main__":
    unittest.main()
