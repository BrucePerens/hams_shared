#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Tests for check_claude_hook_registration.py: every repository checkout (hams_com, hams_open, hams_shared) registers the
signal guard PreToolUse hook in its tracked .claude/settings.json (incident of 2026-11-09: an agent worked in a hams_shared
worktree that had no such file and killed its own parent).

Tests [@ANCHOR: check_claude_hook_registration:command_is_guard]
Tests [@ANCHOR: check_claude_hook_registration:matcher_covers_bash]
Tests [@ANCHOR: check_claude_hook_registration:is_tracked]
Tests [@ANCHOR: check_claude_hook_registration:check_repo]
Tests [@ANCHOR: check_claude_hook_registration:main]
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import check_claude_hook_registration as chk  # noqa: E402

THIS_REPO = os.path.dirname(HERE)
GOOD_COMMAND = ('f="$HOME/workspace/hams_open/hams_shared/tools/claude_hooks/guard_signals.py"; [ -f "$f" ] || exit 0; '
                'exec python3 "$f"')


def settings(command=GOOD_COMMAND, matcher="Bash", event="PreToolUse", kind="command"):
    return {"hooks": {event: [{"matcher": matcher, "hooks": [{"type": kind, "command": command, "timeout": 10}]}]}}


def git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True)


class CheckRepoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hook-reg-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "hams_shared")
        os.makedirs(os.path.join(self.repo, ".claude"))

    def write(self, data, raw=None):
        with open(os.path.join(self.repo, ".claude", "settings.json"), "w") as handle:
            handle.write(raw if raw is not None else json.dumps(data))

    def test_command_is_guard_needs_an_anchored_path(self):
        self.assertTrue(chk.command_is_guard(GOOD_COMMAND))
        self.assertTrue(chk.command_is_guard("python3 /home/bruce/workspace/hams_open/hams_shared/tools/claude_hooks/guard_signals.py"))
        self.assertTrue(chk.command_is_guard('python3 "$CLAUDE_PROJECT_DIR/hams_shared/tools/claude_hooks/guard_signals.py"'))
        self.assertTrue(chk.command_is_guard("python3 ~/workspace/hams_open/hams_shared/tools/claude_hooks/guard_signals.py"))
        self.assertFalse(chk.command_is_guard("python3 hams_shared/tools/claude_hooks/guard_signals.py"), "relative: breaks in a worktree")
        self.assertFalse(chk.command_is_guard("python3 tools/claude_hooks/guard_signals.py"))
        self.assertFalse(chk.command_is_guard("echo hi"))
        self.assertFalse(chk.command_is_guard(None))

    def test_matcher_covers_bash(self):
        for matcher in ("Bash", "", "*", None, "Bash|Edit", "Edit|Bash"):
            self.assertTrue(chk.matcher_covers_bash(matcher), matcher)
        for matcher in ("Edit", "Write|Read", "Bashful", "(unclosed"):
            self.assertFalse(chk.matcher_covers_bash(matcher), matcher)

    def test_a_registered_hook_passes(self):
        self.write(settings())
        self.assertEqual(chk.check_repo(self.repo), [])

    def test_missing_file_bad_json_wrong_event_wrong_tool_wrong_command_all_fail(self):
        self.assertIn("does not exist", chk.check_repo(self.repo)[0])
        self.write(None, raw="{not json")
        self.assertIn("JSON", chk.check_repo(self.repo)[0])
        for bad in (settings(event="PostToolUse"), settings(matcher="Edit"), settings(command="echo hi"),
                    settings(kind="prompt"), {}, {"hooks": {}}, {"hooks": {"PreToolUse": "x"}}, [], {"hooks": {"PreToolUse": [5]}}):
            self.write(bad)
            problems = chk.check_repo(self.repo)
            self.assertTrue(problems and "has no PreToolUse hook" in problems[0], bad)

    def test_the_hook_beside_other_hooks_and_permissions_passes(self):
        data = settings()
        data["hooks"]["SessionStart"] = [{"hooks": [{"type": "command", "command": "true"}]}]
        data["hooks"]["PreToolUse"].insert(0, {"matcher": "Edit", "hooks": [{"type": "command", "command": "true"}]})
        data["permissions"] = {"allow": ["Bash(cargo test *)"]}
        self.write(data)
        self.assertEqual(chk.check_repo(self.repo), [])

    def test_a_file_that_git_does_not_track_fails_and_a_tracked_one_passes(self):
        git(self.repo, "init", "-q")
        self.write(settings())
        self.assertTrue(any("not tracked" in problem for problem in chk.check_repo(self.repo)))
        self.assertFalse(chk.is_tracked(self.repo))
        git(self.repo, "add", ".claude/settings.json")
        self.assertTrue(chk.is_tracked(self.repo))
        self.assertEqual(chk.check_repo(self.repo), [])

    def test_outside_git_the_tracking_rule_does_not_apply(self):
        self.assertTrue(chk.is_tracked(self.repo))

    def test_main_prints_the_problem_and_returns_one_or_zero(self):
        out = io.StringIO()
        self.assertEqual(chk.main([self.repo], out), 1)
        self.assertIn("hams_shared", out.getvalue())
        self.write(settings())
        out = io.StringIO()
        self.assertEqual(chk.main([self.repo], out), 0)
        self.assertEqual(out.getvalue(), "")
        other = os.path.join(self.tmp, "hams_com")
        os.makedirs(other)
        self.assertEqual(chk.main([self.repo, other], io.StringIO()), 1, "every named repository must register the hook")

    def test_the_script_runs_from_the_command_line(self):
        self.write(settings())
        done = subprocess.run([sys.executable, os.path.join(HERE, "check_claude_hook_registration.py"), self.repo], capture_output=True, text=True)
        self.assertEqual((done.returncode, done.stdout), (0, ""))
        os.remove(os.path.join(self.repo, ".claude", "settings.json"))
        done = subprocess.run([sys.executable, os.path.join(HERE, "check_claude_hook_registration.py"), self.repo], capture_output=True, text=True)
        self.assertEqual(done.returncode, 1)


def checkouts():
    """The checkouts that exist next to this one: this repository, its superproject (hams_open) and the hams_com beside it."""
    found = {"hams_shared": THIS_REPO}
    done = subprocess.run(["git", "-C", THIS_REPO, "rev-parse", "--show-superproject-working-tree"], capture_output=True, text=True)
    if done.returncode == 0 and done.stdout.strip():
        found["hams_open"] = done.stdout.strip()
    for name in ("hams_open", "hams_com"):
        candidate = os.path.join(os.path.dirname(os.path.dirname(THIS_REPO)), name)
        if name not in found and os.path.isfile(os.path.join(candidate, "CLAUDE.md" if name == "hams_com" else "README.md")):
            found[name] = candidate
    return found


class RealCheckoutTests(unittest.TestCase):
    def test_this_checkout_registers_the_guard_and_the_registered_command_denies_the_incident(self):
        self.assertEqual(chk.check_repo(THIS_REPO), [])
        with open(os.path.join(THIS_REPO, ".claude", "settings.json"), encoding="utf-8") as handle:
            command = json.load(handle)["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        home = tempfile.mkdtemp(prefix="hook-home-")
        self.addCleanup(shutil.rmtree, home, True)
        os.makedirs(os.path.join(home, "workspace", "hams_open"))
        os.symlink(THIS_REPO, os.path.join(home, "workspace", "hams_open", "hams_shared"))
        payload = json.dumps({"session_id": "s", "tool_name": "Bash", "tool_input": {
            "command": 'pp=$(ps -o ppid= -p 2268409 | tr -d " "); echo parent $pp; kill $pp 2>/dev/null; kill 2268409 2268411 2>/dev/null'}})
        env = dict(os.environ, HOME=home, HAMS_JOBS_DIR=os.path.join(home, "jobs"))
        done = subprocess.run(["sh", "-c", command], input=payload, capture_output=True, text=True, env=env, cwd=home)
        self.assertEqual(done.returncode, 0)
        self.assertEqual(json.loads(done.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_every_checkout_found_beside_this_one_registers_the_guard(self):
        found = checkouts()
        self.assertIn("hams_shared", found)
        for name, path in found.items():
            self.assertEqual(chk.check_repo(path), [], name)
        if len(found) < 3:
            self.skipTest("only %s found here; the lint run of each repository checks that repository itself" % sorted(found))


if __name__ == "__main__":
    unittest.main()
