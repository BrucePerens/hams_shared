#!/usr/bin/env python3
import os

def fix_test_imports():
    """
    Recursively scans and repairs test dependencies across all modules,
    ensuring UI tours and Python tests point to the newly centralized zero_sudo facility.
    """
    exts = ('.py', '.js', '.xml', '.csv')
    updated_count = 0

    for root, dirs, files in os.walk('.'):
        # Ignore version control and environment directories
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('__pycache__', 'venv', 'node_modules')]

        for file in files:
            if not file.endswith(exts):
                continue

            filepath = os.path.join(root, file)

            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    content = f.read()

                new_content = content

                # 1. Global Cross-Module Replacements
                new_content = new_content.replace('odoo.addons.hams_test.tests.common', 'odoo.addons.zero_sudo.tests.common')
                new_content = new_content.replace('odoo.addons.hams_test.tests.real_transaction', 'odoo.addons.zero_sudo.tests.real_transaction')
                new_content = new_content.replace('@hams_test/js/tour_utils', '@zero_sudo/js/tour_utils')
                new_content = new_content.replace('hams_test.action_noisy_table', 'zero_sudo.action_noisy_table')
                new_content = new_content.replace('hams_test.tests.real_transaction.RealTransactionCase', 'zero_sudo.tests.real_transaction.RealTransactionCase')

                # String literal fallbacks for skip messages and logs
                new_content = new_content.replace("'hams_test'", "'zero_sudo'")
                new_content = new_content.replace('"hams_test"', '"zero_sudo"')

                # 2. Internal Loader Fixes (Prevent ModuleNotFoundError during discovery)
                if 'zero_sudo/tests/' in filepath and filepath.endswith('.py'):
                    new_content = new_content.replace('from odoo.addons.zero_sudo.tests.common import', 'from .common import')
                    new_content = new_content.replace('from odoo.addons.zero_sudo.tests.real_transaction import', 'from .real_transaction import')

                if new_content != content:
                    with open(filepath, 'w', encoding='utf-8') as f:
                        f.write(new_content)
                    print(f"[REPAIRED] {filepath}")
                    updated_count += 1

            except UnicodeDecodeError:
                pass
            except OSError as e:
                print(f"[ERROR] Failed to process {filepath}: {e}")

    print("-" * 40)
    print(f"Migration Finalization Complete. Repaired {updated_count} files.")

if __name__ == '__main__':
    print("Starting final test import and dependency migration...")
    fix_test_imports()
