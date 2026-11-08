# Anchor documentation inventory

`tools/anchor_doc_inventory.py` is step 1 of `night_shift_todo/low/document-every-anchor-completely-*.md` (ADR 0113): it
measures how complete the documentation entries for anchors are and lists data structures that have no anchor. It reads files
and prints; it never fails on what it finds, because it is a measurement and not a gate (the gate is step 2 of the to-do, an
extension of `verify_anchors.py`).

```
python3 hams_shared/tools/anchor_doc_inventory.py [ROOT ...] [--short-words N] [--limit N] [--structures]
```

| Option | Default | Meaning |
|---|---|---|
| `ROOT` | `.` | A checkout or worktree to measure; several may be given, each is reported on its own. Exit code 2 when one is not a directory. |
| `--short-words N` | 40 | An anchor whose share of its entry is under N words counts as "short". |
| `--limit N` | 40 | Rows to print per table; 0 prints all. |
| `--structures` | off | Also list the data-structure definitions without an anchor. |

The report has one row per documentation file (columns: anchors, short, median words per anchor, entries with a table, path),
ordered relay first, then radioctl, the console, and everything else, and inside a group by the number of short anchors.
The first line gives the totals.

### `anchor_doc_inventory:entry_shape`
`entry_shape(text) -> {"words", "table", "labelled"}`. `words` counts the words of the entry outside anchor tags (a word is a run of
letters, digits, backticks, `_`, `'`, `.` and `-`), `table` is True if any line starts with `|`, `labelled` counts `**Label:**`
paragraphs (the "Errors:", "Concurrency:" paragraphs ADR 0113 asks for). Pure; never raises. Test:
`EntryShapeTests` in `tools/test_anchor_doc_inventory.py`.

### `anchor_doc_inventory:doc_entries`
`doc_entries(path) -> [{"anchors": [ids], "line": n, "shape": entry_shape}]`. An anchor on a heading line owns the text up to the
next heading of the same or a higher level (so sub-headings belong to it); an anchor in a bullet or paragraph owns the
paragraph (up to a blank line or heading). Several anchors in one entry share it, which is how the older one-line README bullets
are written. A file that cannot be read (missing, not UTF-8) gives `[]`. Test: `DocEntryTests`.

### `anchor_doc_inventory:scan_docs`
`scan_docs(root, short_words=40) -> [row]` with `row = {"path", "entries", "anchors", "median_words", "short", "tables"}`. Reads
`README.md` (any case) and `*.md` files under a `docs` directory; skips the directories `verify_anchors` prunes (`.git`,
`target`, `__pycache__`, `.claude*`). The median and the short count use each anchor's share of its entry (entry words divided
by the anchors on it). Test: `ScanDocsTests`.

### `anchor_doc_inventory:find_data_structures`
`find_data_structures(path) -> [(line, kind, name)]`: Rust `struct`/`enum`/`type`/`union`, Python `class` and JavaScript `class`
definitions in one file with no anchor tag on the definition line or in the unbroken run of attribute, decorator and comment
lines above it. Rust code after a `#[cfg(test)]` line and files in test paths are skipped, as is any other file type. This is a
line scan, not a parse: a definition inside a comment or string can be listed, and a macro-generated type is missed. Test:
`DataStructureTests`.

### `anchor_doc_inventory:scan_structures`
`scan_structures(root) -> {relative path: [(line, kind, name)]}`: `find_data_structures` over every `.rs .py .js .mjs` file under
`root`, skipping the pruned directories and `node_modules`, virtual environments and `site-packages`. Test: `DataStructureTests`.

### `anchor_doc_inventory:main`
`main(argv=None) -> int`: the command line above. Returns 0, or 2 for a root that is not a directory (message on stderr). Test:
`MainTests`.
