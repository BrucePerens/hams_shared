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
        # Get list of modified files against HEAD
        res = subprocess.run(["git", "diff", "--name-only", "HEAD"], capture_output=True, text=True, check=True)
        modified_files = [f for f in res.stdout.strip().split("\n") if f]
    except subprocess.CalledProcessError:
        # Not in a git repo or no HEAD yet
        sys.exit(0)
    
    violation = False
    
    for filepath in modified_files:
        if not os.path.exists(filepath):
            continue  # file was deleted, that's fine (or at least handled by other PR reviews)
        
        # We specifically care about prompt files, python files, and docs
        if not (filepath.endswith(".md") or filepath.endswith(".py") or filepath.endswith(".json")):
            continue

        try:
            old_size_res = subprocess.run(["git", "cat-file", "-s", f"HEAD:{filepath}"], capture_output=True, text=True, check=True)
            old_size = int(old_size_res.stdout.strip())
        except subprocess.CalledProcessError:
            # File might be new (not in HEAD)
            continue
            
        new_size = os.path.getsize(filepath)
        
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
