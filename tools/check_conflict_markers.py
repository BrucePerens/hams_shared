#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Fails when a tracked text file still holds a git merge-conflict block: a line starting with `<<<<<<< `
or `>>>>>>> `, or a `=======` line between those two. A `=======` line outside such a block (a Markdown or
RST heading underline) is not a conflict and passes.
"""

import os
import subprocess
import sys
import check_target

CONFLICT_OPEN = "<<<<<<< "
CONFLICT_CLOSE = ">>>>>>> "
CONFLICT_SEPARATOR = "======="


def find_conflicts(lines):
    """(line number, marker line) for every conflict marker in `lines`."""
    found = []
    inside_block = False
    for number, line in enumerate(lines, 1):
        text = line.rstrip("\r\n")
        if text.startswith(CONFLICT_OPEN):
            inside_block = True
            found.append((number, text))
        elif text.startswith(CONFLICT_CLOSE):
            inside_block = False
            found.append((number, text))
        elif inside_block and text == CONFLICT_SEPARATOR:
            found.append((number, text))
    return found


def check_conflict_markers(repo_dir, stats=None):
    listed = subprocess.run(["git", "-C", repo_dir, "ls-files", "-z"], capture_output=True, check=False)
    if listed.returncode != 0:
        print(f"check_conflict_markers: git ls-files failed in {repo_dir}: {listed.stderr.decode(errors='replace').strip()}")
        sys.exit(2)

    violations = []
    for rel in listed.stdout.decode("utf-8", "surrogateescape").split("\0"):
        path = os.path.join(repo_dir, rel)
        # A tracked symlink holds a link, not text; a tracked path deleted in this checkout has nothing to read.
        if not rel or os.path.islink(path) or not os.path.isfile(path):
            continue
        if stats is not None:
            stats["files"] = stats.get("files", 0) + 1
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except UnicodeDecodeError:
            # Binary or non-UTF-8 content is not a text file with conflict markers.
            continue
        for number, marker in find_conflicts(lines):
            violations.append(f"{rel}:{number} {marker}")
    return violations


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: check_conflict_markers.py <repo_dir>")
        sys.exit(1)

    repo_dir = check_target.require_directory(sys.argv[1], "check_conflict_markers")
    stats = {}
    violations = check_conflict_markers(repo_dir, stats)
    check_target.require_scanned(stats.get("files", 0), repo_dir, "check_conflict_markers", "tracked text files")

    if violations:
        print("Conflict markers left in tracked files:")
        for v in violations:
            print(f"  - {v}")
        sys.exit(1)

    sys.exit(0)
