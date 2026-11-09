#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Guard: every tools/check_*.py is either covered by the shared target validation (check_target.py) and a negative-control test that
names it, or listed here as exempt with the reason. A new checker that scans a directory and silently passes on a missing or empty one
fails this test until it is converted or the exemption is argued (docs/GATE_AUDIT.md, "how to write a gate that can fail").
"""

import glob
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
# The helper itself is not a checker.
HELPER = "check_target.py"

# checker -> why it has no target directory to validate. An exemption is a claim: the test fails when the checker starts using
# check_target (the entry is stale) or when the file is gone.
EXEMPT = {
    "check_backup_encryption_changes.py": "reads a git diff of the checkout, not a directory of sources; an unreadable diff is its own failure",
    "check_claims_freshness.py": "reads git's file list and fails on a non-git tree or an unreadable claim (hams_shared #569)",
    "check_no_rule_lost.py": "compares the files named by --before/--after; a missing file is an error of its own",
    "check_private_by_default.py": "a ratchet over rpc_exposure.json files found by the diff, with its own missing-file handling",
    "check_bot_compliance.py": "probes this host's public IP and reverse DNS; it scans nothing, and an unanswered probe already exits 1",
    "check_staged_files.py": "a git hook over the index; nothing staged is the normal case, and a failed git call raises",
    "check_summation_bias.py": "compares staged or changed files with HEAD; it has no directory scan",
}


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class EveryCheckerIsCoveredOrExemptTests(unittest.TestCase):
    def checkers(self):
        found = sorted(os.path.basename(p) for p in glob.glob(os.path.join(HERE, "check_*.py")) if os.path.basename(p) != HELPER)
        self.assertGreater(len(found), 40, found)
        return found

    def negative_control_text(self):
        return "\n".join(read(p) for p in sorted(glob.glob(os.path.join(HERE, "test_check_*.py"))) if "test_check_target_coverage" not in p)

    def test_every_checker_uses_check_target_or_is_exempt(self):
        missing = [c for c in self.checkers() if c not in EXEMPT and "import check_target" not in read(os.path.join(HERE, c))]
        self.assertEqual(missing, [], "these checkers pass on a missing or empty target: convert them to check_target.py or add them to EXEMPT with the reason")

    def test_every_converted_checker_is_named_by_a_negative_control_test(self):
        controls = self.negative_control_text()
        unnamed = [c for c in self.checkers() if c not in EXEMPT and c not in controls and c[: -len(".py")] not in controls]
        self.assertEqual(unnamed, [], "these converted checkers have no test that feeds them a missing or empty target")

    def test_an_exemption_is_not_stale(self):
        for name, reason in EXEMPT.items():
            with self.subTest(checker=name):
                path = os.path.join(HERE, name)
                self.assertTrue(os.path.isfile(path), f"{name} no longer exists: remove its exemption")
                self.assertNotIn("import check_target", read(path), f"{name} now uses check_target: remove its exemption ({reason})")
                self.assertTrue(reason.strip())


if __name__ == "__main__":
    unittest.main()
