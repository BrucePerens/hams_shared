#!/bin/bash
# Copyright © Bruce Perens K6BP. All Rights Reserved.

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "========================================================"
echo " 🎨 RUNNING BLACK FORMATTER"
echo "========================================================"

# Pre-process Python files to append '# fmt: skip' to our custom linter bypass tags.
# This instructs Black not to wrap these specific lines, ensuring the AST linter's
# line-number correlation remains perfectly intact.
echo "[*] Pinning linter avoidance comments..."
"$VENV_PYTHON" -c '
import os, sys
def pin_comments(d):
    for root, dirs, files in os.walk(d):
        dirs[:] = [dir_name for dir_name in dirs if not dir_name.startswith(".") and dir_name not in ("venv", "env", "node_modules", "__pycache__")]
        for f in files:
            if f.endswith(".py"):
                path = os.path.join(root, f)
                try:
                    with open(path, "r", encoding="utf-8") as file:
                        lines = file.readlines()
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).warning("An error occurred: %s", e)
                    continue
                changed = False
                for i, line in enumerate(lines):
                        if ("# burn-ignore" in line or "# audit-ignore" in line) and "fmt: skip" not in line:
                            lines[i] = line.rstrip() + "  # fmt: skip\n"
                            changed = True
                if changed:
                    with open(path, "w", encoding="utf-8") as file:
                        file.writelines(lines)
pin_comments(sys.argv[1])
' "$DIR"

# Execute Black formatting across the directory, skipping isolated environments
"$VENV_PYTHON" -m black "$DIR" --exclude "/(\.venv|venv|env|\.git|__pycache__|node_modules)/"

echo "✅ Formatting complete."
