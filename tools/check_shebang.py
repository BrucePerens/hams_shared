#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Checks all source files to ensure that a shebang (#!), if present,
is only on the very first line of the file.
"""

import os
import re
import sys

_HEREDOC_OPEN = re.compile(r"<<-?\s*(?:'([^']+)'|\"([^\"]+)\"|\\?([A-Za-z_][A-Za-z0-9_]*))")


def check_shebang(repo_dir):
    violations = []

    # Common directories to ignore
    ignore_dirs = {
        ".git",
        "node_modules",
        "venv",
        "env",
        ".venv",
        "__pycache__",
        ".agents",
        # Real bug found 2026-09-12: this project's own standing convention runs concurrent
        # bug-hunt dispatches in isolated git worktrees under .claude/worktrees/<session>/
        # inside the repo root -- a real git worktree checkout, not an empty directory --
        # which this set never excluded.
        ".claude",
        "target",
        "radae",
        # Vendored third-party source (cargo vendor, opus, prometheus/procfs's ttar, ...): not ours to edit, and
        # several of those files legitimately carry a shebang inside a generated here-document.
        "vendor",
        # Matches any virtualenv by its structurally-guaranteed lib/pythonX.Y/site-packages/
        # subdirectory regardless of the venv's own outer directory name -- "venv"/".venv" alone
        # missed a real stray "whisper_venv/" (see check_float_isfinite.py's own comment/test).
        "site-packages",
    }
    # Only check script files
    valid_exts = {
        ".py",
        ".sh",
    }

    for root, dirs, files in os.walk(repo_dir):
        # Modify dirs in-place to ignore specified directories
        dirs[:] = [d for d in dirs if d not in ignore_dirs]

        for file in files:
            ext = os.path.splitext(file)[1]
            if ext not in valid_exts and "." in file:
                continue

            file_path = os.path.join(root, file)
            # A dangling symlink (recorded sysfs trees such as bench_mcp's usb_fixtures hold
            # `driver` links into the machine they were recorded on) has no content to scan.
            if os.path.islink(file_path) and not os.path.exists(file_path):
                continue
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    heredoc_end = None
                    for i, line in enumerate(f, 1):
                        # A shell script that writes another script out with a here-document carries that
                        # script's own `#!` line inside the body; that is data, not a misplaced shebang.
                        if heredoc_end is not None:
                            if line.strip() == heredoc_end:
                                heredoc_end = None
                            continue
                        if ext != ".py" and not line.lstrip().startswith("#"):
                            opener = _HEREDOC_OPEN.search(line)
                            if opener:
                                heredoc_end = next(g for g in opener.groups() if g)
                                continue
                        if line.startswith("#!") and i != 1:
                            violations.append(
                                f"{os.path.relpath(file_path, repo_dir)}:{i} Contains shebang `#!` on a line other than line 1"
                            )
            except UnicodeDecodeError:
                # Binary content (a recorded USB `descriptors` blob): not a script, so it
                # cannot carry a misplaced shebang. Not a warning.
                continue
            except OSError as e:
                # Skip binary files or files with weird encodings (UnicodeDecodeError), AND any
                # other realistic filesystem condition open() can raise for a name os.walk()
                # already handed us as a "file" -- most concretely a broken symlink
                # (FileNotFoundError), a real, currently-reachable state in this exact repo
                # (see check_absolute_paths.py's own identical fix/comment for the concrete
                # AGENTS.md-in-a-fresh-worktree scenario this guards against). Before this fix,
                # a single broken symlink to a .py/.sh file crashed the ENTIRE scan.
                print(f"Warning: could not read {file_path}: {e}")

    return violations

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: check_shebang.py <repo_dir>")
        sys.exit(1)

    repo_dir = sys.argv[1]
    violations = check_shebang(repo_dir)

    if violations:
        print("❌ Shebang Violations:")
        for v in violations:
            print(f"  - {v}")
        sys.exit(1)

    sys.exit(0)
