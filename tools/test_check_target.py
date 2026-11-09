#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Tests for check_target.py and for the rule it enforces on every `check_*.py` linter: a target directory that does not exist, is
not a directory, or holds none of the files the checker reads is exit 2 with the cause named, never "Checked 0 files ... Passed".

`assert_checker_refuses_a_bad_target` is the shared negative control: each batch of converted checkers has its own test file
(test_check_target_*.py) that calls it for every checker it converted.
"""

import os
import subprocess
import sys
import tempfile
import unittest

import check_target

HERE = os.path.dirname(os.path.abspath(__file__))


def checker_environment(**extra):
    """The environment a checker has when a person runs it by hand: no opt-out."""
    env = {k: v for k, v in os.environ.items() if k != check_target.ALLOW_EMPTY_ENV}
    env.update(extra)
    return env


def run_checker(script, *args, env=None, cwd=None):
    return subprocess.run(
        [sys.executable, os.path.join(HERE, script), *args],
        capture_output=True,
        text=True,
        env=checker_environment() if env is None else env,
        cwd=cwd or HERE,
        timeout=300,
    )


def assert_checker_refuses_a_bad_target(case, script, extra_args=(), empty=True, file_target=True):
    """Run `script` on a missing path, a plain file and an empty directory; each must exit 2 and say why.

    `extra_args` goes after the target (a checker that takes more arguments). `empty=False` skips the empty-directory case for a
    checker that documents an empty scan as normal; `file_target=False` skips the plain-file case for one that accepts a file."""
    with tempfile.TemporaryDirectory() as tmp:
        missing = os.path.join(tmp, "no_such_directory")
        done = run_checker(script, missing, *extra_args, cwd=tmp)
        case.assertEqual(done.returncode, 2, f"{script} on a missing directory:\n{done.stdout}{done.stderr}")
        case.assertIn("does not exist", done.stdout + done.stderr)
        case.assertNotIn("Passed", done.stdout)
        if file_target:
            a_file = os.path.join(tmp, "a_file.txt")
            with open(a_file, "w", encoding="utf-8") as handle:
                handle.write("x\n")
            done = run_checker(script, a_file, *extra_args, cwd=tmp)
            case.assertEqual(done.returncode, 2, f"{script} on a file:\n{done.stdout}{done.stderr}")
            case.assertIn("not a directory", done.stdout + done.stderr)
        if empty:
            nothing = os.path.join(tmp, "empty")
            os.mkdir(nothing)
            done = run_checker(script, nothing, *extra_args, cwd=tmp)
            case.assertEqual(done.returncode, 2, f"{script} on an empty directory:\n{done.stdout}{done.stderr}")
            case.assertIn("scanned 0", done.stdout + done.stderr)
            case.assertIn(check_target.ALLOW_EMPTY_ENV, done.stdout + done.stderr)
            # The documented opt-out turns only the empty-scan failure into a pass.
            done = run_checker(script, nothing, *extra_args, env=checker_environment(**{check_target.ALLOW_EMPTY_ENV: "1"}), cwd=tmp)
            case.assertEqual(done.returncode, 0, f"{script} on an empty directory with the opt-out:\n{done.stdout}{done.stderr}")
            done = run_checker(script, missing, *extra_args, env=checker_environment(**{check_target.ALLOW_EMPTY_ENV: "1"}), cwd=tmp)
            case.assertEqual(done.returncode, 2, f"{script} on a missing directory with the opt-out:\n{done.stdout}{done.stderr}")


class RequireDirectoryTests(unittest.TestCase):
    def test_a_missing_path_exits_2_naming_the_path_and_the_next_action(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(SystemExit) as raised:
            check_target.require_directory(os.path.join(tmp, "gone"), "check_x")
        self.assertEqual(raised.exception.code, 2)

    def test_a_file_exits_2(self):
        with tempfile.NamedTemporaryFile() as handle, self.assertRaises(SystemExit) as raised:
            check_target.require_directory(handle.name, "check_x")
        self.assertEqual(raised.exception.code, 2)

    def test_a_directory_is_returned_as_an_absolute_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(check_target.require_directory(tmp, "check_x"), os.path.abspath(tmp))


class RequireScannedTests(unittest.TestCase):
    def test_zero_files_exits_2_and_names_the_checker_the_kind_the_cause_and_the_opt_out(self):
        err = tempfile.TemporaryFile("w+")
        real, sys.stderr = sys.stderr, err
        try:
            with self.assertRaises(SystemExit) as raised:
                check_target.require_scanned(0, "/some/root", "check_x", "Python files", environ={})
        finally:
            sys.stderr = real
        self.assertEqual(raised.exception.code, 2)
        err.seek(0)
        text = err.read()
        for needle in ("check_x", "0 Python files", "/some/root", "Cause:", "Next:", check_target.ALLOW_EMPTY_ENV):
            self.assertIn(needle, text)

    def test_a_positive_count_passes_through(self):
        self.assertEqual(check_target.require_scanned(3, "/r", "check_x", "files", environ={}), 3)

    def test_the_opt_out_accepts_zero(self):
        self.assertEqual(check_target.require_scanned(0, "/r", "check_x", "files", environ={check_target.ALLOW_EMPTY_ENV: "1"}), 0)

    def test_only_the_exact_value_1_opts_out(self):
        for value in ("", "0", "true", "yes", "2"):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                check_target.require_scanned(0, "/r", "check_x", "files", environ={check_target.ALLOW_EMPTY_ENV: value})


class RunLintersOptOutTests(unittest.TestCase):
    def test_a_checker_step_carries_the_opt_out_and_other_steps_inherit(self):
        import run_linters

        env = run_linters.checker_env(["python3", "/x/tools/check_shebang.py", "/repo"])
        self.assertEqual(env[check_target.ALLOW_EMPTY_ENV], "1")
        self.assertIsNone(run_linters.checker_env(["python3", "-m", "pytest", "tools/test_check_shebang.py"]))
        self.assertIsNone(run_linters.checker_env(["flake8", "."]))
        self.assertIsNone(run_linters.checker_env(None))


if __name__ == "__main__":
    unittest.main()
