#!/usr/bin/env python3
# Copyright © Bruce Perens K6BP. All Rights Reserved.
"""Tests for mutation_harness.py: mutants are only ever written inside a throwaway copy, never through a symlink
into another checkout (incident 2026-11-09)."""
import ast
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import mutation_harness  # noqa: E402


class RequireInsideCopyTests(unittest.TestCase):
    def test_file_inside_copy_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = os.path.join(tmp, "w0")
            os.makedirs(os.path.join(work, "tools"))
            path = os.path.join(work, "tools", "a.py")
            open(path, "w").close()
            self.assertEqual(mutation_harness.require_inside_copy(path, work), os.path.realpath(path))

    def test_symlinked_directory_into_other_checkout_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            main = os.path.join(tmp, "main_checkout", "tools")
            os.makedirs(main)
            target = os.path.join(main, "gate.py")
            open(target, "w").close()
            work = os.path.join(tmp, "w0")
            os.makedirs(work)
            os.symlink(os.path.join(tmp, "main_checkout"), os.path.join(work, "hams_shared"))
            with self.assertRaises(SystemExit) as ctx:
                mutation_harness.require_inside_copy(os.path.join(work, "hams_shared", "tools", "gate.py"), work)
            self.assertIn("SAFETY", str(ctx.exception))
            self.assertTrue(os.path.exists(target))

    def test_symlinked_file_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside = os.path.join(tmp, "outside.py")
            open(outside, "w").close()
            work = os.path.join(tmp, "w0")
            os.makedirs(work)
            os.symlink(outside, os.path.join(work, "a.py"))
            with self.assertRaises(SystemExit):
                mutation_harness.require_inside_copy(os.path.join(work, "a.py"), work)

    def test_sibling_directory_with_same_prefix_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = os.path.join(tmp, "w0")
            sibling = os.path.join(tmp, "w01")
            os.makedirs(work)
            os.makedirs(sibling)
            path = os.path.join(sibling, "a.py")
            open(path, "w").close()
            with self.assertRaises(SystemExit):
                mutation_harness.require_inside_copy(path, work)


class MutantGenerationTests(unittest.TestCase):
    def test_comparison_and_return_mutants_are_generated_and_apply(self):
        tree = ast.parse("def gate(x):\n    if x > 3:\n        return True\n    return False\n")
        muts = mutation_harness.enumerate_mutants(tree, None)
        kinds = {d for d, _, _ in muts}
        self.assertIn("cmp Gt->GtE", kinds)
        self.assertIn("return bool flip", kinds)
        flipped = [ast.unparse(mutation_harness.apply(tree, m)) for d, _, m in muts if d == "cmp Gt->GtE"]
        self.assertIn("x >= 3", flipped[0])


if __name__ == "__main__":
    unittest.main()
