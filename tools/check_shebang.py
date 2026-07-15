#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Checks all source files to ensure that a shebang (#!), if present,
is only on the very first line of the file.
"""

import os
import sys

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
        "target",
        "radae",
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
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    for i, line in enumerate(f, 1):
                        if line.startswith("#!") and i != 1:
                            violations.append(
                                f"{os.path.relpath(file_path, repo_dir)}:{i} Contains shebang `#!` on a line other than line 1"
                            )
            except UnicodeDecodeError as e:
                # Skip binary files or files with weird encodings
                print(f"Warning: UnicodeDecodeError reading {file_path}: {e}")

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
