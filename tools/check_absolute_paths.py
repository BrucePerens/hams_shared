#!/usr/bin/env python3
"""
Checks all source files for hard-coded absolute paths based in /home.
"""

import os
import sys

def check_absolute_paths(repo_dir):
    violations = []
    
    # Common directories to ignore
    ignore_dirs = {'.git', 'node_modules', 'venv', 'env', '.venv', '__pycache__', '.agents'}
    # Only check text-based files
    valid_exts = {'.py', '.js', '.xml', '.csv', '.md', '.json', '.txt', '.sh', '.html', '.css'}

    for root, dirs, files in os.walk(repo_dir):
        # Modify dirs in-place to ignore specified directories
        dirs[:] = [d for d in dirs if d not in ignore_dirs]
        
        for file in files:
            ext = os.path.splitext(file)[1]
            if ext not in valid_exts and file != 'Makefile':
                continue
            
            file_path = os.path.join(root, file)
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    for i, line in enumerate(f, 1):
                        if '/home' + '/' in line:
                            # Skip if it is a file URI which may be in SKILL.md examples or similar
                            # Actually, we should even prohibit the h-o-m-e path because it's non-portable
                            violations.append(f"{os.path.relpath(file_path, repo_dir)}:{i} Contains hardcoded home path")
            except UnicodeDecodeError:
                # Skip binary files or files with weird encodings
                pass

    return violations

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: check_absolute_paths.py <repo_dir>")
        sys.exit(1)
        
    repo_dir = sys.argv[1]
    violations = check_absolute_paths(repo_dir)
    
    if violations:
        print("❌ Absolute Paths Violations:")
        for v in violations:
            print(f"  - {v}")
        sys.exit(1)
    
    sys.exit(0)
