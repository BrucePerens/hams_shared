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

    def test_an_empty_or_unreadable_stamp_is_refused_for_a_code_change_but_not_for_markdown(self):
        for text in ("", "\n", "garbage 2026-10-30T10:00:00 steps=3 names=flake8\n"):
            self.assertIn("empty or unreadable", lint_stamp_gate.refusal(text, ["tools/a.py"]), repr(text))
            self.assertIsNone(lint_stamp_gate.refusal(text, ["docs/a.md"]), repr(text))

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

    def test_a_hams_shared_worktree_run_watches_the_worktree_head_not_the_throwaway_clone(self):
        layout = object()
        self.assertEqual(run_linters.stamped_head_repo("/clone/hams_open", "/work/wt", layout), "/work/wt")
        self.assertEqual(run_linters.stamped_head_repo("/work/repo", "/work/repo", None), "/work/repo")

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


class CrossRepositoryAnchorTests(SelectionFixture):
    """The pre-merge run started from hams_com must credit documentation that lives in hams_open."""

    def test_a_hams_com_test_that_cites_a_hams_open_feature_does_not_make_it_undocumented(self):
        os.symlink(os.path.join(self.shared, "tools"), os.path.join(self.com_repo, "tools"))
        _write(
            os.path.join(self.com_repo, "ham_x", "tests", "test_cites_open.py"),
            "# Tests [@ANCHOR: zero_sudo:persona_login]\n"
            "def test_login_from_hams_com():\n"
            "    from zero_sudo.models.model import persona_login\n"
            "    assert persona_login() == 1\n",
        )
        env = dict(os.environ)
        env["HAMS_ALLOW_STALE_SHARED"] = "1"
        result = subprocess.run(
            [sys.executable, os.path.join(self.com_repo, "tools", "run_linters.py"), "--only", "verify_anchors"],
            cwd=self.com_repo,
            capture_output=True,
            text=True,
            env=env,
            timeout=300,
        )
        text = result.stdout + result.stderr
        self.assertNotIn("completely missing from documentation", text)
        self.assertEqual(result.returncode, 0, text)


class InheritFixture(unittest.TestCase):
    """A real throwaway repository: `main` with one commit, and a branch whose commits are stamped (or not) by the tests."""

    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="inherit-")
        self.passed = os.path.join(self.repo, ".passed")
        self.addCleanup(shutil.rmtree, self.repo, True)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.invalid")
        self.git("config", "user.name", "t")
        self.commit("README.txt", "x\n", "base")
        self.git("branch", "-q", "origin_main")
        self.git("checkout", "-q", "-b", "work")
        self.messages = []

    def git(self, *args):
        done = subprocess.run(["git", "-C", self.repo, *args], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip()

    def commit(self, path, text, message):
        full = os.path.join(self.repo, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(text)
        self.git("add", path)
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def put_stamp(self, sha, line=None):
        os.makedirs(self.passed, exist_ok=True)
        with open(os.path.join(self.passed, sha), "w", encoding="utf-8") as handle:
            handle.write(line or stamp(3, "flake8,check_cargo_clippy,check_cargo_fmt,check_rust_function_test_anchors,verify_anchors"))

    def inherit(self, tip):
        return lint_stamp_gate.inherit_stamp(self.repo, tip, self.passed, base="origin_main", log=self.messages.append)


class InheritTests(InheritFixture):
    def test_a_commit_that_only_adds_records_inherits_the_stamp_and_names_its_source(self):
        code = self.commit("daemons/x/src/a.rs", "fn main() {}\n", "code")
        self.put_stamp(code)
        self.commit("night_shift_todo/medium/a.md", "# a\n", "claim")
        tip = self.commit("claims/b.md", "# b\n", "claim 2")
        text = self.inherit(tip)
        self.assertIn("inherited-from=" + code, text)
        self.assertTrue(text.startswith("changed "), "the old line is kept, so its step list is still judged")
        with open(os.path.join(self.passed, tip), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), text)
        self.assertIn("inherited from " + code[:12], lint_stamp_gate.inherited_note(text))
        self.assertIsNone(lint_stamp_gate.refusal(text, ["daemons/x/src/a.rs", "night_shift_todo/medium/a.md"]))

    def test_any_other_file_since_the_stamped_commit_forces_a_new_run(self):
        code = self.commit("daemons/x/src/a.rs", "fn main() {}\n", "code")
        self.put_stamp(code)
        for path in ("daemons/x/src/b.rs", "docs/README.md", "tools/test_x.py", "conftest.py", "night_shift_todo/a.txt", "night_shift_history/a.py"):
            tip = self.commit(path, "y\n", "other " + path)
            self.assertIsNone(self.inherit(tip), path)
            self.assertFalse(os.path.exists(os.path.join(self.passed, tip)), path)
            self.git("reset", "-q", "--hard", code)
        self.assertTrue(any("not only records" in m for m in self.messages))

    def test_a_record_mixed_with_a_code_change_forces_a_new_run(self):
        code = self.commit("daemons/x/src/a.rs", "fn main() {}\n", "code")
        self.put_stamp(code)
        self.commit("night_shift_todo/a.md", "# a\n", "claim")
        tip = self.commit("daemons/x/src/a.rs", "fn main() { }\n", "edit")
        self.assertIsNone(self.inherit(tip))

    def test_the_nearest_stamped_commit_decides_and_no_stamp_means_no_inheritance(self):
        first = self.commit("daemons/x/src/a.rs", "1\n", "one")
        self.put_stamp(first)
        second = self.commit("daemons/x/src/a.rs", "2\n", "two")  # never stamped
        tip = self.commit("night_shift_todo/a.md", "# a\n", "claim")
        self.assertIsNone(self.inherit(tip), "the stamp of `first` does not cover `second`")
        self.put_stamp(second)
        self.assertIn("inherited-from=" + second, self.inherit(tip))

    def test_a_stamp_of_the_tip_itself_is_never_replaced_and_a_branch_without_stamps_is_left_alone(self):
        tip = self.commit("night_shift_todo/a.md", "# a\n", "claim")
        self.assertIsNone(self.inherit(tip))
        self.put_stamp(tip, "full 2026-11-08T10:00:00\n")
        self.assertEqual(self.inherit(tip), "full 2026-11-08T10:00:00\n")

    def test_a_merge_of_main_is_not_a_record_change(self):
        code = self.commit("daemons/x/src/a.rs", "1\n", "code")
        self.put_stamp(code)
        self.git("checkout", "-q", "main")
        self.commit("tools/other.py", "z\n", "main moves")
        self.git("branch", "-f", "origin_main", "main")
        self.git("checkout", "-q", "work")
        self.git("merge", "-q", "--no-edit", "main")
        self.assertIsNone(self.inherit(self.git("rev-parse", "HEAD")))

    def test_an_invalid_history_entry_is_not_inherited(self):
        import night_shift_history

        self.commit("night_shift_history.md", night_shift_history.STUB_TEXT, "stub")
        code = self.commit("daemons/x/src/a.rs", "1\n", "code")
        self.put_stamp(code)
        tip = self.commit("night_shift_history/2026-11-08-000000-bad-aaaaaa.md", "no heading at all\n", "bad entry")
        self.assertIsNone(self.inherit(tip))
        self.assertTrue(any("not valid" in m for m in self.messages), self.messages)

    def test_the_exempt_list_is_narrow(self):
        for path in ("night_shift_history/2026-11-08-000000-a-aaaaaa.md", "night_shift_todo/medium/x.md", "claims/y.md"):
            self.assertTrue(lint_stamp_gate.exempt_path(path), path)
        for path in ("README.md", "docs/x.md", "night_shift_todo/x.py", "claims/../tools/x.md", "/claims/x.md", "claimsx/y.md",
                     "agents/skills/a/SKILL.md", "night_shift_history.md"):
            self.assertFalse(lint_stamp_gate.exempt_path(path), path)

    def test_the_command_line_writes_the_stamp_and_prmerge_calls_it(self):
        code = self.commit("daemons/x/src/a.rs", "1\n", "code")
        self.put_stamp(code)
        tip = self.commit("night_shift_todo/a.md", "# a\n", "claim")
        gate = os.path.join(HERE, "lint_stamp_gate.py")
        done = subprocess.run([sys.executable, gate, "--inherit", tip, self.passed, "origin_main"], cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("inherited-from=" + code, done.stdout)
        shown = subprocess.run([sys.executable, gate, os.path.join(self.passed, tip)], input="daemons/x/src/a.rs\n", capture_output=True, text=True)
        self.assertEqual(shown.returncode, 0, shown.stdout)
        self.assertIn("NOTE: this pass is inherited", shown.stdout)
        refused = subprocess.run([sys.executable, gate, "--inherit", code, self.passed, "origin_main"], cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(refused.returncode, 0, "a stamped commit answers with its own stamp")


if __name__ == "__main__":
    unittest.main()
