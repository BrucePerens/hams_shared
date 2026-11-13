# SPDX-License-Identifier: AGPL-3.0-or-later
"""Glosses a term or acronym at its first prose use in each Markdown document.

A dictionary file (`gloss_first_use_terms.txt`, lines `TERM | expansion`) names the terms. For each
document the first use in prose (not in headings, fenced or indented code, inline code, links, URLs,
paths, tables, HTML tags or anchor lines) is found. A document passes when that use already has a gloss
(`TERM (` or `TERM is/means/stands for` follows, or the expansion appears earlier in the document).
`--apply` rewrites the first use
as `TERM (expansion)`; a use that ends a parenthesis or clause inside parentheses becomes `TERM: expansion`.

    gloss_first_use.py [--terms FILE] [--apply] [PATH ...]

PATHs are Markdown files or directories; with none, the README, ADR, design, runbook and story files
under the current directory (symlinks are skipped, as hams_shared docs are linked into other repos).
Check mode lists `path:line TERM` for every missing gloss and exits 1 if there is one.
"""

import argparse
import os
import re
import subprocess
import sys

DEFAULT_TERMS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gloss_first_use_terms.txt")
SCOPE_DIRECTORIES = {"adrs", "design", "runbooks", "stories"}
_MASK = "\x00"
_INLINE_MASKS = (
    re.compile(r"`[^`\n]*`"),
    re.compile(r"!?\[[^\]\n]*\]\([^)\n]*\)"),
    re.compile(r"<[^>\n]+>"),
    re.compile(r"\[@[^\]\n]*\]"),
    re.compile(r"(?:https?://|www\.)\S+"),
    re.compile(r"\S*[/\\]\S*"),
    re.compile(r"\w+\.(?:md|py|rs|js|xml|json|txt|sh|html|yml|yaml|toml|conf|csv|service|timer)\b"),
)
_LIST_ITEM = re.compile(r"\s*(?:[-*+]|\d+[.)])\s")


def load_terms(path):
    """Returns {term: expansion} from a `TERM | expansion` file; `#` lines and blanks are ignored."""
    terms = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line and not line.startswith("#"):
                term, _, expansion = line.partition("|")
                terms[term.strip()] = expansion.strip()
    return terms


def _prose_mask(text):
    """Returns `text` with every character that is not prose replaced by a NUL, same length."""
    out = []
    fence = None
    in_list = False
    in_comment = False
    lines = text.split("\n")
    if lines and lines[0].strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                lines[: index + 1] = [_MASK * len(line) for line in lines[: index + 1]]
                break
    for line in lines:
        stripped = line.lstrip()
        if line.startswith(_MASK):
            out.append(line)
            continue
        if fence:
            if stripped.startswith(fence):
                fence = None
            out.append(_MASK * len(line))
            continue
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fence = stripped[:3]
            out.append(_MASK * len(line))
            continue
        if in_comment or stripped.startswith("<!--"):
            in_comment = "-->" not in line
            out.append(_MASK * len(line))
            continue
        if not stripped:
            out.append(line)
            continue
        if _LIST_ITEM.match(line):
            in_list = True
        indented_code = (line.startswith("    ") or line.startswith("\t")) and not in_list
        if not line.startswith((" ", "\t")) and not _LIST_ITEM.match(line):
            in_list = False
        if (indented_code or stripped.startswith(("#", "|", ">|")) or "[@" in line):
            out.append(_MASK * len(line))
            continue
        for pattern in _INLINE_MASKS:
            line = pattern.sub(lambda match: _MASK * len(match.group()), line)
        out.append(line)
    return "\n".join(out)


def _normal(text):
    return re.sub(r"[\s-]+", " ", text.lower())


# [@ANCHOR: gloss_first_use:find_missing]
def find_missing(text, terms):
    """Returns [(term, offset, inside_parentheses)] for each term whose first prose use in `text` has no
    gloss, in document order. A gloss is `TERM (` right after the use, or the expansion earlier in the text."""
    mask = _prose_mask(text)
    missing = []
    for term, expansion in terms.items():
        match = re.search(r"(?<![\w/.-])%s(?:s)?(?![\w/-])(?!\.\w)" % re.escape(term), mask)
        if not match:
            continue
        start, end = match.start(), match.end()
        defined = re.match(r"\s*\(|,? (?:is|are|means|stands for|i\.e\.|short for)(?!\w)", text[end:end + 40])
        if defined or _normal(expansion) in _normal(text[:start]) or _normal(text[end:end + 80]).startswith(
                ": " + _normal(expansion)):
            continue
        paragraph = mask[mask.rfind("\n\n", 0, start) + 1:start]
        inside = paragraph.count("(") > paragraph.count(")") and re.match(r"[),;.]", text[end:end + 1])
        missing.append((term, start, bool(inside)))
    return sorted(missing, key=lambda item: item[1])


# [@ANCHOR: gloss_first_use:apply_glosses]
def apply_glosses(text, terms):
    """Returns `text` with each missing first use rewritten as `TERM (expansion)` (`TERM: expansion` inside
    parentheses). Nothing else changes."""
    for term, start, in_parentheses in reversed(find_missing(text, terms)):
        end = start + len(term) + (1 if text[start + len(term):start + len(term) + 1] == "s" else 0)
        gloss = ": %s" if in_parentheses else " (%s)"
        text = text[:end] + gloss % terms[term] + text[end:]
    return text


def default_documents(root):
    """README, ADR, design, runbook and story Markdown files under `root` (tracked, not symlinks)."""
    listing = subprocess.run(["git", "ls-files", "*.md"], cwd=root, capture_output=True, text=True, check=True)
    documents = []
    for name in listing.stdout.splitlines():
        path = os.path.join(root, name)
        parts = name.split("/")
        in_scope = os.path.basename(name).startswith("README") or SCOPE_DIRECTORIES & set(parts[:-1])
        if in_scope and not any(
                os.path.islink(os.path.join(root, *parts[:depth])) for depth in range(1, len(parts) + 1)):
            documents.append(path)
    return documents


def _expand(paths):
    documents = []
    for path in paths:
        if os.path.isdir(path):
            for directory, names, files in os.walk(path):
                names[:] = [n for n in names if n != ".git" and not os.path.islink(os.path.join(directory, n))]
                documents += [os.path.join(directory, f) for f in files if f.endswith(".md")
                              and not os.path.islink(os.path.join(directory, f))]
        else:
            documents.append(path)
    return sorted(documents)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("paths", nargs="*")
    parser.add_argument("--terms", default=DEFAULT_TERMS)
    parser.add_argument("--apply", action="store_true", help="rewrite the first uses instead of listing them")
    args = parser.parse_args(argv)
    terms = load_terms(args.terms)
    documents = _expand(args.paths) if args.paths else default_documents(".")
    problems = 0
    for path in documents:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        missing = find_missing(text, terms)
        if not missing:
            continue
        problems += len(missing)
        if args.apply:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(apply_glosses(text, terms))
        else:
            for term, offset, _inside in missing:
                print("%s:%d %s" % (path, text.count("\n", 0, offset) + 1, term))
    print("%d missing glosses in %d documents%s" % (problems, len(documents), " (fixed)" if args.apply else ""))
    return 1 if problems and not args.apply else 0


if __name__ == "__main__":
    sys.exit(main())
