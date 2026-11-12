#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for check_private_by_default.py."""

import json
import os
import tempfile
import unittest

import check_private_by_default as cpd  # noqa: E402


def _addon(root, name, internal):
    os.makedirs(os.path.join(root, name))
    with open(os.path.join(root, name, cpd.EXPOSURE_FILE), "w", encoding="utf-8") as handle:
        json.dump({"internal": internal}, handle)


class CountTests(unittest.TestCase):
    def test_counts_methods_per_addon_and_skips_addons_without_a_file(self):
        with tempfile.TemporaryDirectory() as root:
            _addon(root, "alpha", {"a.b": ["x", "y"], "a.c": ["z"]})
            _addon(root, "beta", {})
            os.makedirs(os.path.join(root, "gamma"))
            self.assertEqual(cpd.count_listed_methods(root), {"alpha": 3, "beta": 0})


class MainTests(unittest.TestCase):
    def test_any_listed_method_fails_and_an_empty_list_passes(self):
        with tempfile.TemporaryDirectory() as root:
            _addon(root, "beta", {})
            self.assertEqual(cpd.main([root]), 0)
            _addon(root, "alpha", {"a.b": ["x"]})
            self.assertEqual(cpd.main([root]), 1)


if __name__ == "__main__":
    unittest.main()
