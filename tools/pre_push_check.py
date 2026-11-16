#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Called by git-hooks/pre-push once per pushed ref: `pre_push_check.py <local_ref> <local_sha> <remote_ref> <remote_sha>` (cwd: the repo).

  - A push to main must come from merge_pr.py, which merges through GitHub, never from a git push: it is refused unless every
    changed file is a to-do note (night_shift_todo/, night_shift_history/, night_shift_questions/, which Bruce allows direct) or
    HAMS_ALLOW_MAIN_PUSH=1 is set (the coordinator, deliberately).
  - A push of any other branch runs merge_pr's cheap check_plan (imported, not copied) on the branch merged into origin/main, within
    PUSH_CHECK_SECONDS. A failing check refuses the push (fix it, run lint_autofix.py, or `git push --no-verify`); a check that does
    not finish in time only warns and the push goes ahead. HAMS_SKIP_PREPUSH_CHECK=1 skips the check.
Exit 0 allow, 1 refuse.
"""
import os
import subprocess
import sys
import time

PUSH_CHECK_SECONDS = 180
NOTE_DIRS = ("night_shift_todo/", "night_shift_history/", "night_shift_questions/")
ZERO = "0" * 40


# [@ANCHOR: pre_push_check_out_of_time]
class OutOfTime(Exception):
    """Raised by the deadline runner when the push-check budget is used up or one command times out."""


def git(args, cwd=None):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)


# [@ANCHOR: pre_push_check_main_push_problem]
# Verified by [@ANCHOR: test_a_main_push_is_allowed_only_for_notes_or_with_the_override]
def main_push_problem(files, environ=os.environ):
    """None when a direct push of `files` to main is allowed, else the reason."""
    if environ.get("HAMS_ALLOW_MAIN_PUSH") == "1":
        return None
    other = [f for f in files if not f.startswith(NOTE_DIRS)]
    if not other:
        return None
    return ("a direct push to main changes files other than to-do notes (%s ...). Open a PR and land it with "
            "devbox_tools/merge_pr.py <repo> <PR>; never push code to main." % ", ".join(other[:3]))


def find_merge_pr():
    """The directory holding merge_pr.py (the hams_com checkout's devbox_tools), or None."""
    candidates = [os.environ.get("HAMS_DEVBOX_TOOLS", ""), os.path.expanduser("~/workspace/hams_com/devbox_tools")]
    return next((c for c in candidates if c and os.path.exists(os.path.join(c, "merge_pr.py"))), None)


# [@ANCHOR: pre_push_check_deadline_runner]
# Verified by [@ANCHOR: test_the_runner_gives_up_when_the_time_is_spent]
def deadline_runner(run, deadline, clock=time.monotonic):
    """A runner for merge_pr.first_failure that gives each command what is left of the budget and raises OutOfTime when none is left or a command times out."""
    def runner(argv, cwd=None):
        left = deadline - clock()
        if left <= 0:
            raise OutOfTime()
        result = run(argv, cwd=cwd, timeout=left)
        if result.returncode == 124:
            raise OutOfTime()
        return result
    return runner


def check_branch(repo, sha, tools_dir, seconds=PUSH_CHECK_SECONDS, say=print):
    """True to allow the push: the branch at `sha` merged into origin/main passes merge_pr's cheap checks, or they could not finish in time."""
    sys.path.insert(0, tools_dir)
    import merge_pr
    import new_agent_worktree

    deadline = time.monotonic() + seconds
    record = new_agent_worktree.create(repo, "prepush-%d" % os.getpid(), root=None, run_fixer=True)
    main, tree = record["main_checkout"], record["worktree"]
    try:
        merged = git(["-c", "user.name=pre-push", "-c", "user.email=pre-push@localhost", "merge", "--no-edit", sha], cwd=tree)
        if merged.returncode != 0:
            say("pre-push: merging %s into origin/main conflicts; merge origin/main into your branch before the PR. Push allowed." % sha[:10])
            return True
        files = git(["diff", "--name-only", "origin/main...HEAD"], cwd=tree).stdout.split()
        try:
            failure = merge_pr.first_failure(merge_pr.check_plan(repo, files, tree), deadline_runner(merge_pr.run, deadline), merge_pr.scope_needles(files, tree))
        except OutOfTime:
            say("pre-push: the cheap checks did not finish in %d s; push allowed. merge_pr.py will run them." % seconds)
            return True
        if failure:
            say("pre-push: CHECK FAILED: %s\n%s\nFix it (python3 hams_shared/tools/lint_autofix.py fixes the mechanical ones), or push anyway with --no-verify." % failure)
            return False
        return True
    finally:
        git(["worktree", "remove", "--force", tree], cwd=main)
        git(["branch", "-D", record["branch"]], cwd=main)
        subprocess.run(["rm", "-rf", record["job_dir"]])
        state = os.path.join(new_agent_worktree.state_dir(), record["job"] + ".json")
        if os.path.exists(state):
            os.remove(state)


def main(argv):
    local_ref, local_sha, remote_ref, remote_sha = argv[1:5]
    if local_sha == ZERO or not remote_ref.startswith("refs/heads/"):
        return 0  # a deletion or a tag; the shell hook handles main deletion
    if remote_ref == "refs/heads/main":
        problem = main_push_problem(git(["diff", "--name-only", "origin/main", local_sha]).stdout.split())
        if problem:
            print("pre-push: refusing: " + problem, file=sys.stderr)
            return 1
        return 0
    if os.environ.get("HAMS_SKIP_PREPUSH_CHECK") == "1":
        return 0
    tools_dir = find_merge_pr()
    url = git(["remote", "get-url", "origin"]).stdout.strip()
    repo = os.path.basename(url).removesuffix(".git")
    if tools_dir is None or repo not in ("hams_com", "hams_open", "hams_shared"):
        print("pre-push: merge_pr.py or the repo name not found; check skipped.", file=sys.stderr)
        return 0
    return 0 if check_branch(repo, local_sha, tools_dir, say=lambda m: print(m, file=sys.stderr)) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
