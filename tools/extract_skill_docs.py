#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Literate Programming Extractor for Linter Compliance

Extracts markdown blocks embedded in `check_burn_list.py` as AST string literals
and writes them to the AI SKILL.md documentation file to ensure synchronization.
"""

import ast
import os
import sys
import textwrap


def main():
    source_file = os.path.join(os.path.dirname(__file__), "check_burn_list.py")
    target_file = os.path.join(
        os.path.dirname(__file__), "../agents/skills/linter-compliance/SKILL.md"
    )

    with open(source_file, "r", encoding="utf-8") as f:
        source_code = f.read()

    try:
        tree = ast.parse(source_code)
    except SyntaxError as e:
        print(f"Failed to parse {source_file}: {e}")
        sys.exit(1)

    extracted_blocks = []

    for node in ast.walk(tree):
        # In Python 3.8+, string literals inside Expr nodes are represented as Constant
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                text = node.value.value
                if text.startswith("!"):
                    # Remove the "markdown\n" prefix
                    extracted_text = textwrap.dedent(text[1:])
                    extracted_blocks.append((node.lineno, extracted_text))

    # Ensure blocks are appended in the order they appear in the source file
    extracted_blocks.sort(key=lambda x: x[0])

    if not extracted_blocks:
        print("No markdown blocks found.")
        sys.exit(1)

    # Reconstruct the document
    output_content = ""
    for lineno, text in extracted_blocks:
        output_content += text.strip() + "\n\n---\n\n"

    # Clean up the trailing delimiter
    if output_content.endswith("\n\n---\n\n"):
        output_content = output_content[:-7] + "\n"

    with open(target_file, "w", encoding="utf-8") as f:
        f.write(output_content)

    print(
        f"Successfully extracted {len(extracted_blocks)} literate documentation blocks to {target_file}"
    )


if __name__ == "__main__":
    main()
