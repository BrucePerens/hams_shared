#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
The sibling layout a hams_shared tree is linted in (night_shift_todo hams-shared-lint-gate-scans-the-main-hams-open).

`run_linters.py` started from a worktree of hams_shared used to read the sibling hams_open and hams_com MAIN
checkouts, whose `hams_shared` submodule is the commit the main checkout happens to have. Every cross-repository step
(verify_anchors, the claims and burn-list scans, ...) therefore judged the old hams_shared, never the branch: on
2026-11-07 hams_shared #411 passed its gate, merged, and broke `verify_anchors` on every branch; its fix could not
pass its own gate either. `build` makes a throwaway layout in which the sibling hams_open holds THIS hams_shared as
`hams_open/hams_shared` (a real directory with its own git repository, as a populated submodule has), with hams_com
beside it, and `run_linters.py` lints against that. Operation and the incident: tools/README.md, entries
`lint_layout:*`.

Tests [@ANCHOR: lint_layout:Layout]
Tests [@ANCHOR: lint_layout:base_commit]
Tests [@ANCHOR: lint_layout:build]
Tests [@ANCHOR: lint_layout:remove]
Tests [@ANCHOR: lint_layout:mirror_changed]
Tests [@ANCHOR: lint_layout:reap_orphans]
"""

import collections
import os
import subprocess
import shutil
import sys

import batch_queue

KIND = "lintlayout"
SHARED_NAME = "hams_shared"
# Links a repository's root carries into hams_shared (tracked in hams_open and hams_com; made here only if missing).
SHARED_LINKS = ("agents", "AGENTS.md", "docs", "tools")


class LayoutError(RuntimeError):
    """The layout could not be built. The lint run must stop: falling back to the main checkouts would silently
    bring back the bug this module exists to remove."""


# [@ANCHOR: lint_layout:Layout]
Layout = collections.namedtuple("Layout", ["parent", "hams_open", "hams_com", "shared_source", "shared_copy"])
Layout.__doc__ = """Where one layout lives (a namedtuple: parent, hams_open, hams_com, shared_source, shared_copy)."""


def _git(repo, *args, timeout=None):
    try:
        return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        return subprocess.CompletedProcess(args, 1, "", str(error))


def _checked(repo, *args):
    result = _git(repo, *args)
    if result.returncode != 0:
        raise LayoutError("git %s failed in %s: %s" % (" ".join(args[:3]), repo, (result.stderr or result.stdout).strip()[:300]))
    return result.stdout.strip()


# [@ANCHOR: lint_layout:base_commit]
def base_commit(checkout, fetch=True):
    """The commit a sibling is linted at: `origin/main` of `checkout` after a quiet fetch, else its HEAD.

    `fetch` (bool): False in tests and offline runs. The fetch has a 20 s limit and its failure is ignored (the
    comparison is then with whatever origin/main the checkout already knows). Returns a 40-character sha, or None
    when the checkout has neither ref (not a git repository)."""
    if fetch:
        _git(checkout, "fetch", "-q", "origin", timeout=20)
    for ref in ("origin/main", "HEAD"):
        result = _git(checkout, "rev-parse", "--verify", "-q", ref + "^{commit}")
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    return None


def _clone_at(source, dest, sha):
    """Detached checkout of `sha` of the repository holding `source`, in `dest`, sharing its objects (no copy of history,
    nothing registered in the source repository, so removing `dest` is just deleting the directory)."""
    top = _checked(source, "rev-parse", "--show-toplevel")
    clone = subprocess.run(["git", "clone", "-q", "--shared", "--no-checkout", top, dest], capture_output=True, text=True)
    if clone.returncode != 0:
        raise LayoutError("git clone of %s failed: %s" % (top, clone.stderr.strip()[:300]))
    _checked(dest, "checkout", "-q", "--detach", sha)


def _overlay_uncommitted(source, dest):
    """Copy `source`'s uncommitted regular files into `dest` and delete there what was deleted (secrets and symlinks
    are skipped, as lint_remote.dirty_files does), so a tree that is not yet committed is linted as it is."""
    import lint_remote

    changed, deleted = lint_remote.dirty_files(source)
    for rel in changed:
        target = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(os.path.join(source, rel), target)
    for rel in deleted:
        try:
            os.remove(os.path.join(dest, rel))
        except OSError:
            pass


def _ensure_links(repo):
    for name in SHARED_LINKS:
        path = os.path.join(repo, name)
        if not os.path.lexists(path):
            os.symlink(os.path.join(SHARED_NAME, name), path)


# [@ANCHOR: lint_layout:build]
def build(shared_tree, open_main, com_main, environ=None, fetch=True, out=print):
    """Create the layout for linting the hams_shared worktree `shared_tree`; returns a `Layout`.

    `shared_tree` (str): the hams_shared worktree (or checkout) under lint; read only. `open_main`, `com_main` (str):
    the main hams_open and hams_com checkouts (read only; `com_main` may be None or missing, then the layout has no
    hams_com). `environ` (mapping or None): `HAMS_BATCH_TREE_DIR` names the directory the layout goes under (default
    `~/workspace/tmp`). `fetch` (bool): fetch the siblings' origin first (see `base_commit`). `out`: progress printer.

    The layout is `<parent>/hams_open` (a clone at `base_commit(open_main)`, whose `hams_shared` directory is a clone
    of `shared_tree`'s HEAD plus its uncommitted files) and `<parent>/hams_com` (a clone at `base_commit(com_main)`;
    its tracked `hams_shared` link then resolves into the layout's hams_open). Raises `LayoutError` on any failure and
    removes what it made first. Cost: a checkout of each sibling (seconds to tens of seconds), no network. Call `remove`
    when the run ends (run_linters registers it with atexit); `reap_orphans` clears layouts of runs that were killed."""
    parent = batch_queue.tree_parent(KIND, SHARED_NAME, environ=environ)
    hams_open = os.path.join(parent, "hams_open")
    hams_com = os.path.join(parent, "hams_com")
    shared_copy = os.path.join(hams_open, SHARED_NAME)
    layout = Layout(parent, hams_open, None, os.path.realpath(shared_tree), shared_copy)
    try:
        os.makedirs(parent, exist_ok=True)
        open_sha = base_commit(open_main, fetch)
        if not open_sha:
            raise LayoutError("%s has no origin/main and no HEAD" % open_main)
        _clone_at(open_main, hams_open, open_sha)
        shared_sha = _checked(shared_tree, "rev-parse", "HEAD")
        _clone_at(shared_tree, shared_copy, shared_sha)
        _overlay_uncommitted(shared_tree, shared_copy)
        _ensure_links(hams_open)
        com_sha = None
        if com_main and os.path.isdir(com_main):
            com_sha = base_commit(com_main, fetch)
            if com_sha:
                _clone_at(com_main, hams_com, com_sha)
                layout = layout._replace(hams_com=hams_com)
    except (LayoutError, OSError):
        remove(layout)
        raise
    out("[*] hams_shared tree: linting in a layout with this hams_shared inside hams_open %s%s (%s)" % (
        open_sha[:9], ", hams_com " + com_sha[:9] if com_sha else "", parent))
    return layout


# [@ANCHOR: lint_layout:remove]
def remove(layout):
    """Delete a layout's directory. Safe to call twice and on a half-built layout. Files a test run left owned by
    another user are removed with a non-interactive `sudo -n rm -rf` (`batch_queue.remove_tree`). Returns None."""
    batch_queue.remove_tree(layout.hams_open, layout.hams_open)


# [@ANCHOR: lint_layout:mirror_changed]
def mirror_changed(changed, layout):
    """`changed` (a set of absolute real paths) plus the layout's copy of every path below the hams_shared tree.

    `--changed` reports the files the branch changed in the hams_shared worktree; the layout lints a copy of them, and a
    step whose inputs are rooted at hams_open would not see the change without the copy's path. Returns a new set;
    `changed` is not modified. With `layout` None it returns `changed` as it is."""
    if layout is None:
        return changed
    prefix = layout.shared_source + os.sep
    extra = {os.path.join(layout.shared_copy, path[len(prefix):]) for path in changed if path.startswith(prefix)}
    return set(changed) | extra


# [@ANCHOR: lint_layout:reap_orphans]
def reap_orphans(repo, environ=None):
    """Delete layouts whose run was killed (`batch_queue.reap_orphan_trees` with this module's kind). `repo` is any
    git checkout (the batch cleanup runs `git worktree prune` there). Returns the removed directories."""
    return batch_queue.reap_orphan_trees(repo, KIND, SHARED_NAME, environ=environ)


if __name__ == "__main__":
    sys.exit("lint_layout.py is a library for run_linters.py")
