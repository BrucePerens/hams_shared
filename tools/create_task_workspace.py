#!/usr/bin/env python3
import os
import shutil
import argparse
from datetime import datetime


def create_workspace(dest_dir):
    """
    Generates an isolated task workspace containing only formal documentation,
    API contracts, and tooling to preserve LLM token context when building new features.
    """
    # Resolve to absolute path to safely copy outside the repo if requested
    dest_path = os.path.abspath(dest_dir)

    if not os.path.exists(dest_path):
        os.makedirs(dest_path)

    print(f"[*] Creating isolated task workspace at: {dest_path}")

    # Directories to copy explicitly
    dirs_to_copy = ["docs", "tools"]
    for target_dir in dirs_to_copy:
        src = target_dir
        dest = os.path.join(dest_path, target_dir)

        if os.path.exists(src):
            # dirs_exist_ok=True requires Python 3.8+
            shutil.copytree(src, dest, dirs_exist_ok=True)
            print(f"[+] Copied directory ({target_dir}/)")
        else:
            print(f"[!] Warning: {target_dir}/ directory not found in current path.")

    # Top-level context files
    top_level_files = ["AGENTS.md", "README.md", "docker-compose.yml"]
    for f in top_level_files:
        if os.path.exists(f):
            shutil.copy2(f, dest_path)
            print(f"[+] Copied {f}")

    # Exclude actual source code
    print("[*] Source code explicitly excluded to preserve LLM token context.")
    print("[*] Workspace generation complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate an isolated Task Workspace for LLM development."
    )
    # Default to placing it outside the current git tree to avoid accidental commits
    default_dest = f"../task_workspace_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    parser.add_argument(
        "--dest", default=default_dest, help="Destination directory for the workspace"
    )
    args = parser.parse_args()

    create_workspace(args.dest)
