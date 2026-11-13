#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Worktree Hygiene
----------------
Half-finished worktrees and branches pile up (about 300 local branches were found in hams_open and hams_com on 2026-10-26, most
already squash-merged). This tool finds them and removes the ones that are provably finished.

  worktree_hygiene.py [--apply] [--stale-minutes N] [--no-fetch] [--branch NAME [--tip SHA]] REPO...

For every REPO (the main working tree of a repository):
  * A linked worktree whose branch is merged (no commits beyond origin/main, or its tip is the head of a merged pull request),
    whose tree is clean and that has seen no git activity for N minutes is MERGED: with --apply it is removed, then its branch
    is deleted. A worktree made a minute ago has no commits either, so only its idleness says it is not about to be used.
  * A local branch with no worktree that is merged and was last updated more than N minutes ago is deleted with --apply.
  * A worktree that has uncommitted changes, or commits not in origin/main, and has not been touched for N minutes (default 30)
    is STALE: it is only reported, never removed. The fix is to commit the named paths, open a pull request and merge it, even
    if the work is unfinished (say so in the pull request), then run this tool again.
  * A worktree that a running process holds (its working directory, an open file or its executable is inside the worktree) is HELD:
    it is reported and never removed, merged or not, since removing the directory under a running test, editor or shell breaks that
    process. The shell that started this tool, and the shells above it, do not count: `prmerge.sh` run from the merged worktree
    must be able to clean up the worktree it ran in. Processes of other users (the `odoo` user running `test.py`) are found through
    `sudo -n`; without passwordless sudo only the caller's own processes can be seen.
  * `--branch NAME` is the post-merge mode `prmerge.sh` uses: only the worktree and local branch of NAME are considered, and no
    idle time is required, because the pull request has just merged and nothing else is going to use them. Dirty, unmerged (the
    local tip is not the merged head; `--tip SHA` names that head) and held worktrees are still kept, and reported with the reason.
  * A linked worktree that lies INSIDE another git working tree (hams_open/tmp/..., .claude/worktrees/...) is NESTED (2026-11-09):
    anchor scans, ESLint and other checks that walk a checkout then scan it too. It is reported with the command that makes the clean
    layout; with `--move-nested --apply` a clean, unheld, unlocked one is moved to `<workspace>/tmp/<job>/<repo>` (`hams_shared_wt` for hams_shared) with the sibling
    links of `devbox_tools/new_agent_worktree.py`. Dirty, held and locked ones stay where they are and say why. Make worktrees with
    `devbox_tools/new_agent_worktree.py` and they are never nested.
  * Jobs that tool registered (`~/.local/state/agent-worktrees/<job>.json`, AGENT_WORKTREE_STATE overrides) whose worktree is gone and
    whose job directory holds nothing but symlinks have the directory removed with --apply (JOB).
Exit status 1 when anything is STALE or NESTED, so a timer or a session start can surface it.

Nothing unmerged or dirty is ever deleted. Merged pull request heads come from `gh pr list`; without gh only the plain
"no commits beyond origin/main" test is used.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

_SHELLS = ("bash", "sh", "dash", "zsh", "ksh", "fish")


# A fetch to a hung GitHub must not hang the half-hourly timer unit that runs this module.
GIT_TIMEOUT_SECONDS = 300
GH_TIMEOUT_SECONDS = 120


# [@ANCHOR: worktree_hygiene:git]
def git(repo, *args, check=True):
    try:
        result = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        if check:
            raise RuntimeError(f"git {' '.join(args)} in {repo}: no answer in {GIT_TIMEOUT_SECONDS} s")
        return ""
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} in {repo}: {result.stderr.strip()}")
    return result.stdout.strip()


# [@ANCHOR: worktree_hygiene:merged_pull_request_heads]
def merged_pull_request_heads(repo):
    """{branch name: set of tip shas} of merged pull requests of the repository the working tree belongs to."""
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--state", "merged", "--limit", "1000", "--json", "headRefName,headRefOid"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {}  # gh did not answer: the plain "no commits beyond origin/main" test applies
    except OSError:
        return {}  # gh is not installed: only the plain "no commits beyond origin/main" test applies, as the module docstring says
    if result.returncode != 0:
        return {}
    heads = {}
    for entry in json.loads(result.stdout or "[]"):
        heads.setdefault(entry["headRefName"], set()).add(entry["headRefOid"])
    return heads


# [@ANCHOR: worktree_hygiene:all_worktrees]
def all_worktrees(repo):
    """[(path, branch or None)] of every worktree, the main working tree first."""
    entries, current = [], {}
    for line in git(repo, "worktree", "list", "--porcelain").splitlines() + [""]:
        if not line:
            if current:
                entries.append((current["worktree"], current.get("branch")))
            current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value.removeprefix("refs/heads/") if key == "branch" else value
    return entries


def worktrees(repo):
    """[(path, branch or None)] of the linked worktrees (the main working tree is not one)."""
    return all_worktrees(repo)[1:]


# [@ANCHOR: worktree_hygiene:locked_worktrees]
def locked_worktrees(repo):
    """Set of worktree paths marked `git worktree lock` (a batch lint run's, or one on removable media). They are never removed."""
    locked, path = set(), None
    for line in git(repo, "worktree", "list", "--porcelain").splitlines():
        key, _, value = line.partition(" ")
        if key == "worktree":
            path = value
        elif key == "locked":
            locked.add(path)
    return locked


# [@ANCHOR: worktree_hygiene:is_merged]
def is_merged(repo, branch, merged_heads, base="origin/main"):
    tip = git(repo, "rev-parse", branch)
    if int(git(repo, "rev-list", "--count", f"{base}..{branch}")) == 0:
        return True
    return tip in merged_heads.get(branch, set())


# [@ANCHOR: worktree_hygiene:last_touched]
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


# [@ANCHOR: worktree_hygiene:worktree_activity]
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


# [@ANCHOR: worktree_hygiene:branch_activity]
def branch_activity(repo, branch):
    """Seconds since the epoch of the branch's newest reflog entry (its creation or last update), or its tip's commit time."""
    # `%ct` of a `-g` walk is the commit's own time, not the reflog entry's, so a branch created now at an old commit would read as idle.
    entry = git(repo, "log", "-g", "-1", "--date=unix", "--format=%gd", branch, check=False)
    reflog = entry[entry.rfind("@{") + 2 : -1] if entry.endswith("}") and "@{" in entry else ""
    return int(reflog) if reflog.isdigit() else int(git(repo, "log", "-1", "--format=%ct", branch, check=False) or 0)


def _inside(link_target, real_root):
    return link_target == real_root or link_target.startswith(real_root + os.sep)


# [@ANCHOR: worktree_hygiene:scan_holders]
def scan_holders(path):
    """[(pid, command)] of every process this user can inspect whose working directory, executable or an open file is inside `path`,
    and the number of processes it could not inspect (another user's). `/proc` is read directly, so there is no `lsof` dependency."""
    root = os.path.realpath(path)
    holders, unreadable = [], 0
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        proc = f"/proc/{entry}"
        targets = []
        try:
            for name in ("cwd", "exe"):
                try:
                    targets.append(os.readlink(f"{proc}/{name}"))
                except FileNotFoundError:
                    pass  # a kernel thread has no cwd or exe link target; a process that just exited has no /proc entry
            for fd in os.listdir(f"{proc}/fd"):
                try:
                    targets.append(os.readlink(f"{proc}/fd/{fd}"))
                except FileNotFoundError:
                    pass
            with open(f"{proc}/comm") as handle:
                command = handle.read().strip()
        except PermissionError:
            unreadable += 1
            continue
        except FileNotFoundError:
            continue  # exited while scanning
        if any(_inside(target.removesuffix(" (deleted)"), root) for target in targets):
            holders.append((int(entry), command))
    return holders, unreadable


def _parent_and_command(pid):
    """(parent pid, command) of a process from /proc/<pid>/stat; (0, "") when it has gone."""
    try:
        with open(f"/proc/{pid}/stat") as handle:
            stat = handle.read()
    except OSError:
        return 0, ""
    command = stat[stat.index("(") + 1 : stat.rindex(")")]
    return int(stat[stat.rindex(")") + 2 :].split()[1]), command


# [@ANCHOR: worktree_hygiene:calling_shells]
def calling_shells():
    """Pids that must not count as holding a worktree: this process, and the chain of shells directly above it (the script that ran this
    tool, the `bash -c` of an agent's command), up to the first process that is not a shell."""
    pids = {os.getpid()}
    pid = os.getppid()
    while pid > 1:
        parent, command = _parent_and_command(pid)
        if command not in _SHELLS:
            break
        pids.add(pid)
        pid = parent
    return pids


# [@ANCHOR: worktree_hygiene:holding_processes]
def holding_processes(path, exempt=None):
    """[(pid, command)] of the running processes that hold `path` (see scan_holders), except `exempt` (default: calling_shells()).
    Processes of other users are found by running this module's own `--holders` mode under `sudo -n`; when that is not available
    only the caller's own processes are known."""
    exempt = calling_shells() if exempt is None else exempt
    holders, unreadable = scan_holders(path)
    if unreadable and shutil.which("sudo"):
        # cwd="/" so the helper's own working directory is not the worktree it is looking for.
        result = subprocess.run(
            ["sudo", "-n", sys.executable, "-I", os.path.abspath(__file__), "--holders", path],
            capture_output=True,
            text=True,
            cwd="/",
        )
        if result.returncode == 0:
            holders = [(int(line.split(None, 1)[0]), line.split(None, 1)[1]) for line in result.stdout.splitlines() if line.strip()]
    return sorted(set(h for h in holders if h[0] not in exempt))


def _idle(now, touched, stale_seconds):
    """True when something last touched at `touched` has been idle for `stale_seconds`. A limit of 0 (the post-merge `--branch` mode) is
    always met: file times can be a moment ahead of time.time(), so a change made just now would otherwise read as not yet idle."""
    return stale_seconds <= 0 or now - touched >= stale_seconds


# [@ANCHOR: worktree_hygiene:container_checkout]
def container_checkout(path):
    """The git working tree that contains the worktree `path` (an ancestor holding a `.git`), or None for a worktree in a clean place."""
    here = os.path.dirname(os.path.abspath(path))
    while True:
        if os.path.exists(os.path.join(here, ".git")):
            return here
        parent = os.path.dirname(here)
        if parent == here:
            return None
        here = parent


# [@ANCHOR: worktree_hygiene:nested_worktrees]
def nested_worktrees(repo, holders_of=holding_processes):
    """[(path, branch, container, why_kept or None)] of the linked worktrees lying inside another checkout. why_kept is set for one
    that must not be moved: dirty, locked or held by a running process."""
    locked = locked_worktrees(repo)
    found = []
    for path, branch in worktrees(repo):
        container = container_checkout(path)
        if not container:
            continue
        if path in locked:
            why = "locked"
        elif git(path, "status", "--porcelain", check=False):
            why = "uncommitted changes"
        else:
            holders = holders_of(path)
            why = "in use by " + ", ".join(f"{command} (pid {pid})" for pid, command in holders) if holders else None
        found.append((path, branch, container, why))
    return found


# Sibling links a moved worktree's job directory carries, as devbox_tools/new_agent_worktree.py makes them.
_SIBLINGS = {"hams_com": ("hams_open",), "hams_open": ("hams_com",), "hams_shared": ("hams_com", "hams_open")}


# [@ANCHOR: worktree_hygiene:move_nested]
def move_nested(repo, path, workspace=None):
    """Moves the clean nested worktree `path` of `repo` to <workspace>/tmp/<job>/<repo name> with the sibling links. Returns the new
    path. `git worktree move` refuses some trees (one with a submodule); the error is raised and nothing is left half-made."""
    workspace = workspace or os.path.expanduser("~/workspace")
    name = os.path.basename(all_worktrees(repo)[0][0].rstrip("/"))
    # A hams_shared tree is never named hams_shared: run_linters.py would take its parent for the repository root.
    tree_name = "hams_shared_wt" if name == "hams_shared" else name
    job_dir = os.path.join(workspace, "tmp", "moved-" + os.path.basename(path.rstrip("/")))
    target = os.path.join(job_dir, tree_name)
    if os.path.lexists(job_dir):
        raise RuntimeError(f"{job_dir} already exists")
    os.makedirs(job_dir)
    try:
        git(repo, "worktree", "move", path, target)
    except RuntimeError:
        os.rmdir(job_dir)
        raise
    for sibling in _SIBLINGS.get(name, ()):
        main = os.path.join(workspace, "hams_open", "hams_shared") if sibling == "hams_shared" else os.path.join(workspace, sibling)
        os.symlink(main, os.path.join(job_dir, sibling))
    return target


# [@ANCHOR: worktree_hygiene:registered_jobs]
def registered_jobs(state_dir=None):
    """[(record file, record dict)] of the jobs devbox_tools/new_agent_worktree.py registered. An unreadable record is skipped."""
    state_dir = state_dir or os.environ.get("AGENT_WORKTREE_STATE") or os.path.expanduser("~/.local/state/agent-worktrees")
    jobs = []
    if os.path.isdir(state_dir):
        for name in sorted(os.listdir(state_dir)):
            if not name.endswith(".json"):
                continue
            path = os.path.join(state_dir, name)
            try:
                with open(path, encoding="utf-8") as handle:
                    jobs.append((path, json.load(handle)))
            except (OSError, ValueError):
                continue
    return jobs


# [@ANCHOR: worktree_hygiene:reap_finished_jobs]
def reap_finished_jobs(apply, state_dir=None):
    """Removes the job directory (and record) of each registered job whose worktree is gone, when the directory holds only symlinks.
    Returns [(job_dir, removed bool)]; a directory with anything else in it is reported (False) and kept."""
    results = []
    for record_path, record in registered_jobs(state_dir):
        worktree, job_dir = record.get("worktree"), record.get("job_dir")
        if not worktree or not job_dir or os.path.lexists(worktree):
            continue
        leftovers = [n for n in os.listdir(job_dir) if not os.path.islink(os.path.join(job_dir, n))] if os.path.isdir(job_dir) else []
        if leftovers:
            results.append((job_dir, False))
            continue
        if apply:
            if os.path.isdir(job_dir):
                for n in os.listdir(job_dir):
                    os.remove(os.path.join(job_dir, n))
                os.rmdir(job_dir)
            os.remove(record_path)
        results.append((job_dir, True))
    return results


# [@ANCHOR: worktree_hygiene:run]
def run(repo, apply, stale_seconds, merged_heads, now=None, only_branch=None, holders_of=holding_processes):
    now = now or time.time()
    report = {"removed": [], "deleted": [], "stale": [], "kept": [], "held": []}
    # The main working tree's branch is checked out too: `git branch -D` refuses it, and that error would abort the whole run.
    checked_out = {all_worktrees(repo)[0][1]}
    locked = locked_worktrees(repo)
    for path, branch in worktrees(repo):
        checked_out.add(branch)
        if only_branch and branch != only_branch:
            continue
        if path in locked:
            report["held"].append((path, branch, [("locked", "git worktree lock")]))
            continue
        dirty = bool(git(path, "status", "--porcelain", check=False))
        if branch and not dirty and is_merged(repo, branch, merged_heads) and _idle(now, worktree_activity(path), stale_seconds):
            holders = holders_of(path)
            if holders:
                report["held"].append((path, branch, holders))
                continue
            if apply:
                git(repo, "worktree", "remove", "--force", path)
                git(repo, "branch", "-D", branch)
            report["removed"].append((path, branch))
            continue
        ahead = branch and not is_merged(repo, branch, merged_heads)
        if (dirty or ahead) and _idle(now, last_touched(path), stale_seconds):
            report["stale"].append((path, branch, "uncommitted changes" if dirty else "commits not in main"))
        else:
            report["kept"].append((path, branch))
    default = "main"
    for branch in git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").splitlines():
        if branch == default or branch in checked_out or (only_branch and branch != only_branch):
            continue
        if is_merged(repo, branch, merged_heads) and _idle(now, branch_activity(repo, branch), stale_seconds):
            if apply:
                git(repo, "branch", "-D", branch)
            report["deleted"].append(branch)
        else:
            report["stale"].append((None, branch, "branch with commits not in main and no worktree"))
    return report


# [@ANCHOR: worktree_hygiene:main]
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("repos", nargs="*")
    parser.add_argument("--apply", action="store_true", help="remove merged worktrees and delete merged branches")
    parser.add_argument("--stale-minutes", type=float, default=30)
    parser.add_argument("--no-fetch", action="store_true")
    parser.add_argument("--move-nested", action="store_true", help="with --apply: move clean, unheld worktrees nested inside a checkout to ~/workspace/tmp/<job>/")
    parser.add_argument("--branch", help="only this branch and its worktree, with no idle time required (the post-merge cleanup)")
    parser.add_argument("--tip", help="with --branch: the commit the pull request merged (its head); skips the `gh pr list` lookup")
    parser.add_argument("--holders", metavar="PATH", help="print 'pid command' of each process holding PATH and exit (used under sudo)")
    args = parser.parse_args(argv)
    if args.holders:
        for pid, command in scan_holders(args.holders)[0]:
            print(pid, command)
        return 0
    if not args.repos:
        parser.error("REPO is required")
    any_stale = False
    for repo in args.repos:
        if not args.no_fetch:
            git(repo, "fetch", "-q", "--prune", "origin", check=False)
        stale_seconds = 0 if args.branch else args.stale_minutes * 60
        merged = {args.branch: {args.tip}} if args.branch and args.tip else merged_pull_request_heads(repo)
        report = run(repo, args.apply, stale_seconds, merged, only_branch=args.branch)
        verb = "removed" if args.apply else "would remove"
        print(f"== {repo}")
        for path, branch in report["removed"]:
            print(f"  MERGED   {verb} worktree {path} ({branch})")
        for branch in report["deleted"]:
            print(f"  MERGED   {'deleted' if args.apply else 'would delete'} branch {branch}")
        for path, branch, why in report["stale"]:
            print(f"  STALE    {path or '(no worktree)'} {branch}: {why}; commit it, open a PR and merge it now, unfinished or not")
            any_stale = True
        for path, branch, holders in report["held"]:
            who = ", ".join(f"{command} (pid {pid})" for pid, command in holders)
            print(f"  HELD     {path} {branch}: kept, in use by {who}")
        for path, branch in report["kept"]:
            print(f"  active   {path} {branch}")
        for path, branch, container, why in nested_worktrees(repo):
            if why:
                any_stale = True
                print(f"  NESTED   {path} {branch}: inside {container}; kept ({why}); move it once it is free")
            elif args.move_nested and args.apply and not args.branch:
                try:
                    print(f"  NESTED   moved {path} ({branch}) to {move_nested(repo, path)}")
                except RuntimeError as error:
                    any_stale = True
                    print(f"  NESTED   {path} {branch}: inside {container}; could not move it: {error}")
            else:
                any_stale = True
                print(f"  NESTED   {path} {branch}: inside {container}, so scans of that checkout read it; "
                      f"`--move-nested --apply` moves it to ~/workspace/tmp/<job>/ (or make it with devbox_tools/new_agent_worktree.py)")
    if not args.branch:
        for job_dir, removed in reap_finished_jobs(args.apply):
            if removed:
                print(f"  JOB      {'removed' if args.apply else 'would remove'} finished job directory {job_dir}")
            else:
                print(f"  JOB      {job_dir}: worktree gone but the directory holds other files; kept")
    return 1 if any_stale else 0


if __name__ == "__main__":
    sys.exit(main())
