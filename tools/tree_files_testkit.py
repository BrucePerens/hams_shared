# SPDX-License-Identifier: AGPL-3.0-or-later

"""Test fixture shared by the tests of every checker that selects files with `tree_files`: a git checkout with a real
second worktree nested under it, the situation that made a raw `os.walk` fail one agent's lint with another's findings."""

import builtins
import contextlib
import io
import os
import runpy
import subprocess
import sys
from unittest import mock

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


TOOLS = os.path.dirname(os.path.abspath(__file__))


def run_checker(script, root, cache_home):
    """Runs the checker as a command in this process; returns (paths it opened, its output)."""
    opened = []
    real_open = builtins.open

    def spy(file, *args, **kwargs):
        if isinstance(file, (str, os.PathLike)):
            opened.append(os.fspath(file))
        return real_open(file, *args, **kwargs)

    out = io.StringIO()
    patches = [mock.patch.object(builtins, "open", spy), mock.patch.object(io, "open", spy)]
    try:  # lxml parses a path itself, without going through open()
        from lxml import etree

        real_parse = etree.parse

        def parse_spy(source, *args, **kwargs):
            opened.append(os.fspath(source) if isinstance(source, (str, os.PathLike)) else "")
            return real_parse(source, *args, **kwargs)

        patches.append(mock.patch.object(etree, "parse", parse_spy))
    except ImportError:
        pass
    env = {"XDG_CACHE_HOME": cache_home, "HOME": cache_home}
    with contextlib.ExitStack() as stack:
        for patch in [mock.patch.dict(os.environ, env), *patches]:
            stack.enter_context(patch)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out), mock.patch.object(sys, "argv", [script, root]):
            try:
                runpy.run_path(os.path.join(TOOLS, script), run_name="__main__")
            except SystemExit:
                pass
    return opened, out.getvalue()


def check_nested_worktree_table(test, table):
    """For each `script: (files, probe)` of `table`: run the checker (it gets the checkout as its argument) on a checkout that
    tracks `files` and has a real nested worktree, and again on one whose `files` are new and not `git add`ed. The tree's own
    `probe` file must be read or reported, nothing under `tmp/other` may be. `test` is a unittest.TestCase."""
    import shutil
    import tempfile

    tmp = os.path.realpath(tempfile.mkdtemp())
    test.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
    cache, root = os.path.join(tmp, "cache"), os.path.join(tmp, "checkout")
    for script, (files, probe) in table.items():
        for added in (True, False):
            with test.subTest(script=script, git_added=added):
                shutil.rmtree(root, ignore_errors=True)
                os.makedirs(root)
                checkout_files_with_nested_worktree(root, files if added else {".keep": ""})
                for rel, content in ({} if added else files).items():
                    write(root, rel, content)
                opened, out = run_checker(script, root, cache)
                everything = "\n".join(opened) + "\n" + out
                test.assertIn(os.path.join(*probe.split("/")), everything, "the tree's own file must be scanned")
                test.assertNotIn(os.path.join("tmp", "other"), everything)
