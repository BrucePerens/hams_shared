#!/usr/bin/env python3
"""
Checks all python files to ensure they don't manually spawn pika connections,
but instead use the hams_rabbitmq.pool global connection.
"""

import os
import sys
import re

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
    }
    
    # Pattern to look for direct pika connection instantiation
    pattern = re.compile(r"pika\.(BlockingConnection|SelectConnection)")

    for root, dirs, files in os.walk(repo_dir):
        # Modify dirs in-place to ignore specified directories
        dirs[:] = [d for d in dirs if d not in ignore_dirs]

        for file in files:
            if not file.endswith(".py"):
                continue
            
            file_path = os.path.join(root, file)
            # Skip the rabbitmq pool itself
            if "rabbitmq_pool.py" in file_path:
                continue

            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    for i, line in enumerate(f, 1):
                        if pattern.search(line):
                            violations.append(
                                f"{os.path.relpath(file_path, repo_dir)}:{i} Instantiates pika connection directly. Use env['hams_rabbitmq.pool'] instead."
                            )
            except UnicodeDecodeError:
                pass

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
