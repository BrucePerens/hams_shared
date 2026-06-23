import os
import sys
import subprocess
import argparse
import multiprocessing
import re
import logging

def load_ignore_file(filepath):
    patterns = []
    if filepath and os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append(re.compile(line))
    return patterns

def is_ignored(path, patterns):
    for pat in patterns:
        if re.search(pat, path):
            return True
    return False

def check_file(file_path):
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            code = f.read()
    except Exception as e: # audit-ignore-catch-all
        logging.warning("Failed to read %s: %s", file_path, e)
        return None

    # --- Custom Odoo Architecture Checks ---
    if "extends Interaction" in code and "mountComponent(" in code:
        err_msg = (
            "🚨 [AUDIT] ARCHITECTURE TRAP: Do not manually call `mountComponent(`\n"
            "inside an Interaction class. This causes an Owl Registry Collision with\n"
            "the NotificationContainer. Let Odoo handle mounting natively via\n"
            "`static components` and data props.\n"
            "See docs/LLM_EXPERIENCE.md (Item 38) for details."
        )
        return file_path, err_msg

    # Using --input-type=module forces Node to natively parse ES6 Imports/Exports
    # without needing experimental VM modules or package.json overrides.
    res = subprocess.run(
        ['node', '--input-type=module', '--check'],
        input=code,
        capture_output=True,
        text=True
    )

    if res.returncode != 0:
        # Node reports stdin errors as '[stdin]:line'. Inject the real filename.
        err_msg = res.stderr.replace('[stdin]', os.path.basename(file_path))
        return file_path, err_msg
    return None

def main():
    parser = argparse.ArgumentParser(description="Check JS syntax")
    parser.add_argument("--ignore-file", help="Path to ignore list")
    parser.add_argument("directories", nargs="+", help="Directories to scan")
    args = parser.parse_args()

    ignore_patterns = load_ignore_file(args.ignore_file)
    js_files = []

    for d in args.directories:
        if not os.path.isdir(d):
            continue
        for root, dirs, files in os.walk(d):
            # Prune hidden folders and standard library directories to save time
            dirs[:] = [dir_name for dir_name in dirs if not dir_name.startswith('.') and dir_name not in ('node_modules', '__pycache__', 'lib', 'static/lib')]
            for f in files:
                if f.endswith('.js') and '.min.' not in f:
                    full_path = os.path.join(root, f)
                    rel_path = os.path.relpath(full_path, os.path.dirname(d))
                    if not is_ignored(rel_path, ignore_patterns):
                        js_files.append(full_path)

    if not js_files:
        print("[+] JS Syntax Linter: No JS files found in target modules.")
        return 0

    print(f"[*] JS Syntax Linter: Checking {len(js_files)} JS files...")
    errors = []

    # Constrain pool size to avoid overwhelming the VM CPU scheduler
    pool_size = min(4, multiprocessing.cpu_count() or 1)
    with multiprocessing.Pool(pool_size) as pool:
        for res in pool.imap_unordered(check_file, js_files):
            if res:
                errors.append(res)

    if errors:
        print("🚨 JAVASCRIPT SYNTAX ERRORS DETECTED 🚨\n")
        for file_path, stderr in errors:
            print(f"File: {file_path}\n{stderr.strip()}\n" + "-" * 60)
        return 1

    print(f"[+] JS Syntax Linter: All {len(js_files)} files passed.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
