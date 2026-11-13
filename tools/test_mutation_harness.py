#!/usr/bin/env python3
# Copyright © Bruce Perens K6BP. All Rights Reserved.
"""Tests for mutation_harness.py: mutants are only ever written inside a throwaway copy, never through a symlink
into another checkout (incident 2026-11-09)."""
import ast
import os
import sys
import tempfile
import time
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


class BudgetTests(unittest.TestCase):
    def test_budget_stops_new_mutants_and_says_partial(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "tree")
            os.makedirs(root)
            with open(os.path.join(root, "m.py"), "w") as handle:
                handle.write("def f(a):\n    return a + 1\n")
            with open(os.path.join(root, "test_m.py"), "w") as handle:
                handle.write("import m\n\ndef test_f():\n    assert m.f(1) == 2\n")
            done = subprocess.run([sys.executable, os.path.join(HERE, "mutation_harness.py"), "--root", root, "--file", "m.py", "--test",
                                   "python3 -m pytest -q -x test_m.py", "--workers", "1", "--work", os.path.join(tmp, "work"), "--budget", "0.000001"],
                                  capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertIn("PARTIAL: 0 of", done.stdout)


def _alive(pid):
    try:
        with open(f"/proc/{pid}/stat") as handle:
            return handle.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


class ProcessGroupTests(unittest.TestCase):
    """[@ANCHOR: mutation_harness:run_group] a timed-out mutant's whole process tree ends, not just the wrapper shell."""

    def test_timeout_ends_grandchild(self):
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = os.path.join(tmp, "pid")
            started = time.time()
            _, _, timed_out = mutation_harness.run_group(f"sh -c 'sleep 60 & echo $! > {pidfile}; wait'", tmp, dict(os.environ),
                                                         timeout=2, grace=1)
            self.assertTrue(timed_out)
            self.assertLess(time.time() - started, 15)
            pid = int(open(pidfile).read())
            for _ in range(30):
                if not _alive(pid):
                    break
                time.sleep(0.1)
            self.assertFalse(_alive(pid), "grandchild survived the timeout")

    def test_memory_cap_gives_nonzero_exit(self):
        code, _, timed_out = mutation_harness.run_group("python3 -c 'bytearray(2 * 1024**3)'", ".", dict(os.environ),
                                                        timeout=30, mem_limit_mb=512)
        self.assertFalse(timed_out)
        self.assertNotEqual(code, 0)

    def test_exit_zero_survives(self):
        code, out, _ = mutation_harness.run_group("echo ok", ".", dict(os.environ), timeout=10)
        self.assertEqual((code, out.strip()), (0, b"ok"))


if __name__ == "__main__":
    unittest.main()
