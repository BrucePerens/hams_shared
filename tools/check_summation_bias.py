#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Summation Bias Checker
----------------------
Detects if a file's size has been reduced by more than 5% compared to HEAD.
This helps prevent AI agents from summarizing and dropping critical nuance during edits.
"""

import os
import subprocess
import sys

def main():
    try:
        # `git rev-parse --show-toplevel` resolves correctly regardless of the CURRENT working
        # directory, as long as it's anywhere inside the repo -- unlike the naive relative-path
        # reads below, which is exactly the real bug this fixes (see comment at the
        # os.path.exists/os.path.getsize call sites).
        repo_root_res = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
        )
        repo_root = repo_root_res.stdout.strip()

        # Get list of modified files against HEAD
        res = subprocess.run(["git", "diff", "--name-only", "HEAD"], capture_output=True, text=True, check=True)
        modified_files = [f for f in res.stdout.strip().split("\n") if f]
    except subprocess.CalledProcessError:
        # Not in a git repo or no HEAD yet
        sys.exit(0)

    violation = False

    for filepath in modified_files:
        # Real bug found 2026-09-10: `git diff --name-only` always reports paths relative to
        # the REPO ROOT, regardless of the process's own current working directory -- but
        # os.path.exists/os.path.getsize resolve a relative path against the CURRENT working
        # directory instead. Running this script from anywhere other than the exact repo root
        # (any subdirectory -- entirely realistic: nothing about how this script is invoked
        # pins the caller's cwd to the repo root) made every real, non-deleted, modified file
        # resolve to a nonexistent path from that subdirectory's own point of view, silently
        # hitting the "file was deleted" branch below for EVERY file and reporting zero
        # violations -- completely defeating this exact anti-summation-bias check, with no
        # warning, on a script whose whole purpose is guarding against silent content loss.
        abs_filepath = os.path.join(repo_root, filepath)
        if not os.path.exists(abs_filepath):
            continue  # file was deleted, that's fine (or at least handled by other PR reviews)

        # We specifically care about prompt files, python files, and docs
        if not (filepath.endswith(".md") or filepath.endswith(".py") or filepath.endswith(".json")):
            continue

        try:
            old_size_res = subprocess.run(
                ["git", "cat-file", "-s", f"HEAD:{filepath}"],
                capture_output=True,
                text=True,
                check=True,
                cwd=repo_root,
            )
            old_size = int(old_size_res.stdout.strip())
        except subprocess.CalledProcessError:
            # File might be new (not in HEAD)
            continue

        new_size = os.path.getsize(abs_filepath)
        
        if old_size > 0 and new_size < old_size:
            reduction_ratio = (old_size - new_size) / old_size
            if reduction_ratio > 0.05:
                print(f"❌ SUMMATION BIAS DETECTED in {filepath}")
                print(f"   - File size reduced by {reduction_ratio:.1%} (from {old_size} to {new_size} bytes).")
                print("   - CRITICAL REQUIREMENT: Did you summarize or drop critical rules/nuance during your edit?")
                print("   - If this deletion was intentional, you MUST manually bypass or ignore this error,")
                print("     AND you MUST explicitly explain the reduction in your response to assure the User")
                print("     and the Reviewer that it was not summation bias.")
                violation = True

    if violation:
        sys.exit(1)
    
if __name__ == "__main__":
    main()
