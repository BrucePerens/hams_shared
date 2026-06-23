#!/usr/bin/env python3
"""
gemini_startup.py
Generates a highly-focused initialization payload for Gemini sessions.
Instructs the AI to fetch static docs while only embedding uncommitted WIP files,
resulting in a lean, extremely actionable starting prompt.
"""

import os
import subprocess
import argparse

IGNORE_DIRS = {".git", "__pycache__", "node_modules", "venv", ".venv", "env"}
IGNORE_EXTENSIONS = {
    ".pyc",
    ".pyo",
    ".so",
    ".dll",
    ".exe",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".zip",
    ".tar",
    ".gz",
    ".pdf",
    ".sqlite3",
}


def get_git_root():
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return os.path.abspath(".")


def get_github_info(root_dir):
    """
    Determines the GitHub username, email, and repository name from git config.
    """
    try:
        github_user = subprocess.run(
            ["git", "config", "user.name"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except subprocess.CalledProcessError:
        github_user = "UnknownUser"

    try:
        github_email = subprocess.run(
            ["git", "config", "user.email"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except subprocess.CalledProcessError:
        github_email = "unknown@example.com"

    repo_name = os.path.basename(root_dir)
    return github_user, github_email, repo_name


def get_uncommitted_files():
    """Returns a list of modified, added, or untracked files."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True, check=True
        )
        files = []
        for line in result.stdout.splitlines():
            if len(line) > 3:
                # Extract filepath starting from the 4th character
                filepath = line[3:]
                if filepath.startswith('"') and filepath.endswith('"'):
                    filepath = filepath[1:-1]
                files.append(filepath)
        return files
    except subprocess.CalledProcessError:
        print("[!] Warning: Not a git repository or git error.")
        return []


def is_text_file(filepath):
    ext = os.path.splitext(filepath)[1].lower()
    return ext not in IGNORE_EXTENSIONS


def generate_payload(modules=None):
    if modules is None:
        modules = []

    root_dir = get_git_root()
    os.chdir(root_dir)

    github_user, github_email, repo_name = get_github_info(root_dir)

    uncommitted = get_uncommitted_files()

    # We now rely on the AI fetching AGENTS.md and docs/ dynamically,
    # unless they are actively being modified (uncommitted).
    target_files = set()

    for f in uncommitted:
        if os.path.exists(f) and os.path.isfile(f):
            # Exclude files inside ignored directories
            if not any(ignored in f.split("/") for ignored in IGNORE_DIRS):
                target_files.add(f)

    # Final safety check: ensure file still exists on disk
    target_files = {f for f in target_files if os.path.exists(f) and os.path.isfile(f)}

    output_dir = os.path.expanduser("~/tmp")
    os.makedirs(output_dir, exist_ok=True)
    output_filename = os.path.join(output_dir, "gemini_startup_payload.txt")

    with open(output_filename, "w", encoding="utf-8") as out:
        out.write("SYSTEM DIRECTIVE: INITIALIZATION PAYLOAD\n")
        out.write(f"Repository Target: {github_user}/{repo_name}\n")
        out.write(f"Author Email: {github_email}\n\n")

        out.write("--- CONTEXT INSTRUCTIONS ---\n")
        out.write("1. You have read-only access to my GitHub repository via your extensions.\n")
        out.write("2. The files explicitly dumped below represent my CURRENT UNPUSHED WORK-IN-PROGRESS.\n")
        out.write("   You MUST prioritize the contents of this payload over the historical state on GitHub.\n")
        out.write("3. MANDATORY READING: You MUST use your file fetcher or GitHub integration to READ `AGENTS.md`\n")
        out.write("   and `docs/LLM_GENERAL_REQUIREMENTS.md` BEFORE taking any other action. These contain your strict architectural mandates.\n")
        out.write("4. Code Generation: You MUST continue using the MIME-like Parcel transport schema defined in\n")
        out.write("   `docs/LLM_PARCEL_FORMAT.md` to modify my local files.\n")

        if modules:
            out.write("5. MODULE LOADING MANDATE: Use your @GitHub integration to explicitly fetch and load the following modules/paths before proceeding:\n")
            for mod in modules:
                out.write(f"   - {mod}\n")

        out.write("----------------------------\n\n")

        if target_files:
            for filepath in sorted(target_files):
                if not is_text_file(filepath):
                    continue

                try:
                    with open(filepath, "r", encoding="utf-8") as infile:
                        content = infile.read()

                    out.write(f"File: {filepath}\n")
                    out.write("=" * 80 + "\n")
                    out.write(content)
                    if not content.endswith("\n"):
                        out.write("\n")
                    out.write("=" * 80 + "\n\n")
                except UnicodeDecodeError:
                    # Gracefully skip binaries disguised without extensions
                    pass
                except Exception as e:
                    print(f"[!] Error reading {filepath}: {e}")
        else:
            out.write("[No uncommitted text files found. Rely on GitHub state.]\n\n")

        out.write("--- IMMEDIATE DIRECTIVE ---\n")
        out.write("Acknowledge receipt of these instructions. Confirm you have read the mandatory files ")
        out.write("(`AGENTS.md`, `docs/LLM_GENERAL_REQUIREMENTS.md`), and ask me what specific task, feature, or bug I would like to work on first.\n")

    print(f"\n[+] Successfully generated {output_filename}")
    print(f"    Included: {len(target_files)} uncommitted files.")
    if modules:
        print(f"    Requested GitHub Modules: {', '.join(modules)}")
    print("[*] Upload this file to Gemini to begin the session.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generates a highly-focused initialization payload for Gemini sessions."
    )
    parser.add_argument(
        "modules",
        nargs="*",
        help="List of modules/directories to instruct Gemini to load via @GitHub.",
    )
    args = parser.parse_args()

    generate_payload(args.modules)
