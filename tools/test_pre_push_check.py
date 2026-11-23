"""pre_push_check.py: which direct pushes to main are allowed and the time budget of the branch check."""
import os
import shutil
import subprocess
import tempfile
import unittest
import unittest.mock

import pre_push_check as ppc


class PrePushCheckTests(unittest.TestCase):
    # [@ANCHOR: test_a_main_push_is_refused_unless_the_override_is_set]
    def test_a_main_push_is_refused_unless_the_override_is_set(self):
        ok = ["night_shift_todo/high/a.md", "docs/x/y.txt", "ham_x/claims/c.md", "claims/c.md", "README.md", "notes/n.md"]
        self.assertIsNone(ppc.main_push_problem(ok, {}))
        for bad in ("ham_base/models/x.py", "ham_base/__manifest__.py", "ham_base/tests/t.md", "ham_base/data/d.xml", "night_shift_todo_evil/x.py",
                    "tools/README.md", "x.txt"):
            self.assertIn("NO DIRECT PUSHES TO MAIN", ppc.main_push_problem(ok + [bad], {}), bad)
        self.assertIn("merge_pr.py", ppc.main_push_problem([], {}))
        self.assertIn("NO DIRECT", ppc.main_push_problem(["README.md"], {}, has_anchor=lambda path: True))  # anchor docs need the PR lint
        self.assertIsNone(ppc.main_push_problem(["x.py"], {"HAMS_ALLOW_MAIN_PUSH": "1"}))

    def test_a_failing_cheap_check_blocks_an_allowed_main_push(self):
        calls = []
        done = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with unittest.mock.patch.object(ppc, "git", return_value=done) as fake_git, \
                unittest.mock.patch.object(ppc, "find_merge_pr", return_value="/x"), \
                unittest.mock.patch.object(ppc, "check_branch", side_effect=lambda *a, **k: calls.append(a) or False):
            fake_git.side_effect = lambda args, cwd=None: subprocess.CompletedProcess(
                [], 0, stdout="docs/a.md\n" if args[0] == "diff" else "https://github.com/BrucePerens/hams_com.git\n", stderr="")
            self.assertEqual(ppc.main(["p", "refs/heads/main", "a" * 40, "refs/heads/main", "b" * 40]), 1)
            self.assertEqual(len(calls), 1)
            with unittest.mock.patch.object(ppc, "check_branch", return_value=True):
                self.assertEqual(ppc.main(["p", "refs/heads/main", "a" * 40, "refs/heads/main", "b" * 40]), 0)
            fake_git.side_effect = lambda args, cwd=None: subprocess.CompletedProcess(
                [], 0, stdout="ham_x/models/a.py\n" if args[0] == "diff" else "https://github.com/BrucePerens/hams_com.git\n", stderr="")
            self.assertEqual(ppc.main(["p", "refs/heads/main", "a" * 40, "refs/heads/main", "b" * 40]), 1)

    def test_the_shell_hook_refuses_main_first_and_lets_branches_reach_the_checks(self):
        hook = os.path.join(os.path.dirname(os.path.abspath(__file__)), "git-hooks", "pre-push")
        line = "refs/heads/x abc refs/heads/%s 000\n"
        env = {k: v for k, v in os.environ.items() if k != "HAMS_ALLOW_MAIN_PUSH"}
        repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, repo, True)
        subprocess.run(["git", "init", "-q", repo], check=True)
        subprocess.run(["git", "-C", repo, "remote", "add", "origin", "https://github.com/BrucePerens/hams_shared.git"], check=True)
        refused = subprocess.run(["bash", hook, "origin"], input=line % "main", capture_output=True, text=True, env=env, cwd=repo)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("NO DIRECT PUSHES TO MAIN: open a PR and merge with devbox_tools/merge_pr.py", refused.stderr)
        # the guard is the first thing that runs: no fetch, so no network is needed to be refused
        self.assertNotIn("fetch", refused.stderr)
        # a delete of main is refused too, and a non-main ref is not stopped by this guard
        deleted = subprocess.run(["bash", hook, "origin"], input="(delete) 000 refs/heads/main abc\n", capture_output=True, text=True, env=env, cwd=repo)
        self.assertEqual(deleted.returncode, 1)
        self.assertIn("NO DIRECT PUSHES TO MAIN", deleted.stderr)
        other = subprocess.run(["bash", hook, "origin"], input="", capture_output=True, text=True, env=env, cwd=repo)
        self.assertNotIn("NO DIRECT PUSHES", other.stderr)

    # [@ANCHOR: test_the_runner_gives_up_when_the_time_is_spent]
    def test_the_runner_gives_up_when_the_time_is_spent(self):
        calls = []

        def run(argv, cwd=None, timeout=0, wall=None):
            self.assertEqual(wall, timeout)  # the budget is a wall-clock limit as well as an idle one
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


class CheckBranchSubmoduleTests(unittest.TestCase):
    def test_a_failed_submodule_checkout_allows_the_push_and_a_good_one_runs_the_checks_in_a_populated_tree(self):
        import sys
        import types
        job = tempfile.mkdtemp()
        self.addCleanup(subprocess.run, ["rm", "-rf", job])
        tree = os.path.join(job, "hams_open")
        subprocess.run(["git", "init", "-q", tree], check=True)
        calls = []
        problem = ["boom"]
        fake_wt = types.ModuleType("new_agent_worktree")
        fake_wt.create = lambda *a, **k: {"main_checkout": tree, "worktree": tree, "branch": "b", "job": "j", "job_dir": job}
        fake_wt.state_dir = lambda: job
        fake_wt.WorktreeError = RuntimeError
        fake_wt.default_root = lambda: job
        fake_wt.check_space = lambda root: None
        fake_mp = types.ModuleType("merge_pr")
        fake_mp.ensure_shared_submodule = lambda repo, t, m: calls.append(repo) or problem[0]
        fake_mp.run = lambda *a, **k: None
        fake_mp.check_plan = lambda *a: []
        fake_mp.first_failure = lambda *a, **k: None
        fake_mp.scope_needles = lambda *a: frozenset()
        saved = {name: sys.modules.get(name) for name in ("merge_pr", "new_agent_worktree")}
        sys.modules.update(merge_pr=fake_mp, new_agent_worktree=fake_wt)
        self.addCleanup(lambda: [sys.modules.__setitem__(n, m) if m else sys.modules.pop(n, None) for n, m in saved.items()])

        def make_repo():  # check_branch removes the job directory when it is done
            subprocess.run(["git", "init", "-q", tree], check=True)
            subprocess.run(["git", "-C", tree, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x"], check=True)
            return subprocess.run(["git", "-C", tree, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()

        said = []
        sha = make_repo()
        self.assertTrue(ppc.check_branch("hams_open", sha, job, say=said.append))
        self.assertEqual(calls, ["hams_open"])
        self.assertIn("submodule", said[0])
        problem[0] = ""
        said.clear()
        sha = make_repo()
        self.assertTrue(ppc.check_branch("hams_open", sha, job, say=said.append))
        self.assertEqual(said, [])


class CheckBranchWorktreeErrorTests(unittest.TestCase):
    def test_a_worktree_error_allows_the_push_with_a_warning_and_the_job_name_is_unique_per_run(self):
        import sys
        import types
        names = []

        class WorktreeError(RuntimeError):
            pass

        def create(repo, branch, **kwargs):
            names.append(branch)
            raise WorktreeError("only 3 GB free")

        fake_wt = types.ModuleType("new_agent_worktree")
        fake_wt.WorktreeError = WorktreeError
        fake_wt.create = create
        fake_wt.default_root = lambda: "/x"
        space = [0]

        def check_space(root):
            if space[0]:
                raise WorktreeError("only %d GB free on the filesystem of %s" % (space[0], root))

        fake_wt.check_space = check_space
        fake_mp = types.ModuleType("merge_pr")
        saved = {name: sys.modules.get(name) for name in ("merge_pr", "new_agent_worktree")}
        sys.modules.update(merge_pr=fake_mp, new_agent_worktree=fake_wt)
        self.addCleanup(lambda: [sys.modules.__setitem__(n, m) if m else sys.modules.pop(n, None) for n, m in saved.items()])
        said = []
        self.assertTrue(ppc.check_branch("hams_com", "abc", tempfile.gettempdir(), say=said.append))
        self.assertEqual(len(said), 1)
        self.assertIn("only 3 GB free", said[0])
        self.assertIn("push allowed", said[0])
        self.assertTrue(names[0].startswith("prepush-%d-" % os.getpid()))  # not just the pid: a stale prepush-<pid> directory cannot collide
        space[0], names[:], said[:] = 7, [], []
        self.assertTrue(ppc.check_branch("hams_com", "abc", tempfile.gettempdir(), say=said.append))  # a full disk: warned, allowed, no worktree made
        self.assertIn("only 7 GB free", said[0])
        self.assertEqual(names, [])


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
