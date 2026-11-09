#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Anchor documentation inventory (step 1 of night_shift_todo/low/document-every-anchor-completely, ADR 0113).

Reports, per README.md / docs file, how many anchors it documents and how long each entry is, and lists the
data-structure definitions (Rust struct/enum/type/union, Python class, JavaScript class) that have no anchor.
It reads files only and prints a report; it never fails on what it finds (it is a measurement, not a gate).

    python3 hams_shared/tools/anchor_doc_inventory.py [ROOT ...] [--short-words N] [--limit N] [--structures]

Run it from a clean worktree of the repository (a worktree under another directory name is fine). The order of
the report puts the modules ADR 0113 names first: the relay, radioctl, the console, then the rest.

Tests: tools/test_anchor_doc_inventory.py. Documented in docs/runbooks/ANCHOR_DOC_INVENTORY.md.
"""

import argparse
import os
import re
import statistics
import sys

from verify_anchors import ANCHOR_PATTERN, _is_excluded_dir

DEFAULT_SHORT_WORDS = 40
# Directory-name fragments in the order ADR 0113 gives (transmit-critical first); anything else sorts after.
IMPORTANCE = ("hams_local_relay", "radioctl", "console", "shack")
SOURCE_SUFFIXES = (".rs", ".py", ".js", ".mjs")
HEADING = re.compile(r"^(#{1,6})\s")
STRUCT_PATTERNS = {
    ".rs": re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(struct|enum|type|union)\s+([A-Za-z_]\w*)"),
    ".py": re.compile(r"^\s*(class)\s+([A-Za-z_]\w*)"),
    ".js": re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(class)\s+([A-Za-z_]\w*)"),
}
STRUCT_PATTERNS[".mjs"] = STRUCT_PATTERNS[".js"]
TEST_PATH = re.compile(r"(^|/)(tests?|test_[^/]*|[^/]*_tests?\.[a-z]+|[^/]*\.test\.[a-z]+|[^/]*\.rs\.test)(/|$)")


# [@ANCHOR: anchor_doc_inventory:entry_shape]
def entry_shape(text):
    """Measures one documentation entry: `{"words", "table", "labelled"}`. `words` counts words outside anchor
    tags; `table` is True when the entry has a markdown table row; `labelled` counts `**Label:**` paragraphs."""
    clean = ANCHOR_PATTERN.sub("", text)
    return {
        "words": len(re.findall(r"[A-Za-z0-9_`'.-]+", clean)),
        "table": any(line.lstrip().startswith("|") for line in clean.splitlines()),
        "labelled": len(re.findall(r"^\s*(?:[*-]\s+)?\*\*[^*\n]+:\*\*", clean, re.MULTILINE)),
    }


# [@ANCHOR: anchor_doc_inventory:doc_entries]
def doc_entries(path):
    """The documentation entries of one markdown file: a list of `{"anchors": [ids], "line": n, "shape": {...}}`.

    An anchor on a heading line owns the text up to the next heading of the same or a higher level; an anchor in
    a bullet or paragraph owns that paragraph (up to the next blank line or heading). Several anchors in one entry
    share it, which is how the older one-line README bullets are written. An unreadable file gives `[]`."""
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    entries, i = [], 0
    while i < len(lines):
        ids = ANCHOR_PATTERN.findall(lines[i])
        if not ids:
            i += 1
            continue
        heading = HEADING.match(lines[i])
        j = i + 1
        if heading:
            level = len(heading.group(1))
            while j < len(lines):
                other = HEADING.match(lines[j])
                if other and len(other.group(1)) <= level:
                    break
                j += 1
        else:
            while j < len(lines) and lines[j].strip() and not HEADING.match(lines[j]):
                j += 1
        text = "\n".join(lines[i:j])
        # A paragraph may carry anchors on several of its lines; they share the one entry.
        ids = ANCHOR_PATTERN.findall(text)
        entries.append({"anchors": ids, "line": i + 1, "shape": entry_shape(text)})
        i = j
    return entries


# [@ANCHOR: anchor_doc_inventory:scan_docs]
def scan_docs(root, short_words=DEFAULT_SHORT_WORDS):
    """Per documentation file under `root`: `{"path", "entries", "anchors", "median_words", "short", "tables"}`.

    Only README.md (any case) and `*.md` under a `docs` directory are read. `anchors` counts anchor tags;
    `median_words` is the median of each anchor's share of its entry (entry words divided by the anchors it
    covers); `short` counts anchors whose share is under `short_words`; `tables` counts entries with a table.
    Directories the anchor scan prunes (`.git`, `target`, `.claude*`) are skipped. Sorted by importance."""
    report = []
    for here, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not _is_excluded_dir(d))
        in_docs = "docs" in os.path.relpath(here, root).split(os.sep)
        for name in sorted(files):
            if not (name.lower() == "readme.md" or (in_docs and name.endswith(".md"))):
                continue
            path = os.path.join(here, name)
            entries = doc_entries(path)
            if not entries:
                continue
            shares = []
            for entry in entries:
                shares += [entry["shape"]["words"] / len(entry["anchors"])] * len(entry["anchors"])
            report.append({
                "path": os.path.relpath(path, root),
                "entries": len(entries),
                "anchors": len(shares),
                "median_words": round(statistics.median(shares), 1),
                "short": sum(1 for share in shares if share < short_words),
                "tables": sum(1 for entry in entries if entry["shape"]["table"]),
            })
    report.sort(key=lambda row: (importance(row["path"]), -row["short"], row["path"]))
    return report


def importance(path):
    """Sort key: the index of the first IMPORTANCE fragment the path contains, else after all of them."""
    for rank, fragment in enumerate(IMPORTANCE):
        if fragment in path:
            return rank
    return len(IMPORTANCE)


# [@ANCHOR: anchor_doc_inventory:find_data_structures]
def find_data_structures(path, root=None):
    """Data-structure definitions in one source file that carry no anchor: a list of `(line, kind, name)`.

    A definition counts as anchored when an anchor tag is on the definition line or in the unbroken run of
    attribute, decorator and comment lines directly above it (a blank line or code ends the run). Rust code from a
    `#[cfg(test)]` line to the end of the file, and files in test paths, are skipped. Python classes nested in
    functions are found as well. An unreadable file gives `[]`. With `root`, the test-path rule is applied to the path
    below `root`, so a directory above the scanned tree that is named like a test directory does not hide the tree."""
    suffix = os.path.splitext(path)[1]
    pattern = STRUCT_PATTERNS.get(suffix)
    judged = os.path.relpath(path, root) if root else path
    if pattern is None or TEST_PATH.search(judged.replace(os.sep, "/")):
        return []
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    missing = []
    for number, line in enumerate(lines):
        if suffix == ".rs" and line.strip().startswith("#[cfg(test)]"):
            break
        match = pattern.match(line)
        if not match:
            continue
        block, above = [line], number - 1
        while above >= 0 and lines[above].strip().startswith(("#", "//", "@", "/*", "*")):
            block.append(lines[above])
            above -= 1
        if not any(ANCHOR_PATTERN.search(text) for text in block):
            missing.append((number + 1, match.group(1), match.group(2)))
    return missing


# [@ANCHOR: anchor_doc_inventory:scan_structures]
def scan_structures(root):
    """`{relative source path: [(line, kind, name)]}` for every source file under `root` (`.rs .py .js .mjs`) that
    has data-structure definitions without an anchor (see `find_data_structures`). Pruned directories as in
    `scan_docs`, plus `node_modules` and virtual environments."""
    found = {}
    for here, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not _is_excluded_dir(d) and d not in ("node_modules", "venv", ".venv", "site-packages"))
        for name in sorted(files):
            if name.endswith(SOURCE_SUFFIXES):
                path = os.path.join(here, name)
                missing = find_data_structures(path, root)
                if missing:
                    found[os.path.relpath(path, root)] = missing
    return found


# [@ANCHOR: anchor_doc_inventory:main]
def main(argv=None):
    """Command line: prints the documentation table, then (with `--structures`) the unanchored data structures.
    Returns 0 always (a measurement); 2 when a ROOT is not a directory."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("roots", nargs="*", default=["."])
    parser.add_argument("--short-words", type=int, default=DEFAULT_SHORT_WORDS,
                        help="an anchor whose share of its entry is under this many words is 'short'")
    parser.add_argument("--limit", type=int, default=40, help="rows to print (0 for all)")
    parser.add_argument("--structures", action="store_true", help="also list data structures with no anchor")
    options = parser.parse_args(argv)
    for root in options.roots:
        if not os.path.isdir(root):
            print("not a directory: %s" % root, file=sys.stderr)
            return 2
        rows = scan_docs(root, options.short_words)
        total, short = sum(r["anchors"] for r in rows), sum(r["short"] for r in rows)
        print("%s: %d documentation files, %d anchors, %d under %d words" % (root, len(rows), total, short, options.short_words))
        print("%6s %6s %8s %6s  %s" % ("anchors", "short", "median", "tables", "file"))
        for row in rows[:options.limit or None]:
            print("%6d %6d %8.1f %6d  %s" % (row["anchors"], row["short"], row["median_words"], row["tables"], row["path"]))
        if options.structures:
            structures = scan_structures(root)
            print("data structures without an anchor: %d in %d files" % (sum(map(len, structures.values())), len(structures)))
            for path in sorted(structures, key=lambda p: (importance(p), p))[:options.limit or None]:
                print("  %s: %s" % (path, ", ".join("%s %s:%d" % (kind, name, line) for line, kind, name in structures[path])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
