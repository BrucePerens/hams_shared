#!/usr/bin/env python3
import os
import re
import logging

_logger = logging.getLogger(__name__)

def migrate_dependencies(root_dir):
    """
    Recursively scans the repository to replace all standalone instances
    of 'zero_sudo' with 'zero_sudo' using strict word boundaries.
    """
    # Use word boundaries to prevent corrupting substrings (e.g., 'no_hams_test_here')
    pattern = re.compile(r'\bhams_test\b')

    # Target source files where dependencies, imports, and tours are defined
    target_exts = {'.py', '.js', '.xml', '.csv', '.html', '.md', '.txt', '.yaml', '.json'}

    total_files_updated = 0
    total_replacements = 0

    for dirpath, dirnames, filenames in os.walk(root_dir):
        # Ignore version control and environment directories
        dirnames[:] = [d for d in dirnames if not d.startswith('.') and d != '__pycache__']

        for filename in filenames:
            ext = os.path.splitext(filename)[1]
            if ext in target_exts:
                filepath = os.path.join(dirpath, filename)

                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        content = f.read()

                    new_content, num_subs = pattern.subn('zero_sudo', content)

                    if num_subs > 0:
                        with open(filepath, 'w', encoding='utf-8') as f:
                            f.write(new_content)
                        print(f"[UPDATED] {filepath} ({num_subs} replacements)")
                        total_files_updated += 1
                        total_replacements += num_subs
                except UnicodeDecodeError as e:
                    _logger.warning("Skipped binary or non-utf8 file %s: %s", filepath, e)
                except Exception as e: # audit-ignore-catch-all
                   _logger.error(f"[ERROR] Failed to process {filepath}: {e}")

    print("-" * 40)
    print("Migration Complete.")
    print(f"Files Modified: {total_files_updated}")
    print(f"Total Replacements: {total_replacements}")

if __name__ == '__main__':
    print("Starting dependency migration: 'zero_sudo' -> 'zero_sudo'...")
    # Execute from the current working directory (assumes execution from repo root)
    migrate_dependencies('.')
