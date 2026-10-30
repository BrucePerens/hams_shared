#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
The pass stamp `run_linters.py` writes, the gate that reads it (`lint_stamp_gate.py`, called by
devbox_tools/prmerge.sh), and the two ways a `--changed` run used to prove nothing about a Rust
branch (2026-10-30): it was started through a worktree's `hams_shared` link and selected zero
steps, and its stamp did not say which steps ran.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)

import lint_stamp_gate  # noqa: E402
import run_linters  # noqa: E402
from test_run_linters_changed_selection import SelectionFixture, _base, _git, _write  # noqa: E402

RUST_NAMES = (
    "check_cargo_clippy,check_cargo_fmt,check_rust_function_test_anchors,verify_anchors,flake8"
)


def stamp(count, names, mode="changed"):
    return f"{mode} 2026-10-30T10:00:00 steps={count} names={names}\n"


class GateTests(unittest.TestCase):
    def test_a_stamp_that_selected_zero_steps_is_refused_for_a_code_change(self):
        reason = lint_stamp_gate.refusal(stamp(0, ""), ["daemons/x/src/main.rs"])
        self.assertIn("zero steps", reason)

    def test_a_stamp_without_the_rust_steps_is_refused_for_a_rust_change(self):
        reason = lint_stamp_gate.refusal(stamp(3, "flake8,eslint"), ["daemons/x/src/a.rs"])
        self.assertIn("check_cargo_clippy", reason)
        self.assertIn("check_rust_function_test_anchors", reason)

    def test_a_cargo_lock_change_needs_the_rust_steps_too(self):
        self.assertTrue(lint_stamp_gate.refusal(stamp(3, "flake8"), ["daemons/x/Cargo.lock"]))

    def test_a_stamp_with_the_rust_steps_is_accepted_for_a_rust_change(self):
        self.assertIsNone(lint_stamp_gate.refusal(stamp(9, RUST_NAMES), ["daemons/x/src/a.rs"]))

    def test_a_javascript_change_needs_the_javascript_checker(self):
        self.assertTrue(lint_stamp_gate.refusal(stamp(2, "flake8"), ["m/static/a.js"]))
        self.assertIsNone(lint_stamp_gate.refusal(stamp(2, "check_js_syntax,eslint"), ["m/static/a.js"]))

    def test_a_stamp_from_a_full_run_is_accepted_for_any_change(self):
        text = stamp(70, "", mode="full")
        self.assertIsNone(lint_stamp_gate.refusal(text, ["daemons/x/src/a.rs", "m/a.js"]))

    def test_a_markdown_only_change_needs_no_steps(self):
        self.assertIsNone(lint_stamp_gate.refusal(stamp(0, ""), ["docs/a.md"]))

    def test_an_old_stamp_without_a_step_list_is_refused_for_rust_and_accepted_for_python(self):
        old = "changed 2026-10-29T10:00:00\n"
        self.assertTrue(lint_stamp_gate.refusal(old, ["daemons/x/src/a.rs"]))
        self.assertIsNone(lint_stamp_gate.refusal(old, ["tools/a.py"]))

    def test_the_command_line_refuses_with_exit_status_one(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "stamp")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(stamp(0, ""))
            result = subprocess.run(
                [sys.executable, os.path.join(HERE, "lint_stamp_gate.py"), path],
                input="daemons/x/src/main.rs\n",
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("REFUSING MERGE", result.stdout)


class StampTextTests(unittest.TestCase):
    def test_the_stamp_counts_and_names_the_selected_steps(self):
        text = run_linters.stamp_text(
            "changed", ["flake8", "check_async_lock_scope[a]", "check_async_lock_scope[b]", "pytest x/y"]
        )
        mode, count, names = lint_stamp_gate.parse_stamp(text)
        self.assertEqual((mode, count), ("changed", 4))
        self.assertEqual(names, {"flake8", "check_async_lock_scope", "pytest"})

    def test_a_run_started_through_a_hams_shared_link_is_rooted_at_the_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            real = os.path.join(directory, "main", "hams_shared")
            os.makedirs(os.path.join(real, "tools"))
            worktree = os.path.join(directory, "wt")
            os.makedirs(worktree)
            os.symlink(real, os.path.join(worktree, "hams_shared"))
            script = os.path.join(worktree, "hams_shared", "tools", "run_linters.py")
            self.assertEqual(run_linters.invocation_root(script), worktree)
            plain = os.path.join(real, "tools", "run_linters.py")
            self.assertEqual(run_linters.invocation_root(plain), real)

    def test_a_linked_hams_shared_is_not_stamped_by_a_run_that_never_scanned_it(self):
        with tempfile.TemporaryDirectory() as directory:
            real = os.path.realpath(os.path.join(directory, "main", "hams_shared"))
            os.makedirs(os.path.join(real, "tools"))
            worktree = os.path.realpath(os.path.join(directory, "wt"))
            os.makedirs(worktree)
            os.symlink(real, os.path.join(worktree, "hams_shared"))
            tools = os.path.join(worktree, "tools")
            os.symlink(os.path.join(real, "tools"), tools)
            self.assertEqual(run_linters.stamp_repos(worktree, tools, worktree), [worktree])
            self.assertEqual(run_linters.stamp_repos(worktree, tools, real), [worktree, real])

    def test_a_hams_shared_directory_inside_the_repository_is_stamped(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = os.path.realpath(directory)
            tools = os.path.join(repo, "hams_shared", "tools")
            os.makedirs(tools)
            self.assertEqual(
                run_linters.stamp_repos(repo, tools, repo), [repo, os.path.join(repo, "hams_shared")]
            )


class WorktreeWithLinkedHamsSharedTests(SelectionFixture):
    """A hams_open worktree whose `hams_shared` is the link `fix_worktree_symlinks.py` makes."""

    def setUp(self):
        super().setUp()
        self.worktree = os.path.join(self.ws, "wt-open")
        _git(self.open_repo, "worktree", "add", "-q", "--detach", self.worktree, "HEAD")
        os.symlink(self.shared, os.path.join(self.worktree, "hams_shared"))

    def tearDown(self):
        shutil.rmtree(self.worktree, ignore_errors=True)
        _git(self.open_repo, "worktree", "prune")

    def run_through(self, script_relative, *args):
        env = dict(os.environ)
        env["HAMS_ALLOW_STALE_SHARED"] = "1"
        return subprocess.run(
            [sys.executable, os.path.join(self.worktree, script_relative), *args],
            cwd=self.worktree,
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )

    def selected_in_worktree(self, script_relative, relative, text):
        _write(os.path.join(self.worktree, relative), text)
        result = self.run_through(script_relative, "--changed", "--list")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return {_base(line) for line in result.stdout.splitlines() if line.strip()}

    def test_a_rust_change_selects_the_rust_steps_through_the_tools_link(self):
        picked = self.selected_in_worktree(
            os.path.join("tools", "run_linters.py"), "daemons/bench/src/main.rs", "fn main() { }\n"
        )
        self.assertIn("check_cargo_clippy", picked)

    def test_a_rust_change_selects_the_rust_steps_through_the_hams_shared_link(self):
        picked = self.selected_in_worktree(
            os.path.join("hams_shared", "tools", "run_linters.py"),
            "daemons/bench/src/main.rs",
            "fn main() { }\n",
        )
        for expected in (
            "check_cargo_clippy",
            "check_cargo_fmt",
            "check_cargo_deny",
            "check_rust_function_test_anchors",
            "verify_anchors",
        ):
            self.assertIn(expected, picked)

    def test_a_javascript_change_selects_the_javascript_steps_through_the_hams_shared_link(self):
        picked = self.selected_in_worktree(
            os.path.join("hams_shared", "tools", "run_linters.py"),
            "zero_sudo/static/src/js/a.js",
            "export const a = 9;\n",
        )
        self.assertIn("check_js_syntax", picked)
        self.assertIn("eslint", picked)


if __name__ == "__main__":
    unittest.main()
