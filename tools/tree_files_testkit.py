# SPDX-License-Identifier: AGPL-3.0-or-later

"""Test fixture shared by the tests of every checker that selects files with `tree_files`: a git checkout with a real
second worktree nested under it, the situation that made a raw `os.walk` fail one agent's lint with another's findings."""

import os
import subprocess

_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env={**os.environ, **_ENV})


def write(root, rel, content):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def checkout_files_with_nested_worktree(root, files):
    """In `root`, a git repository that tracks every `{rel: content}` of `files`, with a second real worktree of it at
    `root/tmp/other` (so `tmp/other/<rel>` exists on disk with the same content)."""
    git(root, "init", "-q")
    for rel, content in files.items():
        write(root, rel, content)
        git(root, "add", rel)
    git(root, "commit", "-q", "-m", "x")
    git(root, "worktree", "add", "-q", "--detach", os.path.join("tmp", "other"))
    for rel in files:
        assert os.path.exists(os.path.join(root, "tmp", "other", rel)), "the nested worktree must hold its own copy"


def checkout_with_nested_worktree(root, rel, content):
    """In `root`, a git repository that tracks `rel`, with a second real worktree of it at `root/tmp/other` (so
    `tmp/other/<rel>` exists on disk with the same content). Returns the path of the tracked file."""
    checkout_files_with_nested_worktree(root, {rel: content})
    return os.path.join(root, rel)
