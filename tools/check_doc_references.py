#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Documentation reference checker: Markdown must not point at things that do not exist.

Scans the tracked Markdown of one repository (hams_open includes hams_shared) and reports every broken reference as
`file:line: kind: reference`. Kinds:

  path      a file or directory in backticks or in a link target that exists nowhere (relative to the document, to a parent
            directory of it, to a repository root, or as the tail of any tracked path)
  flag      `script.py --flag` where the script's argparse does not define the flag
  adr       `ADR 0090` with no docs/adrs/0090_*.md in hams_com, hams_open or hams_shared
  anchor    `#heading` link (same or other document) with no such heading ([@ANCHOR] tags are verify_anchors.py's job)
  unit      `name.service` / `.timer` / `.socket` that no tracked non-Markdown file mentions
  config    `HAMS_...` environment variable, or a dotted config parameter on a line that talks about parameters/settings,
            that no tracked non-Markdown file mentions
  function  `name()` / `Class.method()` / `mod::name()` whose last name is an identifier no tracked non-Markdown file contains

Historical, generated and forward-looking Markdown (claims, bug-hunt notes, night_shift_*, archive, tmp, output, proposals,
patent disclosures, reviews, outbound drafts) is not scanned: it records what was true when written or what is planned.

Like the function-anchor ratchet (check_function_test_anchors.py) this is a baseline ratchet: the findings that exist
today are recorded in a JSON baseline and printed as a count; a finding that is not in the baseline fails the run.
Baseline keys have no line numbers (`file|kind|reference`), so editing a document does not invalidate them. Fix a document
and the entry disappears from the next `--generate-baseline`.

  check_doc_references.py REPO [--baseline FILE] [--generate-baseline] [--list]
"""

import argparse
import ast
import builtins
import json
import os
import re
import subprocess
import sys

import check_target  # noqa: E402

EXCLUDE_PREFIXES = (
    "night_shift_", "archive/", "tmp/", "output/", "docs/bug_hunt", "docs/BUG_HUNT", "docs/proposals/", "docs/patent_disclosures/",
    "docs/reviews/", "docs/outbound_drafts/",
)
EXCLUDE_PARTS = ("/claims/", "/node_modules/", "/target/", "/vendor/", "/third_party/")
PATH_EXTENSIONS = {
    "py", "rs", "js", "xml", "json", "csv", "toml", "sh", "md", "html", "scss", "service", "timer", "yml", "yaml", "cfg", "ini",
    "conf", "pyi",
}
SOURCE_NAME_EXTENSIONS = {"py", "rs", "js", "xml", "sh", "md", "html", "scss", "toml"}
LINK_EXTENSIONS = PATH_EXTENSIONS | {"png", "jpg", "svg", "txt", "pdf", "gif"}
SOURCE_SUFFIXES = {
    ".py", ".rs", ".js", ".xml", ".json", ".csv", ".toml", ".sh", ".html", ".scss", ".service", ".timer", ".socket", ".yml",
    ".yaml", ".cfg", ".ini", ".conf", ".pyi", ".txt", ".mjs", ".ts", ".css", ".target", ".path", ".j2", ".tmpl", ".cfg", ".md.j2",
}
# Paths into the Odoo source or other outside trees, which are not tracked here (and may not be installed on a lint host).
EXTERNAL_PREFIXES = ("odoo/", "web/", "web_tour/", "base/", "mail/", "portal/", "website/", "bus/", "huggingface/", "node_modules/")
MAX_SOURCE_BYTES = 1_000_000
UNIT_RE = re.compile(r"^[A-Za-z0-9_\-@.]+\.(?:service|timer|socket)$")
UNIT_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-@.]+\.(?:service|timer|socket)\b")
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
WORD_RE = re.compile(r"[A-Za-z0-9_.\-]+")
ENV_RE = re.compile(r"^HAMS_[A-Z0-9_]+$")
PARAM_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
FUNC_RE = re.compile(r"^((?:[A-Za-z_][A-Za-z0-9_]*(?:\.|::))*)([A-Za-z_][A-Za-z0-9_]*)\(.*\)$")
ADR_RE = re.compile(r"\bADR[ -]?0*(\d{1,4})\b")
LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
HEADING_RE = re.compile(r"^ {0,3}#{1,6}\s+(.*?)\s*#*\s*$")
SPAN_RE = re.compile(r"(`+)(.+?)\1")
FLAG_RE = re.compile(r"(?<![\w-])(--[A-Za-z][\w-]*)")
SCRIPT_RE = re.compile(r"(?<![\w./-])((?:[\w.\-]+/)*[\w.\-]+\.py)(?=\s|$|`)(.*)")
FENCE_RE = re.compile(r"^\s*(```+|~~~+)")
SKIP_FUNCTIONS = set(dir(builtins)) | set(getattr(sys, "stdlib_module_names", ()))
PARAM_LINE_WORDS = ("config parameter", "config_parameter", "system parameter", "ir.config", "set_param", "get_param")


def is_excluded(rel):
    return rel.startswith(EXCLUDE_PREFIXES) or any(part in "/" + rel for part in EXCLUDE_PARTS)


def tracked_files(root):
    """Absolute paths of the files git tracks in `root` (none if it is not a git work tree)."""
    out = subprocess.run(["git", "-C", root, "ls-files", "-z"], capture_output=True, check=False).stdout.decode("utf-8", "replace")
    return [os.path.join(root, name) for name in out.split("\0") if name]


# [@ANCHOR: check_doc_references:check_doc_references_knowledge_class]
class Knowledge:
    """What exists: tracked paths (with every tail), identifiers, unit names, ADR numbers, anchors, env/config words."""

    def __init__(self, files, roots):
        self.roots = roots
        self.tails = {}
        self.present = set()  # tracked files and their directories: what exists is decided by git, not by what is on this disk
        self.identifiers = set()
        self.words = set()
        self.units = set()
        self.adrs = set()
        self._argparse = {}
        for path in files:
            self._add_path(path)
            self._add_aliases(path)
            if path.endswith(".md"):
                continue
            self._add_source(path)

    def _add_path(self, path):
        parts = path.split(os.sep)
        for index in range(2, len(parts) + 1):
            self.present.add(os.sep.join(parts[:index]))
        for index in range(1, len(parts)):
            self.tails.setdefault("/".join(parts[index:]), []).append(path)
            if index < len(parts) - 1:
                self.tails.setdefault("/".join(parts[index:]) + "/", []).append(path)
        match = re.match(r"^(\d{1,4})_.*\.md$", parts[-1])
        if match and "adrs" in parts[:-1]:
            self.adrs.add(int(match.group(1)))
        if UNIT_RE.match(parts[-1]):
            self.units.add(normalise_unit(parts[-1]))

    def _add_aliases(self, path):
        """hams_shared is checked out at hams_open/hams_shared and linked as hams_com/hams_shared: those paths exist too."""
        for root in self.roots:
            if repo_name(root) == "hams_shared" and path.startswith(root + os.sep):
                rel = os.path.relpath(path, root)
                for other in self.roots:
                    if repo_name(other) in ("hams_open", "hams_com"):
                        parts = os.path.join(other, "hams_shared", rel).split(os.sep)
                        self.present.update(os.sep.join(parts[:i]) for i in range(2, len(parts) + 1))

    def _add_source(self, path):
        if os.path.splitext(path)[1] not in SOURCE_SUFFIXES or os.path.islink(path):
            return
        try:
            if os.path.getsize(path) > MAX_SOURCE_BYTES:
                return
            with open(path, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            return
        self.identifiers.update(IDENT_RE.findall(text))
        self.words.update(WORD_RE.findall(text))
        self.units.update(normalise_unit(unit) for unit in UNIT_TOKEN_RE.findall(text))

    def exists(self, token, doc_dir, strict=False):
        """The file(s) a path token names, or []. `strict` (links): only relative to the document or a root."""
        found = []
        starts = [doc_dir] if strict else ancestors(doc_dir, self.roots)
        for base in starts + list(self.roots) + [os.path.join(root, "hams_shared") for root in self.roots]:
            candidate = os.path.normpath(os.path.join(base, token))
            if candidate in self.present:
                found.append(candidate)
        head, _, rest = token.partition("/")
        if not strict and rest and head in ("hams_com", "hams_open", "hams_shared"):  # a repository named by its directory name
            for root in self.roots:
                if repo_name(root) == head and os.path.normpath(os.path.join(root, rest)) in self.present:
                    found.append(os.path.normpath(os.path.join(root, rest)))
            if head == "hams_open" and rest.startswith("hams_shared/"):
                found += self.exists(rest, doc_dir)
        if found or strict:
            return found
        return self.tails.get(token.lstrip("./"), []) if not token.startswith("..") else []

    def logical_dirs(self, doc_dir):
        """The document's directory, and where it sits in the canonical layout (hams_shared is checked out at hams_open/hams_shared)."""
        out = [doc_dir]
        for root in self.roots:
            if repo_name(root) == "hams_shared" and (doc_dir == root or doc_dir.startswith(root + os.sep)):
                for other in self.roots:
                    if repo_name(other) == "hams_open":
                        out.append(os.path.join(other, "hams_shared", os.path.relpath(doc_dir, root)))
        return out

    def argparse_flags(self, path):
        """Option strings of every `add_argument` in the script, or None if it has none or builds them dynamically."""
        if path not in self._argparse:
            self._argparse[path] = parse_argparse(path)
        return self._argparse[path]


def normalise_unit(name):
    return re.sub(r"@[^.@]+\.", "@.", name)


def ancestors(directory, roots):
    """The document directory and each parent up to the top-most root."""
    out = []
    current = directory
    while True:
        out.append(current)
        parent = os.path.dirname(current)
        if parent == current or not any(current == root or current.startswith(root + os.sep) for root in roots):
            return out
        current = parent


def parse_argparse(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            tree = ast.parse(handle.read())
    except (OSError, SyntaxError):
        return None
    flags = set()
    seen = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument":
            seen = True
            if not node.args or not all(isinstance(arg, ast.Constant) and isinstance(arg.value, str) for arg in node.args):
                return None
            flags.update(arg.value for arg in node.args if arg.value.startswith("--"))
    return flags if seen else None


def slug(heading):
    text = re.sub(r"[`*_]|\[([^\]]*)\]\([^)]*\)", lambda m: m.group(1) or "", heading.strip().lower())
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"\s", "-", text)


def heading_slugs(text):
    seen = {}
    slugs = set()
    in_fence = False
    for line in text.splitlines():
        if FENCE_RE.match(line):
            in_fence = not in_fence
        match = None if in_fence else HEADING_RE.match(line)
        if match:
            base = slug(match.group(1))
            count = seen.get(base, 0)
            seen[base] = count + 1
            slugs.add(base if count == 0 else f"{base}-{count}")
    slugs.update(re.findall(r"""<a\s+(?:name|id)=["']([^"']+)["']""", text))
    return slugs


def path_candidate(span):
    """A backtick span that is one path-looking token, with a trailing `:123` stripped, else None."""
    token = re.sub(r":\d+(?:-\d+)?$", "", span.strip())
    if not token or re.search(r"[\s*<>{}$:|;=(),'\"?!^\[\]\\]", token) or token.startswith(("/", "~", "http", "-", ".../", "...")):
        return None
    if token.startswith(EXTERNAL_PREFIXES) or re.search(r"\.\.\.|//", token):
        return None
    if len(os.path.splitext(os.path.basename(token))[0]) < 2 and not token.endswith("/"):
        return None  # `x.py`, `a.md`: an example name
    if token.endswith("/"):
        return token if "/" in token.rstrip("/") else None
    extension = token.rsplit(".", 1)[-1] if "." in os.path.basename(token) else ""
    if "/" not in token and extension not in SOURCE_NAME_EXTENSIONS:
        return None
    if extension not in PATH_EXTENSIONS or os.path.basename(token).startswith("."):
        return None
    return token


def check_script_flags(line, doc_dir, know):
    """Yield (script, flag) for each long flag after `script.py` that the script's argparse does not define."""
    for match in SCRIPT_RE.finditer(line):
        script, rest = match.group(1), match.group(2)
        before = line[: match.start(1)].replace("`", " ").split()
        if before and before[-1].startswith("-") and before[-1] != "--":
            continue  # the script is an argument of something else (`wait --cmd-contains x.py --timeout 5`)
        rest = re.split(r"`|\s&&\s|\s\|\|?\s|;|\s#\s|\s--\s|\s{2,}|\(", rest)[0]
        flags = FLAG_RE.findall(rest)
        if not flags:
            continue
        paths = [p for p in know.exists(script, doc_dir) if p.endswith(".py") and os.path.isfile(p)]
        known = [know.argparse_flags(p) for p in paths]
        known = [k for k in known if k is not None]
        if not known or len(known) != len(paths):
            continue
        defined = set().union(*known)
        for flag in flags:
            if flag in defined or any(d.startswith(flag) for d in defined) or ("--no-" + flag[2:]) in defined:
                continue
            if flag.startswith("--no-") and ("--" + flag[5:]) in defined:
                continue
            if flag in ("--help",):
                continue
            yield script, flag


def check_markdown(text, doc_abs, know):
    """Return a sorted list of (line, kind, reference) for one Markdown document."""
    doc_dir = os.path.dirname(doc_abs)
    own_slugs = heading_slugs(text)
    findings = set()
    in_fence = False
    for number, line in enumerate(text.splitlines(), 1):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        for script, flag in check_script_flags(line, doc_dir, know):
            findings.add((number, "flag", f"{script} {flag}"))
        if in_fence:
            continue
        for match in LINK_RE.finditer(line):
            findings.update(check_link(match.group(1), number, doc_abs, doc_dir, own_slugs, know))
        spans = [m.group(2) for m in SPAN_RE.finditer(line)]
        param_line = any(word in line.lower() for word in PARAM_LINE_WORDS)
        for span in spans:
            findings.update(check_span(span, number, doc_dir, know, param_line))
        for match in ADR_RE.finditer(line):
            if int(match.group(1)) not in know.adrs and int(match.group(1)) > 0:
                findings.add((number, "adr", f"ADR {int(match.group(1)):04d}"))
    return sorted(findings)


def check_link(target, number, doc_abs, doc_dir, own_slugs, know):
    if re.match(r"^[a-z][a-z0-9+.\-]*:", target, re.I) or target.startswith(("<", "{", "$", "/")):
        return  # a URL, a placeholder or a site path (`/shack`), not a file
    path, _, fragment = target.partition("#")
    path = path.split("?")[0]
    if not path:
        if fragment and fragment not in own_slugs and not fragment.startswith("L"):
            yield number, "anchor", "#" + fragment
        return
    # a documentation site serves `page.md` as `page` and `dir/index.md` as `dir`
    found = []
    for base in know.logical_dirs(doc_dir):
        found = found or know.exists(path, base, strict=True) or know.exists(path + ".md", base, strict=True)
    if not found:
        yield number, "path", target
        return
    if fragment and path.endswith(".md") and os.path.isfile(found[0]) and not fragment.startswith("L"):
        try:
            with open(found[0], encoding="utf-8", errors="replace") as handle:
                slugs = heading_slugs(handle.read())
        except OSError:
            return
        if fragment not in slugs:
            yield number, "anchor", target


def check_span(span, number, doc_dir, know, param_line):
    span = span.strip()
    token = path_candidate(span)
    if token and not token.startswith(("%", "@")):
        if not know.exists(token, doc_dir):
            yield number, "path", token
        return
    if UNIT_RE.match(span) and "<" not in span:
        if normalise_unit(span) not in know.units:
            yield number, "unit", span
        return
    if ENV_RE.match(span):
        if span not in know.words:
            yield number, "config", span
        return
    if param_line and PARAM_RE.match(span) and span.rsplit(".", 1)[-1] not in ("py", "md", "json", "xml"):
        if span not in know.words:
            yield number, "config", span
        return
    match = FUNC_RE.match(span)
    if match:
        prefix, name = match.group(1), match.group(2)
        head = prefix.replace("::", ".").split(".")[0]
        if len(name) < 4 or name in SKIP_FUNCTIONS or (head and (head in SKIP_FUNCTIONS or head in ("self", "cls"))):
            return
        if name not in know.identifiers:
            yield number, "function", span.split("(")[0] + "()"


def markdown_files(root):
    out = []
    for path in tracked_files(root):
        rel = os.path.relpath(path, root)
        if rel.endswith(".md") and not is_excluded(rel) and os.path.isfile(path) and not os.path.islink(path):
            out.append((path, rel))
    return out


def repo_name(path):
    base = os.path.basename(os.path.realpath(path))
    for name in ("hams_shared", "hams_open", "hams_com"):
        if base.startswith(name):
            return name
    return base


def sibling_roots(root):
    """The repository itself first, then the other two repositories next to it (hams_shared lives inside hams_open)."""
    root = os.path.realpath(root)
    parent = os.path.dirname(root)
    roots = [root]
    for candidate in ("hams_com", "hams_open", os.path.join("hams_open", "hams_shared"), "hams_shared"):
        other = os.path.realpath(os.path.join(parent, candidate))
        if os.path.isdir(other) and other not in roots and repo_name(other) != repo_name(root):
            if repo_name(other) not in [repo_name(r) for r in roots]:
                roots.append(other)
    return roots


def scan(root):
    """All findings of one repository: list of (relative file, line, kind, reference)."""
    roots = sibling_roots(root)
    root = roots[0]
    files = []
    for each in roots:
        files.extend(tracked_files(each))
    know = Knowledge(files, roots)
    results = []
    for path, rel in markdown_files(root):
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        for line, kind, ref in check_markdown(text, path, know):
            results.append((rel, line, kind, ref))
    return results


def key_of(finding):
    return f"{finding[0]}|{finding[2]}|{finding[3]}"


def load_baseline(path):
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as handle:
        return set(json.load(handle))


def save_baseline(path, keys):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(sorted(keys), handle, indent=1)
        handle.write("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("directory", nargs="?", default=".")
    parser.add_argument("--baseline", help="baseline JSON file (default: alongside this script)")
    parser.add_argument("--generate-baseline", action="store_true", help="record the current findings as the baseline")
    parser.add_argument("--list", action="store_true", help="print every finding, not only the new ones")
    args = parser.parse_args(argv)
    root = os.path.realpath(check_target.require_directory(args.directory, "check_doc_references"))
    baseline_path = args.baseline or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), f"doc_references_baseline_{repo_name(root)}.json"
    )
    check_target.require_scanned(len(markdown_files(root)), root, "check_doc_references", "Markdown files")
    findings = scan(root)
    keys = {key_of(f) for f in findings}
    if args.generate_baseline:
        save_baseline(baseline_path, keys)
        print(f"[*] Baseline written: {len(keys)} broken documentation references recorded at {baseline_path}")
        return 0
    baseline = load_baseline(baseline_path)
    for finding in findings:
        if args.list or key_of(finding) not in baseline:
            print(f"{finding[0]}:{finding[1]}: {finding[2]}: {finding[3]}")
    new = sorted(keys - baseline)
    print(f"[*] check_doc_references: {len(keys)} broken references ({len(keys) - len(new)} in the baseline, {len(new)} new)")
    if new:
        print("[-] FAILED: the new references above do not exist. Fix the document (or, if the thing is meant to exist, add it).")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
