"""pre_push_check.py: which direct pushes to main are allowed and the time budget of the branch check."""
import os
import subprocess
import tempfile
import unittest

import pre_push_check as ppc


class PrePushCheckTests(unittest.TestCase):
    # [@ANCHOR: test_a_main_push_is_allowed_only_for_notes_or_with_the_override]
    def test_a_main_push_is_allowed_only_for_notes_or_with_the_override(self):
        notes = ["night_shift_todo/high/a.md", "night_shift_history/b.md", "night_shift_questions/c.md"]
        self.assertIsNone(ppc.main_push_problem(notes, {}))
        self.assertIn("merge_pr.py", ppc.main_push_problem(notes + ["ham_base/models/x.py"], {}))
        self.assertIsNone(ppc.main_push_problem(["x.py"], {"HAMS_ALLOW_MAIN_PUSH": "1"}))
        self.assertIsNotNone(ppc.main_push_problem(["night_shift_todo_evil/x.py"], {}))

    # [@ANCHOR: test_the_runner_gives_up_when_the_time_is_spent]
    def test_the_runner_gives_up_when_the_time_is_spent(self):
        calls = []

        def run(argv, cwd=None, timeout=0):
            calls.append(timeout)
            return subprocess.CompletedProcess(argv, 124 if argv[0] == "slow" else 0, "", "")

        now = [100.0]
        runner = ppc.deadline_runner(run, 160.0, clock=lambda: now[0])
        self.assertEqual(runner(["fast"]).returncode, 0)
        self.assertEqual(calls, [60.0])
        # Tests [@ANCHOR: pre_push_check_out_of_time]
        with self.assertRaises(ppc.OutOfTime):
            runner(["slow"])
        now[0] = 161.0
        with self.assertRaises(ppc.OutOfTime):
            runner(["fast"])
        self.assertEqual(len(calls), 2)


class PrePushHookEnvironmentTests(unittest.TestCase):
    def test_the_pre_push_hook_runs_its_checks_without_git_dir(self):
        hook = os.path.join(os.path.dirname(os.path.abspath(__file__)), "git-hooks", "pre-push")
        with tempfile.TemporaryDirectory() as tmp:
            def git(cwd, *args):
                return subprocess.run(["git", "-C", cwd, "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True, capture_output=True, text=True).stdout
            main = os.path.join(tmp, "main")
            os.makedirs(os.path.join(main, "tools"))
            git(main, "init", "-q", "-b", "main")
            with open(os.path.join(main, "tools", "pre_push_check.py"), "w", encoding="utf-8") as out:
                out.write("import os, sys\nopen(os.environ['OUT'], 'w').write(repr([k for k in os.environ if k.startswith('GIT_DIR')]))\n")
            git(main, "add", ".")
            git(main, "commit", "-q", "-m", "x")
            work = os.path.join(tmp, "work")
            git(main, "worktree", "add", "-q", "-b", "w", work)
            out_path = os.path.join(tmp, "seen")
            # What git gives a hook run from a linked worktree.
            env = dict(os.environ, OUT=out_path, GIT_DIR=os.path.join(main, ".git", "worktrees", "work"))
            sha = git(work, "rev-parse", "HEAD").strip()
            subprocess.run(["bash", hook, "origin"], cwd=work, env=env, input=f"refs/heads/w {sha} refs/heads/w {'0' * 40}\n", check=True, capture_output=True, text=True)
            with open(out_path, encoding="utf-8") as seen:
                self.assertEqual(seen.read(), "[]")


if __name__ == "__main__":
    unittest.main()
