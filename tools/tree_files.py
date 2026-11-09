# SPDX-License-Identifier: AGPL-3.0-or-later

"""Which files of a checkout a source checker should scan.

Only the files that belong to the tree being linted. A raw `os.walk` of the checkout also visits whatever else
sits in the directory: another agent's worktree made with a relative path under it (`tmp/<name>/hams_open`), an
untracked scratch copy, a build output. Those are not part of this tree and fail it with someone else's findings
(`check_test_temp_dirs` hit this first, #512).

In a git work tree the files come from `git ls-files` (submodules included), so nothing untracked or nested is
visited. Outside git (a bare directory, as in unit tests) the directory is walked, but a directory that carries its
own `.git` is another checkout and is not entered.
"""

import os
import subprocess

# Bound at import: a checker's own test patches `subprocess.run` to fake the tool it drives (`cargo deny`, `pip_audit`), and
# the file selection must still ask the real git.
_run = subprocess.run


def _ls_files(root, args, suffixes):
    cmd = ["git", "-C", root, "ls-files", "-z", *args, "--", *["*" + s for s in suffixes]]
    try:
        done = _run(cmd, capture_output=True, check=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    return [rel for rel in done.stdout.decode("utf-8", "replace").split("\0") if rel]


def _submodule_paths(root):
    try:
        done = _run(["git", "-C", root, "ls-files", "-z", "--stage"], capture_output=True, check=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return []
    paths = []
    for entry in done.stdout.decode("utf-8", "replace").split("\0"):
        meta, _, path = entry.partition("\t")
        if meta.startswith("160000 "):
            paths.append(path)
    return paths


def git_tracked(root, suffixes, untracked=False):
    """Absolute paths of the files git tracks under `root` (submodules included) whose names end in one of
    `suffixes`, or None when `root` is not inside a git work tree. A tracked file that is deleted on disk is left out.

    `untracked=True` adds the files that are neither tracked nor ignored (`git ls-files --others --exclude-standard`,
    also inside each submodule): a new file not yet `git add`ed is still checked. A directory that holds another
    checkout (a nested worktree or clone) is reported by git as one `dir/` entry and is never entered."""
    rels = _ls_files(root, ["--cached", "--recurse-submodules"], suffixes)
    if rels is None:
        return None
    if untracked:
        others = [(root, rel) for rel in _ls_files(root, ["--others", "--exclude-standard"], suffixes) or []]
        for sub in _submodule_paths(root):
            sub_root = os.path.join(root, sub)
            if os.path.isdir(sub_root):
                others += [(sub_root, rel) for rel in _ls_files(sub_root, ["--others", "--exclude-standard"], suffixes) or []]
        rels = rels + [os.path.relpath(os.path.join(base, rel), root) for base, rel in others]
    # A deleted file is gone; a directory is not a file (git lists a nested checkout as one entry, with or without a
    # trailing slash depending on the pathspec).
    paths = [os.path.join(root, rel) for rel in rels]
    return [path for path in dict.fromkeys(paths) if os.path.lexists(path) and not os.path.isdir(path)]


def tree_files(root, suffixes, skip_dirs=(), skip_hidden=False, accept=None, untracked=False):
    """Sorted absolute paths of the files under `root` that belong to its own tree.

    `suffixes`: file-name endings to keep (a string or a tuple). `skip_dirs`: directory names never entered.
    `skip_hidden`: also skip every directory whose name starts with a dot. `accept(rel_path)`: an optional extra
    filter on the path relative to `root` ('/'-separated), for rules a suffix cannot express. `untracked`: also
    return new files that git does not track yet but does not ignore (see `git_tracked`); outside git the walk
    already sees every file."""
    suffixes = (suffixes,) if isinstance(suffixes, str) else tuple(suffixes)
    skip_dirs = set(skip_dirs)

    def keep_dir(name):
        return name not in skip_dirs and not (skip_hidden and name.startswith("."))

    tracked = git_tracked(root, suffixes, untracked)
    found = []
    if tracked is not None:
        for path in tracked:
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            if all(keep_dir(part) for part in rel.split("/")[:-1]) and (accept is None or accept(rel)):
                found.append(path)
        return sorted(found)
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if keep_dir(d) and not os.path.exists(os.path.join(base, d, ".git"))]
        for name in files:
            if name.endswith(suffixes):
                path = os.path.join(base, name)
                if accept is None or accept(os.path.relpath(path, root).replace(os.sep, "/")):
                    found.append(path)
    return sorted(found)


def tree_walk(root, suffixes, skip_dirs=(), skip_hidden=False, accept=None, untracked=False):
    """`os.walk(root)` over `tree_files`: yields `(dirpath, dirnames, filenames)` for `root` and every directory
    below it that holds a selected file, top-down and sorted. `filenames` are the selected files only; `dirnames`
    are the subdirectories that lead to one. For a checker whose rule looks at a directory as a whole (a module
    directory holding `__manifest__.py`). Changing `dirnames` in place does not prune anything: pass `skip_dirs`
    or `accept` instead."""
    root = os.path.normpath(root)
    files_in, dirs_in = {}, {}
    for path in tree_files(root, suffixes, skip_dirs, skip_hidden, accept, untracked):
        here = os.path.dirname(path)
        files_in.setdefault(here, []).append(os.path.basename(path))
        while here != root and here.startswith(root + os.sep):
            parent = os.path.dirname(here)
            dirs_in.setdefault(parent, set()).add(os.path.basename(here))
            here = parent

    def walk(base):
        subdirs = sorted(dirs_in.get(base, ()))
        yield base, subdirs, sorted(files_in.get(base, ()))
        for name in subdirs:
            yield from walk(os.path.join(base, name))

    if files_in:
        yield from walk(root)
