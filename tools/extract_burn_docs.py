#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

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


_EMPTY_MARKER = "# Linter Rules (Burn List)\n\n"


def write_docs(docs, output_file):
    """Writes `docs` as a markdown bullet list to `output_file`.

    Real bug found 2026-09-12, confirmed directly against this repo's own real, currently
    checked-in `linter_rules.md`: `check_burn_list.py` no longer contains any "!"-prefixed
    literate-doc string this extractor looks for (confirmed empirically: `extract_docs` returns
    an empty list against the real, current `check_burn_list.py`) -- that convention was
    abandoned at some point in favor of hand-maintaining `linter_rules.md` directly (its own real
    git history has 30+ commits, the most recent from this same bug-hunt campaign, describing a
    rich reference document with headers/tables/code blocks that bears no resemblance to what
    this script's own bullet-list format could ever produce). Running this script for real today
    would silently overwrite that real, valuable, actively-maintained file with an almost-empty
    one (just the header) -- with a misleading "Extracted 0 ..." message that looks like a normal
    successful run, giving no indication anything destructive just happened. Refuses to overwrite
    when doing so would be destructive: `docs` is empty AND the existing output file already has
    real content beyond the trivial empty-result skeleton this script itself would write for a
    genuinely empty source. A fresh/synthetic scenario with no real docs and no pre-existing
    output file (or one already at the empty skeleton) is unaffected -- this only blocks the
    specific "would silently erase real content" case.
    """
    if not docs and os.path.exists(output_file):
        with open(output_file, "r", encoding="utf-8") as f:
            existing = f.read()
        if existing != _EMPTY_MARKER:
            raise SystemExit(
                f"Refusing to overwrite {output_file}: found zero '!'-prefixed literate doc "
                "strings in the source, but the existing output file already has real content "
                "beyond the empty-result skeleton. Either the source's own literate-doc "
                "convention has been abandoned (in which case this generator itself needs a "
                "real decision, not a silent destructive overwrite) or something is genuinely "
                "wrong with the source parse -- check by hand before proceeding."
            )
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(_EMPTY_MARKER)
        for doc in docs:
            f.write(f"- {doc}\n")


if __name__ == "__main__":
    source_file = os.path.join(os.path.dirname(__file__), "check_burn_list.py")
    output_file = os.path.join(os.path.dirname(__file__), "linter_rules.md")

    docs = extract_docs(source_file)
    write_docs(docs, output_file)
    print(f"Extracted {len(docs)} literate documentation strings to {output_file}")
