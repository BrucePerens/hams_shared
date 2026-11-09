#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Which test files of a `pytest` lint step a `--changed` run must run: the test files that, directly or through other files, mention a changed file.

The `pytest tools/[NofM]`, `pytest scripts/` and `pytest daemons/*` steps ran every test file under their root whenever any file under it changed (the
four `tools/` chunks took 150 to 280 s each, 2026-11-08). Under `--changed` the runner now keeps only the test files that depend on a changed file. Same
premise as the step selection of `--changed`: the base commit passes, so a test whose inputs did not change still passes.

The dependency is textual and therefore generous: a file depends on another when its text holds the other's module name (`import x`, `from x import`, a
path string, a subprocess call of `x.py`) or, for any other file, its file name or its top directory's name. Files depend on each other transitively. A
test file that scans a directory by itself (`glob`, `os.walk`, `listdir`, `git ls-files` on `__file__`, `HERE` or the tree root) is selected whenever
anything under the root changed. Conservative: everything is selected when anything cannot be decided (a `conftest.py`, `__init__.py`, `requirements*`
or pytest configuration file changed; a changed file outside the root; a changed file that nothing mentions, such as a new script with no test; no
changed path at all). The runner prints which test files were selected and which skipped, and the step cannot lose a file by accident: it also takes
`HAMS_LINT_TEST_IMPACT=0` to select everything.
"""

import ast
import io
import os
import re
import tokenize

PRUNED_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv", "venv", "target", ".mypy_cache", ".pytest_cache", ".tox"})
MAX_BYTES = 4_000_000  # a larger file is not read: it is treated as mentioning nothing (a data file, not code)
# Files whose change can alter every test: pytest's own configuration and the packaging of the root.
GLOBAL_NAMES = frozenset({"conftest.py", "__init__.py", "pytest.ini", "setup.cfg", "pyproject.toml", "tox.ini", "setup.py", ".coveragerc"})
_SCAN = re.compile(r"glob\.i?glob|\.i?glob\(|listdir|os\.walk|iterdir|rglob|scandir|ls-files")
_ANCHOR = re.compile(r"__file__|\bHERE\b|TOOLS|REPO_|ROOT|SHARED|\"\.\.\"|walk\(\"\.\"\)|walk\('\.'\)")
_SCRIPT = re.compile(r"(\w+)\.py\b")
_TOKEN = re.compile(r"\w+")
_NAME_TOKEN = re.compile(r"\w[\w.\-]*\w")


def _under(path, directory):
    return path == directory or path.startswith(directory.rstrip(os.sep) + os.sep)


def _tokens(text):
    """Every word of `text` and every dotted or hyphenated name in it (`README.md`, `hams-gate-canary.service`)."""
    return set(_TOKEN.findall(text)) | set(_NAME_TOKEN.findall(text))


def _code_text(text):
    """`text` of a Python file without its comments and docstrings (a word a file mentions only in prose is not a dependency); the whole text when it
    does not tokenize. Pure."""
    try:
        tokens = [token for token in tokenize.generate_tokens(io.StringIO(text).readline) if token.type not in (tokenize.COMMENT, tokenize.NL)]
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return text
    starts = (tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT)
    kept = []
    for index, token in enumerate(tokens):
        if token.type == tokenize.STRING:
            at_start = index == 0 or tokens[index - 1].type in starts
            ends = index + 1 >= len(tokens) or tokens[index + 1].type in (tokenize.NEWLINE, tokenize.ENDMARKER)
            if at_start and ends:
                continue  # a statement that is only a string: a docstring
        kept.append(token.string)
    return " ".join(kept)


def _imports(text):
    """The module names a Python file imports, whole and by part (`from a.b import c` gives a, b, c, a.b). Empty when it does not parse. Pure."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return set()
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.update(alias.name.split("."))
        elif isinstance(node, ast.ImportFrom):
            names.update((node.module or "").split("."))
            names.update(alias.name for alias in node.names)
    names.discard("")
    return names


def _scans_a_tree(text):
    """True when `text` has a directory scan that names `__file__`, `HERE`, a tree root or `..` on the same line or the two lines before it (a scan of a
    scratch directory the test made is not one). Pure."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if _SCAN.search(line) and not line.lstrip().startswith(("#", '"""')):
            if _ANCHOR.search(" ".join(lines[max(0, index - 2):index + 1])):
                return True
    return False


def _read(path):
    try:
        if os.path.getsize(path) > MAX_BYTES:
            return ""
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def _python_files(root):
    found = []
    for directory, dirs, names in os.walk(root):
        dirs[:] = [name for name in dirs if name not in PRUNED_DIRS]
        found.extend(os.path.join(directory, name) for name in names if name.endswith(".py"))
    return sorted(found)


def _keys(path, root):
    """The tokens a mention of changed file `path` consists of: its file name, its module name for a `.py` file, its top directory below `root`."""
    name = os.path.basename(path)
    keys = {name}
    if name.endswith(".py"):
        keys.add(name[:-3])
    relative = os.path.relpath(path, root)
    if os.sep in relative:
        keys.add(relative.split(os.sep)[0])
    return keys


# [@ANCHOR: pytest_impact:select_tests]
def select_tests(root, test_files, changed, also=()):
    """(selected, skipped, why): `test_files` split by whether a change in `changed` (absolute resolved paths; only those under `root` or `also`, the step's other input paths, count) can alter its result. `selected` and `skipped`
    keep the order of `test_files`; `why` is a one-line account for the step output. Everything is selected (with the reason in `why`) when `changed` has no path
    under `root`, a changed path of `also`, a pytest configuration or package file, or a path that no file mentions (a new script, a deleted one, a data file of
    no test). A test file that is itself changed is selected. Reads every `.py` file under `root` once. Full entry: tools/README.md."""
    root = os.path.realpath(root)
    files = list(test_files)

    def everything(reason):
        return files, [], reason

    if os.environ.get("HAMS_LINT_TEST_IMPACT") == "0":
        return everything("HAMS_LINT_TEST_IMPACT=0")
    if not files:
        return [], [], "no test files"
    others = [os.path.realpath(path) for path in also]
    changed = sorted({os.path.realpath(path) for path in (changed or ())})
    inside = [path for path in changed if _under(path, root)]
    if not inside:
        return everything("no changed file under the step's root")
    if any(path in others or any(_under(path, other) for other in others) for path in changed if path not in inside):
        return everything("a changed input of the step lies outside the root")
    glob_trigger = next((path for path in inside if os.path.basename(path) in GLOBAL_NAMES or os.path.basename(path).startswith("requirements")), None)
    if glob_trigger is not None:
        return everything("%s changed" % os.path.relpath(glob_trigger, root))
    texts = {path: _read(path) for path in _python_files(root)}
    for path in files:
        texts.setdefault(os.path.realpath(path), _read(path))
    nodes = {path: _tokens(_code_text(text)) for path, text in texts.items()}
    # What a file depends on, for the steps after the first: the modules it imports and the scripts it names (`x.py`). The first step, from a changed
    # file to the files that name it, also takes a bare word (a module name inside a path join, a data file's name).
    refs = {path: _imports(text) | set(_SCRIPT.findall(_code_text(text))) for path, text in texts.items()}
    real_tests = {os.path.realpath(path): path for path in files}
    selected = set()
    for path in inside:
        is_test = path in real_tests
        if os.path.basename(path).startswith("test_") and not os.path.exists(path):
            continue  # a deleted test file leaves nothing to run
        keys = _keys(path, root)
        mentioners = [node for node, tokens in nodes.items() if node != path and keys & tokens]
        if not mentioners and not is_test:
            return everything("%s is mentioned by no file under the root" % os.path.relpath(path, root))
        if is_test:
            selected.add(path)
        selected.update(mentioners)
    # transitive: a file that mentions a selected module is selected too
    queue = [node for node in selected if node.endswith(".py")]
    while queue:
        node = queue.pop()
        stem = os.path.basename(node)[:-3]
        for other in refs:
            if other not in selected and stem in refs[other]:
                selected.add(other)
                queue.append(other)
    scanners = {path for path in real_tests if _scans_a_tree(_read(path))}
    chosen = [path for path in files if os.path.realpath(path) in selected or os.path.realpath(path) in scanners]
    skipped = [path for path in files if path not in chosen]
    return chosen, skipped, "%d of %d test files depend on a changed file (%d scan the tree themselves)" % (len(chosen), len(files), len(scanners))


def describe(step_name, root, selected, skipped, why):
    """The note a run prints for one step: counts, the reason, the skipped files (names only). Pure."""
    names = ", ".join(os.path.relpath(path, root) for path in skipped)
    line = "[*] pytest impact, %s: %s" % (step_name, why)
    if skipped:
        line += "\n    skipped %d: %s" % (len(skipped), names)
    return line
