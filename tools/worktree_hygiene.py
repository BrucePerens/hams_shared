#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Worktree Hygiene
----------------
Half-finished worktrees and branches pile up (about 300 local branches were found in hams_open and hams_com on 2026-10-26, most
already squash-merged). This tool finds them and removes the ones that are provably finished.

  worktree_hygiene.py [--apply] [--stale-minutes N] [--no-fetch] REPO...

For every REPO (the main working tree of a repository):
  * A linked worktree whose branch is merged (no commits beyond origin/main, or its tip is the head of a merged pull request),
    whose tree is clean and that has seen no git activity for N minutes is MERGED: with --apply it is removed, then its branch
    is deleted. A worktree made a minute ago has no commits either, so only its idleness says it is not about to be used.
  * A local branch with no worktree that is merged and was last updated more than N minutes ago is deleted with --apply.
  * A worktree that has uncommitted changes, or commits not in origin/main, and has not been touched for N minutes (default 30)
    is STALE: it is only reported, never removed. The fix is to commit the named paths, open a pull request and merge it, even
    if the work is unfinished (say so in the pull request), then run this tool again.
Exit status 1 when anything is STALE, so a timer or a session start can surface it.

Nothing unmerged or dirty is ever deleted. Merged pull request heads come from `gh pr list`; without gh only the plain
"no commits beyond origin/main" test is used.
"""

import argparse
import json
import os
import subprocess
import sys
import time


def git(repo, *args, check=True):
    result = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} in {repo}: {result.stderr.strip()}")
    return result.stdout.strip()


def merged_pull_request_heads(repo):
    """{branch name: set of tip shas} of merged pull requests of the repository the working tree belongs to."""
    result = subprocess.run(
        ["gh", "pr", "list", "--state", "merged", "--limit", "1000", "--json", "headRefName,headRefOid"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {}
    heads = {}
    for entry in json.loads(result.stdout or "[]"):
        heads.setdefault(entry["headRefName"], set()).add(entry["headRefOid"])
    return heads


def worktrees(repo):
    """[(path, branch or None)] of the linked worktrees (the main working tree is first and is skipped by the caller)."""
    entries, current = [], {}
    for line in git(repo, "worktree", "list", "--porcelain").splitlines() + [""]:
        if not line:
            if current:
                entries.append((current["worktree"], current.get("branch")))
            current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value.removeprefix("refs/heads/") if key == "branch" else value
    return entries[1:]


def is_merged(repo, branch, merged_heads, base="origin/main"):
    tip = git(repo, "rev-parse", branch)
    if int(git(repo, "rev-list", "--count", f"{base}..{branch}")) == 0:
        return True
    return tip in merged_heads.get(branch, set())


def last_touched(path):
    """Seconds since the epoch of the newest of: the last commit, and any changed file in the tree."""
    newest = int(git(path, "log", "-1", "--format=%ct", check=False) or 0)
    for line in git(path, "status", "--porcelain", "-z", check=False).split("\0"):
        if len(line) > 3:
            try:
                newest = max(newest, int(os.stat(path + "/" + line[3:]).st_mtime))
            except OSError:
                pass
    return newest


def worktree_activity(path):
    """Seconds since the epoch of the newest git activity in a worktree (its creation, checkouts, commits), from the files git keeps
    in the worktree's own admin directory. A worktree created a minute ago on origin/main has no commits and a clean tree, which
    is also what a finished one looks like, so age alone must tell them apart."""
    gitdir = git(path, "rev-parse", "--absolute-git-dir", check=False)
    newest = 0
    for name in ("HEAD", "index", "logs/HEAD", "ORIG_HEAD"):
        try:
            newest = max(newest, int(os.stat(os.path.join(gitdir, name)).st_mtime))
        except OSError:
            pass
    return max(newest, last_touched(path))


def branch_activity(repo, branch):
    """Seconds since the epoch of the branch's newest reflog entry (its creation or last update), or its tip's commit time."""
    reflog = git(repo, "log", "-g", "-1", "--format=%ct", branch, check=False)
    return int(reflog) if reflog.isdigit() else int(git(repo, "log", "-1", "--format=%ct", branch, check=False) or 0)


def run(repo, apply, stale_seconds, merged_heads, now=None):
    now = now or time.time()
    report = {"removed": [], "deleted": [], "stale": [], "kept": []}
    checked_out = set()
    for path, branch in worktrees(repo):
        checked_out.add(branch)
        dirty = bool(git(path, "status", "--porcelain", check=False))
        if branch and not dirty and is_merged(repo, branch, merged_heads) and now - worktree_activity(path) >= stale_seconds:
            if apply:
                git(repo, "worktree", "remove", "--force", path)
                git(repo, "branch", "-D", branch)
            report["removed"].append((path, branch))
            continue
        ahead = branch and not is_merged(repo, branch, merged_heads)
        if (dirty or ahead) and now - last_touched(path) >= stale_seconds:
            report["stale"].append((path, branch, "uncommitted changes" if dirty else "commits not in main"))
        else:
            report["kept"].append((path, branch))
    default = "main"
    for branch in git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").splitlines():
        if branch == default or branch in checked_out:
            continue
        if is_merged(repo, branch, merged_heads) and now - branch_activity(repo, branch) >= stale_seconds:
            if apply:
                git(repo, "branch", "-D", branch)
            report["deleted"].append(branch)
        else:
            report["stale"].append((None, branch, "branch with commits not in main and no worktree"))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("repos", nargs="+")
    parser.add_argument("--apply", action="store_true", help="remove merged worktrees and delete merged branches")
    parser.add_argument("--stale-minutes", type=float, default=30)
    parser.add_argument("--no-fetch", action="store_true")
    args = parser.parse_args(argv)
    any_stale = False
    for repo in args.repos:
        if not args.no_fetch:
            git(repo, "fetch", "-q", "--prune", "origin", check=False)
        report = run(repo, args.apply, args.stale_minutes * 60, merged_pull_request_heads(repo))
        verb = "removed" if args.apply else "would remove"
        print(f"== {repo}")
        for path, branch in report["removed"]:
            print(f"  MERGED   {verb} worktree {path} ({branch})")
        for branch in report["deleted"]:
            print(f"  MERGED   {'deleted' if args.apply else 'would delete'} branch {branch}")
        for path, branch, why in report["stale"]:
            print(f"  STALE    {path or '(no worktree)'} {branch}: {why}; commit it, open a PR and merge it now, unfinished or not")
            any_stale = True
        for path, branch in report["kept"]:
            print(f"  active   {path} {branch}")
    return 1 if any_stale else 0


if __name__ == "__main__":
    sys.exit(main())
