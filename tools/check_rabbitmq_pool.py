#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Checks all python files to ensure they don't manually spawn pika connections,
but instead use the hams_rabbitmq.pool global connection.
"""

import os
import sys
import re
import io
import tokenize
from tree_files import tree_walk

def _code_hits(text, pattern):
    """1-based line numbers where `pika.BlockingConnection` / `pika.SelectConnection` appears in CODE.

    Only NAME/OP tokens count, so the same text inside a comment or a string literal (for example a
    mock.patch("...generalized_monitor.pika.BlockingConnection") target, or a sentence in a comment)
    is not an instantiation and is not reported. A file that cannot be tokenized falls back to the
    plain per-line regex, so a broken file is never silently skipped.
    """
    try:
        toks = [
            t
            for t in tokenize.generate_tokens(io.StringIO(text).readline)
            if t.type in (tokenize.NAME, tokenize.OP)
        ]
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return [i for i, line in enumerate(text.splitlines(), 1) if pattern.search(line)]
    hits = []
    for a, b, c in zip(toks, toks[1:], toks[2:]):
        if a.string == "pika" and b.string == "." and c.string in ("BlockingConnection", "SelectConnection"):
            hits.append(a.start[0])
    return hits


def check_rabbitmq(repo_dir):
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
        "target",
        "radae",
        "daemon",
        "daemons",
        # ".claude" holds agent sessions' isolated git worktrees under
        # .claude/worktrees/<session>/ inside the repo root -- real git worktree checkouts, not
        # empty directories -- so it is excluded.
        ".claude",
        # Matches any virtualenv by its structurally-guaranteed lib/pythonX.Y/site-packages/
        # subdirectory regardless of the venv's own outer directory name -- "venv"/".venv" alone
        # missed a real stray "whisper_venv/" (see check_float_isfinite.py's own comment/test).
        "site-packages",
    }
    
    # Pattern to look for direct pika connection instantiation
    pattern = re.compile(r"pika\.(BlockingConnection|SelectConnection)")

    for root, _dirs, files in tree_walk(repo_dir, ".py", ignore_dirs, untracked=True):
        for file in files:
            if not file.endswith(".py"):
                continue
            
            file_path = os.path.join(root, file)
            # Skip the rabbitmq pool itself
            if "rabbitmq_pool.py" in file_path:
                continue

            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    text = f.read()
                lines = text.splitlines()
                for i in _code_hits(text, pattern):
                    if "# burn-ignore-pika" not in lines[i - 1]:
                        violations.append(
                            f"{os.path.relpath(file_path, repo_dir)}:{i} Instantiates pika connection directly. Use env['hams_rabbitmq.pool'] instead."
                        )
            except (UnicodeDecodeError, OSError) as e:
                # OSError is caught too: a broken symlink (target doesn't exist) with a
                # .py extension makes open() raise FileNotFoundError, which would crash the
                # entire scan on one unreadable file. Real, reachable repo state (see
                # check_absolute_paths.py's identical handling), not contrived.
                print(f"Warning: could not read {file_path}: {e}")

    return violations

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: check_rabbitmq_pool.py <repo_dir>")
        sys.exit(1)

    repo_dir = sys.argv[1]
    violations = check_rabbitmq(repo_dir)

    if violations:
        print("❌ RabbitMQ Pool Violations:")
        for v in violations:
            print(f"  - {v}")
        sys.exit(1)

    sys.exit(0)
