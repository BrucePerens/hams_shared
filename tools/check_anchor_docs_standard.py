#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Anchor documentation standard gate (step 2 of night_shift_todo/low/document-every-anchor-completely, ADR 0113).

    python3 hams_shared/tools/check_anchor_docs_standard.py <repo_root>

Reads `tools/anchor_doc_standard.txt`, a list of paths relative to a repository root (one per line, `#` comments). For each
listed path that exists under `<repo_root>`:

  - a documentation file (`.md`, listed by name): every anchor's share of its entry must be at least `MIN_WORDS` words (no stubs);
  - a source file or a directory: every data structure in it (Rust struct/enum/type/union, Python or JavaScript class)
    must carry an anchor (`anchor_doc_inventory.find_data_structures`).

The list is the opt-in: it only grows, until it covers everything (ADR 0090: findings go to zero and stay at zero, no
baseline). A path that does not exist in this repository is skipped, so one list serves hams_com and hams_open. Add a
file or directory to the list in the same change that brings it up to the standard.
"""

import os
import sys

import check_target
from anchor_doc_inventory import DEFAULT_SHORT_WORDS, SOURCE_SUFFIXES, doc_entries, find_data_structures
from verify_anchors import _is_excluded_dir, _walk_tree_files

HERE = os.path.dirname(os.path.abspath(__file__))
LIST_FILE = os.path.join(HERE, "anchor_doc_standard.txt")
MIN_WORDS = DEFAULT_SHORT_WORDS


# [@ANCHOR: anchor_docs_standard:read_list]
def read_list(path=LIST_FILE):
    """The opt-in paths of the list file `path` (default `tools/anchor_doc_standard.txt`): one per line, blank lines and
    `#` comments dropped, a trailing `/` removed. A missing file gives `[]`."""
    try:
        with open(path, encoding="utf-8") as handle:
            lines = [line.split("#", 1)[0].strip() for line in handle]
    except OSError:
        return []
    return [line.rstrip("/") for line in lines if line]


# [@ANCHOR: anchor_docs_standard:doc_findings]
def doc_findings(path, rel, min_words=MIN_WORDS):
    """Findings for one markdown file: a list of strings, one per anchor entry whose share (entry words divided by the
    anchors it covers) is under `min_words`. `rel` is the name used in the message."""
    found = []
    for entry in doc_entries(path):
        share = entry["shape"]["words"] / len(entry["anchors"])
        if share < min_words:
            found.append(f"{rel}:{entry['line']} entry for {', '.join(entry['anchors'])} has {share:.0f} words per anchor "
                         f"(under {min_words}): write the full entry (purpose, arguments, return, errors, special concerns)")
    return found


# [@ANCHOR: anchor_docs_standard:source_findings]
def source_findings(path, rel, root):
    """Findings for one source file: a list of strings, one per data structure without an anchor
    (`find_data_structures`)."""
    return [f"{rel}:{line} {kind} {name} has no anchor: anchor it and describe every field in the README (ADR 0113)"
            for line, kind, name in find_data_structures(path, root)]


def _files_under(root, rel):
    """The files a listed path stands for: the file itself, or every file below a directory (pruned like the anchor scan)."""
    full = os.path.join(root, rel)
    if os.path.isfile(full):
        return [full]
    found = []
    for here, dirs, files in _walk_tree_files(full):
        dirs[:] = sorted(d for d in dirs if not _is_excluded_dir(d) and d not in ("node_modules", "venv", ".venv", "site-packages"))
        found += [os.path.join(here, name) for name in sorted(files)]
    return found


# [@ANCHOR: anchor_docs_standard:find_findings]
def find_findings(root, listed):
    """Runs the checks for every path of `listed` that exists under `root`. Returns `(findings, checked)`: the finding
    strings and the number of listed paths found in this repository. A listed directory covers its source files only; a
    documentation file is checked when it is listed by name."""
    findings, checked = [], 0
    for rel in listed:
        if not os.path.exists(os.path.join(root, rel)):
            continue
        checked += 1
        for path in _files_under(root, rel):
            name = os.path.relpath(path, root)
            if rel.endswith(".md"):
                findings += doc_findings(path, name)
            elif path.endswith(SOURCE_SUFFIXES):
                findings += source_findings(path, name, root)
    return findings, checked


# [@ANCHOR: anchor_docs_standard:main]
def main(argv=None):
    """Command line: `check_anchor_docs_standard.py <repo_root>`. Prints each finding and returns 1, or one line and 0;
    2 for a usage error."""
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: check_anchor_docs_standard.py <repo_root>", file=sys.stderr)
        return 2
    target = check_target.require_directory(argv[0], "check_anchor_docs_standard")
    findings, checked = find_findings(target, read_list())
    # A directory that holds none of the listed paths was not checked at all: that is a failure, not a pass.
    check_target.require_scanned(checked, target, "check_anchor_docs_standard", "listed paths")
    if findings:
        print("[!] ADR 0113: listed documentation or code is below the anchor documentation standard:")
        for finding in findings:
            print(f"    - {finding}")
        return 1
    print(f"[+] SUCCESS: {checked} listed path(s) in this repository meet the anchor documentation standard.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
