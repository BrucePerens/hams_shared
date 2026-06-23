#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import ast
import datetime
from collections import defaultdict


def find_translatable_strings(root_dir):
    """
    Walks the repository and uses the AST to extract all strings wrapped in _()
    """
    translations = defaultdict(list)

    for dirpath, dirnames, filenames in os.walk(root_dir):
        # Exclude hidden directories, virtual environments, and node_modules
        dirnames[:] = [
            d
            for d in dirnames
            if not d.startswith(".") and d not in ("__pycache__", "node_modules")
        ]

        for filename in filenames:
            if filename.endswith(".py") and filename != "generate_pot.py":
                filepath = os.path.join(dirpath, filename)
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        content = f.read()

                    # Parse the Python file into an Abstract Syntax Tree
                    tree = ast.parse(content, filename=filepath)

                    # Walk the AST looking for function calls
                    for node in ast.walk(tree):
                        if isinstance(node, ast.Call):
                            # Check if the function being called is named '_'
                            if getattr(node.func, "id", None) == "_":
                                # Ensure it has at least one argument and that argument is a static string
                                if (
                                    node.args
                                    and isinstance(node.args[0], ast.Constant)
                                    and isinstance(node.args[0].value, str)
                                ):
                                    msgid = node.args[0].value

                                    # Convert absolute path to a clean relative path for the PO file comment
                                    rel_path = os.path.relpath(filepath, root_dir)
                                    translations[msgid].append((rel_path, node.lineno))

                except SyntaxError as e:
                    print(f"[WARN] Syntax error, skipping {filepath}: {e}")
                except Exception as e:
                    print(f"[WARN] Could not parse {filepath}: {e}")

    return translations


def generate_pot(translations, output_path):
    """
    Writes the extracted strings into a standard GNU gettext Portable Object Template (.pot)
    """
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M+0000")

    with open(output_path, "w", encoding="utf-8") as f:
        # Write PO Header
        f.write("# Translation of Hams.com Modules.\n")
        f.write('msgid ""\n')
        f.write('msgstr ""\n')
        f.write('"Project-Id-Version: Odoo Server 19.0\\n"\n')
        f.write('"Report-Msgid-Bugs-To: \\n"\n')
        f.write(f'"POT-Creation-Date: {now}\\n"\n')
        f.write(f'"PO-Revision-Date: {now}\\n"\n')
        f.write('"Last-Translator: \\n"\n')
        f.write('"Language-Team: \\n"\n')
        f.write('"MIME-Version: 1.0\\n"\n')
        f.write('"Content-Type: text/plain; charset=UTF-8\\n"\n')
        f.write('"Content-Transfer-Encoding: \\n"\n')
        f.write('"Plural-Forms: \\n"\n\n')

        # Write extracted strings
        for msgid, occurrences in sorted(translations.items()):
            # Write file location comments
            for filepath, lineno in sorted(occurrences):
                f.write(f"#: {filepath}:{lineno}\n")

            # Safely escape backslashes, quotes, and newlines for the PO format
            safe_msgid = (
                msgid.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
            )

            f.write(f'msgid "{safe_msgid}"\n')
            f.write('msgstr ""\n\n')


if __name__ == "__main__":
    # Define root directory (assuming tools/ is at the root)
    root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    output_pot = os.path.join(root_dir, "i18n", "hams_master.pot")

    print(f"[*] Scanning {root_dir} for _() strings via AST...")
    translations = find_translatable_strings(root_dir)
    print(f"[*] Found {len(translations)} unique translatable strings.")

    generate_pot(translations, output_pot)
    print(f"[*] Generated master POT file at: {output_pot}")
