#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).

import ast
import os


def extract_docs(filepath):
    with open(filepath, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=filepath)

    docs = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            val = node.value.value
            if val.startswith("!"):
                docs.append(val[1:].strip())
    return docs


if __name__ == "__main__":
    source_file = os.path.join(os.path.dirname(__file__), "check_burn_list.py")
    output_file = os.path.join(os.path.dirname(__file__), "linter_rules.md")

    docs = extract_docs(source_file)
    with open(output_file, "w", encoding="utf-8") as f:
        f.write("# Linter Rules (Burn List)\n\n")
        for doc in docs:
            f.write(f"- {doc}\n")
    print(f"Extracted {len(docs)} literate documentation strings to {output_file}")
