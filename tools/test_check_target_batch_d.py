#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Negative controls for batch D of the check_target conversion (check_boundary_hazards, check_secret_storage, check_settings_secrets):
each one exits 2 with the cause when its target is missing, is a file, or (opt-out unset) holds none of the files it reads.
"""

import unittest

import test_check_target as shared

CHECKERS = ("check_boundary_hazards.py", "check_secret_storage.py", "check_settings_secrets.py")


class BatchDRefuseABadTargetTests(unittest.TestCase):
    def test_every_checker_refuses_a_missing_path_a_file_and_an_empty_directory(self):
        for script in CHECKERS:
            with self.subTest(checker=script):
                shared.assert_checker_refuses_a_bad_target(self, script)


if __name__ == "__main__":
    unittest.main()
