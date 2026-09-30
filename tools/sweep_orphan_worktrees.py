#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
ADR-0102: sweeps orphaned git worktrees left behind by a session that never reached its own
graceful exit (a crash, a force-quit) or one created by hand with `git worktree add`.
`ExitWorktree`'s own keep/remove prompt already covers the graceful-exit case and already
refuses to remove a dirty worktree -- this sweep only covers what that prompt can't reach.

Run once per night-watch hourly cycle (see that skill's own SKILL.md), against each repo whose
worktrees live under `.claude/worktrees/`.

Never force-deletes anything with real content in it. Three outcomes per worktree found:
  - its directory is already gone: `git worktree prune` (always safe, native to git).
  - present, clean, and fully pushed, and idle for at least `_MIN_IDLE_HOURS`: removed.
  - anything else (dirty, unpushed, or too recently touched to be confident it's abandoned):
    left alone, flagged once as a night_shift_todo/low/ entry rather than destroyed.

"Clean" tolerates exactly one known, deliberately-never-committed source of dirt:
`fix_worktree_symlinks.py`'s own local re-pointing of AGENTS.md/hams_shared (and similar
checked-in relative symlinks) to an absolute, machine-specific target so they resolve correctly
inside a worktree -- see hams_com/CLAUDE.md's own "Git Worktrees" section. Almost every real
worktree ever created carries this exact drift and nothing else, which used to make this sweep
flag (never remove) nearly all of them even when they were otherwise fully idle and pushed.
"""
import logging
import os
import subprocess
import sys
import time
import uuid

_logger = logging.getLogger("sweep_orphan_worktrees")

_MIN_IDLE_HOURS = 6
_WORKTREES_SUBDIR = ".claude/worktrees"


def _run(args, cwd):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=60)


def _list_worktrees(repo_root):
    result = _run(["git", "worktree", "list", "--porcelain"], cwd=repo_root)
    worktrees = []
    current = {}
    for line in result.stdout.splitlines():
        if not line:
            if current:
                worktrees.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    if current:
        worktrees.append(current)
    return worktrees


def _idle_hours(path):
    newest = 0.0
    for dirpath, dirnames, filenames in os.walk(path):
        if ".git" in dirnames:
            dirnames.remove(".git")
        for name in filenames:
            try:
                newest = max(newest, os.path.getmtime(os.path.join(dirpath, name)))
            except OSError:
                continue
    if newest == 0.0:
        return float("inf")
    return (time.time() - newest) / 3600.0


def _real_symlink_target(anchor_dir, link_content):
    """Resolve a symlink's stored content (absolute or relative), as if the symlink lived in
    `anchor_dir`, to its fully-resolved real filesystem path -- following any intermediate
    symlinks the resolved path passes through (e.g. a path built via a `hams_shared` symlink
    compares equal to the same real location written out in full), and without requiring the
    final target itself to exist."""
    candidate = link_content if os.path.isabs(link_content) else os.path.join(anchor_dir, link_content)
    return os.path.realpath(candidate)


def _is_known_symlink_localization_drift(repo_root, worktree_path, modified_paths):
    """True only if every path in `modified_paths` is a symlink whose sole change is
    fix_worktree_symlinks.py's own local, machine-specific, deliberately-never-committed
    absolute-path rewrite (see hams_com/CLAUDE.md's "Git Worktrees" section -- AGENTS.md and
    hams_shared are checked-in relative symlinks that tool re-points to an absolute, correct
    target inside a worktree, because their committed *relative* target is written assuming the
    symlink sits at the main checkout's own top level, and resolves to the wrong place once the
    same relative string sits several directories deeper inside a worktree).

    Narrow on purpose: each symlink's actual on-disk real target must equal what its committed
    content would resolve to if the same symlink instead sat at its own path directly under
    `repo_root` (the main checkout) -- not merely "some path under this repo" -- so a genuine
    accidental edit to one of these files is never masked as this drift."""
    if not modified_paths:
        return False
    for rel in modified_paths:
        full = os.path.join(worktree_path, rel)
        if not os.path.islink(full):
            return False
        committed = _run(["git", "show", f"HEAD:{rel}"], cwd=worktree_path)
        if committed.returncode != 0:
            return False
        working_real = os.path.realpath(full)
        intended_real = _real_symlink_target(os.path.dirname(os.path.join(repo_root, rel)), committed.stdout)
        if working_real != intended_real:
            return False
    return True


def _is_clean_and_pushed(repo_root, path):
    status = _run(["git", "status", "--porcelain"], cwd=path)
    if status.stdout.strip():
        lines = status.stdout.splitlines()
        # " M path" only -- an unstaged modification to a tracked file, nothing staged, added,
        # deleted or renamed. Anything else (including a bare "M path", i.e. staged) is real
        # dirt this function must not wave through.
        modified = [line[3:] for line in lines if line[:2] == " M"]
        if len(modified) != len(lines) or not _is_known_symlink_localization_drift(repo_root, path, modified):
            return False
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=path).stdout.strip()
    upstream = _run(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd=path
    )
    if upstream.returncode != 0:
        # No upstream at all: never assume that means "safe to delete" -- an unpushed
        # branch with no tracking configured is exactly the case this must not destroy.
        return False
    ahead = _run(["git", "rev-list", "--count", f"{upstream.stdout.strip()}..{branch}"], cwd=path)
    return ahead.returncode == 0 and ahead.stdout.strip() == "0"


def _todo_already_flagged(repo_root, worktree_path):
    todo_dir = os.path.join(repo_root, "night_shift_todo", "low")
    if not os.path.isdir(todo_dir):
        return False
    needle = os.path.basename(worktree_path)
    for name in os.listdir(todo_dir):
        full = os.path.join(todo_dir, name)
        if os.path.isfile(full):
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                if needle in f.read():
                    return True
    return False


def _flag_worktree(repo_root, worktree_path, branch):
    slug = f"orphaned-worktree-{os.path.basename(worktree_path)}-{uuid.uuid4().hex[:8]}"
    todo_dir = os.path.join(repo_root, "night_shift_todo", "low")
    os.makedirs(todo_dir, exist_ok=True)
    content = f"""---
status: open
claimed_by:
repo: {os.path.basename(repo_root)}
category: cleanup
created_at: {time.strftime('%Y-%m-%d')}
---

# Orphaned worktree with real content: `{worktree_path}`

## What

`sweep_orphan_worktrees.py` (ADR-0102) found this worktree idle for over {_MIN_IDLE_HOURS}
hours with uncommitted changes and/or commits not yet pushed to its upstream (branch
`{branch}`). Never force-deleted -- flagged here instead so a person or a session can look at
what it holds before anything is lost.

## Done when

Someone has looked at `{worktree_path}`, decided whether its content is still wanted, and
either pushed/committed what should be kept then removed the worktree (`git worktree remove
{worktree_path}`), or removed it outright if it was genuinely abandoned.
"""
    path = os.path.join(todo_dir, f"{slug}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    _logger.info("Flagged %s -> %s", worktree_path, path)


def sweep_repo(repo_root):
    _run(["git", "worktree", "prune"], cwd=repo_root)

    for wt in _list_worktrees(repo_root):
        path = wt.get("worktree")
        if not path or not os.path.isdir(path):
            continue
        if _WORKTREES_SUBDIR not in path:
            continue  # the main checkout itself, or something not managed by this convention
        if "bare" in wt:
            continue

        idle_hours = _idle_hours(path)
        if idle_hours < _MIN_IDLE_HOURS:
            _logger.info("Skipping %s: touched %.1fh ago, too recent to call orphaned", path, idle_hours)
            continue

        if _is_clean_and_pushed(repo_root, path):
            _logger.info("Removing clean, fully-pushed, idle worktree %s", path)
            # --force: `git worktree remove` does its own, independent dirt check and refuses
            # a worktree carrying only the known symlink-localization drift just as readily as
            # one with real changes -- but `_is_clean_and_pushed` above has already verified any
            # dirt present is exactly that harmless, narrowly-matched pattern (or none at all),
            # so it is safe to override git's own more conservative refusal here specifically.
            _run(["git", "worktree", "remove", "--force", path], cwd=repo_root)
        elif not _todo_already_flagged(repo_root, path):
            branch = wt.get("branch", "").removeprefix("refs/heads/")
            _flag_worktree(repo_root, path, branch)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for repo_root in sys.argv[1:]:
        sweep_repo(os.path.abspath(repo_root))
    return 0


if __name__ == "__main__":
    sys.exit(main())
