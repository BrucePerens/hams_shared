#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Staged File Checker
-------------------
Refuses a commit that stages a file larger than MAX_BYTES or any file under a `target/` directory (cargo build output).
About 1.5 GB of `target/` was once committed in a worktree where the pre-commit size check was silently skipped;
git-hooks/pre-commit now fails closed when it cannot find its checkers, and this is one of them.

Only files being added or modified are judged, by the size of the staged blob, so removing an old large file is allowed.
A deliberate large file: `git commit --no-verify`, and say why in the commit message.
"""

import subprocess
import sys

MAX_BYTES = 5 * 1024 * 1024
BUILD_DIRECTORIES = ("target",)


def staged_entries():
    """(path, size) of every added, copied, modified or renamed file in the index. A submodule pointer (mode 160000) is a commit,
    not a file: it has no size and is skipped, so a bump of hams_shared is not refused."""
    raw = subprocess.run(
        ["git", "diff", "--cached", "--raw", "--no-abbrev", "--diff-filter=ACMR", "-z"],
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8", "surrogateescape")
    fields = raw.split("\0")
    entries = []
    i = 0
    while i + 1 < len(fields) and fields[i].startswith(":"):
        meta = fields[i].split()  # :old_mode new_mode old_sha new_sha status
        status = meta[4] if len(meta) > 4 else ""
        paths = 2 if status[:1] in ("R", "C") else 1
        path = fields[i + paths]
        i += 1 + paths
        if meta[1] == "160000":
            continue
        size = subprocess.run(
            ["git", "cat-file", "-s", f":{path}"], capture_output=True, text=True, check=True
        ).stdout.strip()
        entries.append((path, int(size)))
    return entries


def violations(entries):
    problems = []
    for path, size in entries:
        parts = path.split("/")
        if any(part in BUILD_DIRECTORIES for part in parts[:-1]):
            problems.append(f"{path}: build output ({'/'.join(BUILD_DIRECTORIES)}/ is never committed)")
        elif size > MAX_BYTES:
            problems.append(f"{path}: {size / 1048576:.1f} MB is over the {MAX_BYTES // 1048576} MB limit")
    return problems


def main():
    problems = violations(staged_entries())
    if not problems:
        return 0
    print("pre-commit: refusing this commit:", file=sys.stderr)
    for problem in problems:
        print(f"  {problem}", file=sys.stderr)
    print("A deliberate exception: git commit --no-verify, with the reason in the message.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
