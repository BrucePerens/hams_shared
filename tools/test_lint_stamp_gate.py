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
from unittest import mock

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

    def test_a_full_stamp_that_ran_zero_steps_is_refused_for_a_code_change(self):
        reason = lint_stamp_gate.refusal(stamp(0, "", mode="full"), ["tools/a.py"])
        self.assertIn("zero steps", reason)

    def test_a_truncated_step_list_is_refused_for_a_rust_change(self):
        text = "changed 2026-10-30T10:00:00 steps=9 names=flake8,check_cargo_cl"
        self.assertIn("check_cargo_clippy", lint_stamp_gate.refusal(text, ["daemons/x/src/a.rs"]))

    def test_a_missing_stamp_file_exits_nonzero_on_the_command_line(self):
        result = subprocess.run(
            [sys.executable, os.path.join(HERE, "lint_stamp_gate.py"), "/nonexistent/stamp"],
            input="tools/a.py\n", capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)

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


class ParseAndRefusalExactnessTests(unittest.TestCase):
    """Found by mutation testing (2026-11-09): the gate's reasons, counts and step sets were only checked loosely."""

    def test_parse_stamp_reads_mode_count_and_names_exactly(self):
        self.assertEqual(lint_stamp_gate.parse_stamp("changed 2026-10-30T10:00:00 steps=3 names=a,b\n"), ("changed", 3, {"a", "b"}))
        self.assertEqual(lint_stamp_gate.parse_stamp("full steps=7"), ("full", 7, None))
        self.assertEqual(lint_stamp_gate.parse_stamp("changed steps=3 names=a"), ("changed", 3, {"a"}), "the field right after the mode counts")
        self.assertEqual(lint_stamp_gate.parse_stamp("steps=5"), ("steps=5", None, None), "the first word is the mode, never a field")
        self.assertEqual(lint_stamp_gate.parse_stamp(""), ("", None, None))
        self.assertEqual(lint_stamp_gate.parse_stamp("changed x steps=2 names="), ("changed", 2, set()))
        self.assertEqual(lint_stamp_gate.parse_stamp("changed xsteps=3 steps=3x"), ("changed", None, None))

    def test_the_unknown_mode_reason_names_the_word_and_the_known_modes(self):
        self.assertEqual(
            lint_stamp_gate.refusal("", ["a.py"]),
            "the stamp is empty or unreadable (its first word is '', not one of full/changed)",
        )
        self.assertIn("'bogus'", lint_stamp_gate.refusal("bogus steps=3", ["a.py"]))

    def test_the_zero_step_reason_is_exact(self):
        self.assertEqual(lint_stamp_gate.refusal("changed steps=0", ["a.py"]), "the stamp selected zero steps for a branch that changed code")

    def test_a_stamp_with_steps_but_no_names_is_judged_per_file_kind(self):
        old = "changed 2026-10-30T10:00:00 steps=4\n"
        reason = "the stamp does not list its steps (old run_linters.py): run it again"
        self.assertEqual(lint_stamp_gate.refusal(old, ["a/b.rs"]), reason)
        self.assertEqual(lint_stamp_gate.refusal(old, ["a/Cargo.toml"]), reason)
        self.assertEqual(lint_stamp_gate.refusal(old, ["a/b.js"]), reason)
        self.assertEqual(lint_stamp_gate.refusal(old, ["a/b.mjs"]), reason)
        self.assertEqual(lint_stamp_gate.refusal(old, ["a/b.cjs"]), reason)
        self.assertIsNone(lint_stamp_gate.refusal(old, ["a/b.py"]))
        self.assertIsNone(lint_stamp_gate.refusal(old, ["a/b.py", ""]), "blank lines are not paths")

    def test_the_rust_reason_lists_exactly_the_missing_steps(self):
        reason = lint_stamp_gate.refusal(stamp(2, "check_cargo_clippy,verify_anchors"), ["x.rs"])
        self.assertEqual(reason, "Rust files changed but the stamp did not run: check_cargo_fmt, check_rust_function_test_anchors")

    def test_the_javascript_reason_names_the_checker_and_rust_is_judged_first(self):
        self.assertEqual(
            lint_stamp_gate.refusal(stamp(1, "flake8"), ["x.cjs"]),
            "JavaScript files changed but the stamp did not run: check_js_syntax",
        )
        both = lint_stamp_gate.refusal(stamp(1, "flake8"), ["x.js", "y.rs"])
        self.assertTrue(both.startswith("Rust files changed"), both)
        self.assertIsNone(lint_stamp_gate.refusal(stamp(5, RUST_NAMES + ",check_js_syntax"), ["x.js", "y.rs"]))

    def test_exempt_path_rejects_traversal_absolute_paths_and_other_suffixes(self):
        self.assertTrue(lint_stamp_gate.exempt_path("claims/a.md"))
        self.assertFalse(lint_stamp_gate.exempt_path("claims/a.txt"))
        self.assertFalse(lint_stamp_gate.exempt_path("claims/../tools/a.md"))
        self.assertFalse(lint_stamp_gate.exempt_path("/claims/a.md"))
        self.assertFalse(lint_stamp_gate.exempt_path("docs/a.md"))
        self.assertTrue(lint_stamp_gate.exempt_path("claims/..x/a.md"), "only a whole `..` component is traversal")


class NoteTests(unittest.TestCase):
    def test_known_red_note_names_what_was_tolerated_and_retried(self):
        self.assertIsNone(lint_stamp_gate.known_red_note("changed steps=3 names=a"))
        self.assertEqual(
            lint_stamp_gate.known_red_note("changed steps=3 known_red=a,b baseline=abc"),
            "NOTE: this pass tolerated failures that are known red on main (not caused by the branch): a,b",
        )
        self.assertEqual(
            lint_stamp_gate.known_red_note("changed steps=3 timeout-retried=x,y"),
            "NOTE: this pass rests on a serial rerun of steps that timed out while the machine was saturated: x,y",
        )
        both = lint_stamp_gate.known_red_note("changed known_red=a timeout-retried=x").split("\n")
        self.assertEqual(len(both), 2)
        self.assertTrue(both[0].endswith(": a") and both[1].endswith(": x"), both)

    def test_inherited_note_is_exact_and_absent_for_a_plain_stamp(self):
        self.assertIsNone(lint_stamp_gate.inherited_note("changed steps=3"))
        sha = "0123456789abcdef0123456789abcdef01234567"
        self.assertEqual(
            lint_stamp_gate.inherited_note("changed steps=3 inherited-from=" + sha),
            "NOTE: this pass is inherited from 0123456789ab: only night-shift records (history, to-do, claim files) changed since that run",
        )


class InheritExactnessTests(InheritFixture):
    def test_the_inherited_stamp_text_is_the_old_line_plus_the_source(self):
        code = self.commit("daemons/x/src/a.rs", "1\n", "code")
        self.put_stamp(code, "changed 2026-10-30T10:00:00 steps=3 names=check_cargo_clippy  \n\n")
        tip = self.commit("claims/a.md", "# a\n", "claim")
        self.assertEqual(self.inherit(tip), "changed 2026-10-30T10:00:00 steps=3 names=check_cargo_clippy inherited-from=%s\n" % code)
        self.assertEqual(self.messages, [])

    def test_an_unlistable_range_is_logged_with_the_range_and_the_cause(self):
        tip = self.commit("claims/a.md", "# a\n", "claim")
        self.assertIsNone(lint_stamp_gate.inherit_stamp(self.repo, tip, self.passed, base="no_such_ref", log=self.messages.append))
        self.assertEqual(len(self.messages), 1)
        self.assertTrue(self.messages[0].startswith("stamp inheritance: cannot list no_such_ref..%s: " % tip), self.messages)
        self.assertGreater(len(self.messages[0]), len("stamp inheritance: cannot list no_such_ref..%s: " % tip))

    def test_an_existing_stamp_of_the_tip_is_returned_as_it_is(self):
        tip = self.commit("claims/a.md", "# a\n", "claim")
        self.put_stamp(tip, "full steps=9\n")
        self.assertEqual(self.inherit(tip), "full steps=9\n")

    def test_the_refusal_log_counts_the_other_files_and_shows_at_most_five(self):
        code = self.commit("daemons/x/src/a.rs", "1\n", "code")
        self.put_stamp(code)
        for name in "abcdef":
            self.commit("src/%s.py" % name, "1\n", name)
        tip = self.git("rev-parse", "HEAD")
        self.assertIsNone(self.inherit(tip))
        self.assertEqual(
            self.messages,
            ["stamp inheritance: 6 changed since the stamped commit %s, not only records: src/a.py, src/b.py, src/c.py, src/d.py, src/e.py" % code[:9]],
        )

    def test_an_invalid_history_logs_the_tip_and_at_most_three_problems(self):
        code = self.commit("daemons/x/src/a.rs", "1\n", "code")
        self.put_stamp(code)
        tip = self.commit("claims/a.md", "# a\n", "claim")
        original = lint_stamp_gate._history_problems
        lint_stamp_gate._history_problems = lambda repo, t: ["p1", "p2", "p3", "p4"]
        self.addCleanup(setattr, lint_stamp_gate, "_history_problems", original)
        self.assertIsNone(self.inherit(tip))
        self.assertEqual(self.messages, ["stamp inheritance: the night-shift history of %s is not valid: p1; p2; p3" % tip[:9]])

    def test_no_stamped_commit_in_range_is_logged_with_base_and_tip(self):
        self.commit("claims/a.md", "# a\n", "claim")
        tip = self.git("rev-parse", "HEAD")
        self.assertIsNone(self.inherit(tip))
        self.assertEqual(self.messages, ["stamp inheritance: no stamped commit between origin_main and %s" % tip[:9]])

    def _chain(self, records, salt=""):
        # `salt` keeps two chains built within the same second from being identical commits (same tree, message, author and
        # date give the same hash): the second chain would then share the first one's stamped commits and stamp files.
        code = self.commit("daemons/x/src/a.rs", "1%s\n" % salt, "code" + salt)
        self.put_stamp(code)
        for index in range(records):
            tip = self.commit("claims/r%d.md" % index, "# r%s\n" % salt, "r%d%s" % (index, salt))
        return code, tip

    def test_the_stamped_commit_may_be_the_oldest_of_the_sixty_one_listed_and_no_older(self):
        limit = lint_stamp_gate.INHERIT_MAX_COMMITS
        self.assertEqual(limit, 60)
        code, tip = self._chain(limit)
        self.assertIn("inherited-from=" + code, self.inherit(tip))
        self.git("checkout", "-q", "-B", "work2", "origin_main")
        code, tip = self._chain(limit + 1, salt="-second")
        self.assertIsNone(self.inherit(tip), "sixty-two commits listed is one more than the walk reads")


class MainExactnessTests(InheritFixture):
    GATE = os.path.join(HERE, "lint_stamp_gate.py")

    def run_gate(self, *args, stdin=None):
        return subprocess.run([sys.executable, self.GATE, *args], cwd=self.repo, input=stdin, capture_output=True, text=True)

    def test_the_inherit_option_checks_its_argument_count(self):
        for args in (("--inherit",), ("--inherit", "tip"), ("--inherit", "tip", "dir", "base", "extra")):
            done = self.run_gate(*args)
            self.assertEqual(done.returncode, 2, args)
            self.assertEqual(done.stderr, "usage: lint_stamp_gate.py --inherit TIP PASSED_DIR [BASE]\n", args)

    def test_the_inherit_option_defaults_the_base_to_origin_main_and_fails_with_one(self):
        tip = self.commit("claims/a.md", "# a\n", "claim")
        done = self.run_gate("--inherit", tip, self.passed)
        self.assertEqual(done.returncode, 1)
        self.assertIn("cannot list origin/main..%s" % tip, done.stderr)
        self.assertEqual(done.stdout, "")

    def test_the_inherit_option_prints_the_stamp_line_without_the_newline_doubling(self):
        code = self.commit("daemons/x/src/a.rs", "1\n", "code")
        self.put_stamp(code)
        tip = self.commit("claims/a.md", "# a\n", "claim")
        done = self.run_gate("--inherit", tip, self.passed, "origin_main")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.count("\n"), 1)
        self.assertTrue(done.stdout.endswith("inherited-from=%s\n" % code))

    def test_the_stamp_form_needs_exactly_one_argument(self):
        for args in ((), ("a", "b")):
            done = self.run_gate(*args, stdin="")
            self.assertEqual(done.returncode, 2, args)
            self.assertEqual(done.stderr, "usage: lint_stamp_gate.py STAMP_FILE < changed_paths\n", args)

    def test_the_stamp_form_prints_the_stamp_then_the_known_red_note_then_the_inherited_note(self):
        path = os.path.join(self.repo, "stamp")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("changed 2026-10-30T10:00:00 steps=3 names=flake8 known_red=k1 inherited-from=" + "a" * 40 + "\n")
        done = self.run_gate(path, stdin="tools/x.py\n")
        self.assertEqual(done.returncode, 0, done.stdout)
        lines = done.stdout.splitlines()
        self.assertEqual(len(lines), 3, lines)
        self.assertTrue(lines[0].startswith("changed ") and lines[0].endswith("a" * 40))
        self.assertIn("known red on main", lines[1])
        self.assertIn("inherited from " + "a" * 12, lines[2])

    def test_a_refusal_prints_the_reason_with_a_full_stop_and_exits_one(self):
        path = os.path.join(self.repo, "stamp")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("changed steps=0\n")
        done = self.run_gate(path, stdin="tools/x.py\n")
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout, "REFUSING MERGE: the stamp selected zero steps for a branch that changed code.\n")


if __name__ == "__main__":
    unittest.main()


class HistoryProblemsTests(InheritFixture):
    """`_history_problems` must say "no problems" only when it looked: a failed git archive is a problem naming the cause."""

    def _failing_archive(self):
        real = subprocess.run

        def run(cmd, *args, **kwargs):
            if "archive" in cmd:
                return subprocess.CompletedProcess(cmd, 128, b"", b"fatal: simulated archive failure\n")
            return real(cmd, *args, **kwargs)

        return mock.patch.object(lint_stamp_gate.subprocess, "run", run)

    def test_a_repository_without_a_history_directory_has_no_problems(self):
        tip = self.git("rev-parse", "HEAD")
        self.assertEqual(lint_stamp_gate._history_problems(self.repo, tip), [])

    def test_a_failed_git_archive_is_a_problem_naming_the_cause_not_no_problems(self):
        tip = self.commit("night_shift_history.md", "# h\n", "history")
        with self._failing_archive():
            problems = lint_stamp_gate._history_problems(self.repo, tip)
        self.assertEqual(len(problems), 1)
        self.assertIn("git archive", problems[0])
        self.assertIn("simulated archive failure", problems[0])
        self.assertIn(tip[:9], problems[0])

    def test_a_tip_git_cannot_list_is_a_problem_not_no_problems(self):
        problems = lint_stamp_gate._history_problems(self.repo, "0" * 40)
        self.assertEqual(len(problems), 1)
        self.assertIn("git ls-tree", problems[0])

    def test_a_repository_that_is_not_a_repository_is_a_problem(self):
        with tempfile.TemporaryDirectory() as empty:
            problems = lint_stamp_gate._history_problems(empty, "HEAD")
        self.assertEqual(len(problems), 1)

    def test_inheriting_a_stamp_is_refused_when_the_history_cannot_be_archived(self):
        self.commit("night_shift_history.md", "# h\n", "history")
        code = self.commit("daemons/x/src/a.rs", "fn main() {}\n", "code")
        self.put_stamp(code)
        tip = self.commit("claims/b.md", "# b\n", "claim")
        with self._failing_archive():
            self.assertIsNone(self.inherit(tip))
        self.assertFalse(os.path.exists(os.path.join(self.passed, tip)))
        self.assertTrue(any("not valid" in m and "git archive" in m for m in self.messages), self.messages)
