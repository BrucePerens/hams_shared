#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Staged File Checker
-------------------
Refuses a commit that stages a file larger than MAX_BYTES or any file under a `target/` directory (cargo build output).
About 1.5 GB of `target/` was once committed in a worktree where the pre-commit size check was silently skipped;
git-hooks/pre-commit now fails closed when it cannot find its checkers, and this is one of them.

Only files being added or modified are judged, by the size of the staged blob, so removing an old large file is allowed.
A deliberate large file is named in LARGE_FILES_ALLOWED (exact path) with the reason beside it; nothing else is exempt.

Not converted to check_target.py, deliberately: this is a git hook with no target directory, and an index with nothing staged is the
normal case (exit 0). It already fails loudly when git itself fails (`check=True`: a traceback and a non-zero exit, not a pass).
"""

import subprocess
import sys

MAX_BYTES = 5 * 1024 * 1024
BUILD_DIRECTORIES = ("target",)
# [@ANCHOR: tools:large_files_allowed]
# Exact paths allowed over MAX_BYTES. The pinned Opus source archive (5.3 MB, GitHub's zip of commit 940d4e5, the bytes
# rade_c/cmake/BuildOpus.cmake checks by SHA-256) is committed so relay builds need no download: Bruce, 2026-11-07, so
# cloud sessions (whose GitHub gate refuses archives) can build the relay.
LARGE_FILES_ALLOWED = frozenset({
    "daemons/hams_local_relay/rade_c/third_party/opus-940d4e5af64351ca8ba8390df3f555484c567fbb.zip",
})


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


def gitlink_replacements():
    """Paths whose staged change turns a submodule pointer (mode 160000) into something else, typically a symlink made by
    fix_worktree_symlinks.py for a worktree (a hams_open PR once replaced the hams_shared entry that way)."""
    raw = subprocess.run(
        ["git", "diff", "--cached", "--raw", "--no-abbrev", "--diff-filter=T", "-z"],
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8", "surrogateescape")
    fields = raw.split("\0")
    return [fields[i + 1] for i in range(0, len(fields) - 1, 2)
            if fields[i].startswith(":") and fields[i].split()[0] == ":160000" and fields[i].split()[1] != "160000"]


def violations(entries):
    problems = []
    for path, size in entries:
        parts = path.split("/")
        if any(part in BUILD_DIRECTORIES for part in parts[:-1]):
            problems.append(f"{path}: build output ({'/'.join(BUILD_DIRECTORIES)}/ is never committed)")
        elif size > MAX_BYTES and path not in LARGE_FILES_ALLOWED:
            problems.append(f"{path}: {size / 1048576:.1f} MB is over the {MAX_BYTES // 1048576} MB limit")
    return problems


def main():
    problems = violations(staged_entries())
    problems += [f"{path}: replaces a submodule pointer (gitlink) with a plain entry or symlink; unstage it (git restore --staged {path})"
                 for path in gitlink_replacements()]
    if not problems:
        return 0
    print("pre-commit: refusing this commit:", file=sys.stderr)
    for problem in problems:
        print(f"  {problem}", file=sys.stderr)
    print("A deliberate exception: git commit --no-verify, with the reason in the message.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
