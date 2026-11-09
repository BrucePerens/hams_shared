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


def git_tracked(root, suffixes):
    """Absolute paths of the files git tracks under `root` (submodules included) whose names end in one of
    `suffixes`, or None when `root` is not inside a git work tree."""
    cmd = ["git", "-C", root, "ls-files", "-z", "--cached", "--recurse-submodules", "--"]
    cmd += ["*" + s for s in suffixes]
    try:
        done = subprocess.run(cmd, capture_output=True, check=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    return [os.path.join(root, rel) for rel in done.stdout.decode("utf-8", "replace").split("\0") if rel]


def tree_files(root, suffixes, skip_dirs=(), skip_hidden=False, accept=None):
    """Sorted absolute paths of the files under `root` that belong to its own tree.

    `suffixes`: file-name endings to keep (a string or a tuple). `skip_dirs`: directory names never entered.
    `skip_hidden`: also skip every directory whose name starts with a dot. `accept(rel_path)`: an optional extra
    filter on the path relative to `root` ('/'-separated), for rules a suffix cannot express."""
    suffixes = (suffixes,) if isinstance(suffixes, str) else tuple(suffixes)
    skip_dirs = set(skip_dirs)

    def keep_dir(name):
        return name not in skip_dirs and not (skip_hidden and name.startswith("."))

    tracked = git_tracked(root, suffixes)
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
