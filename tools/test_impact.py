#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Test impact: the smallest `test.py -u ...` run that is still safe for a branch.

Reads the files a branch changes against origin/main, maps each to an Odoo module (a directory with a
`__manifest__.py`), widens the set to every module that depends on one of them (the reverse-dependency closure over
hams_com + hams_open), and prints the command. When only test files changed it narrows further to the test classes.

Conservative by design: it never under-tests. A change it cannot map to one module's code (a manifest, `security/`,
`data/`, `views/`, any XML or CSV outside `static/`, any file outside every module such as a hams_shared tool, a
shared library or a daemon, an unknown file type) answers "run the full module list". Only documentation-only changes
answer "no Odoo test impact". Every file prints its verdict and the reason, so a reviewer can disagree.

    python3 hams_shared/tools/test_impact.py                 # branch vs origin/main, from any repo checkout
    python3 hams_shared/tools/test_impact.py --command-only  # just the command (or a '#' line when nothing to run)
    python3 hams_shared/tools/test_impact.py --files a/models/x.py b/tests/test_y.py   # no git needed

It is a per-PR shortcut. The nightly whole-system run stays the safety net for what a targeted run cannot see.
"""

import argparse
import ast
import collections
import os
import subprocess
import sys

# [@ANCHOR: test_impact:Impact]
# The answer: what to run and why. See tools/README.md.
Impact = collections.namedtuple("Impact", "kind modules test_tags reasons")

KIND_NONE = "none"  # nothing Odoo can test changed
KIND_TARGETED = "targeted"  # these modules (the closure) only
KIND_TESTS = "tests"  # only test files changed: these modules, narrowed to test_tags where possible
KIND_FULL = "full"  # run every module

FULL_DIRS = frozenset({"security", "data", "views", "demo", "i18n", "report", "reports", "templates"})
DOC_DIRS = ("docs", "night_shift_history", "night_shift_todo", "night_shift_questions")
DOC_SUFFIXES = (".md", ".txt", ".rst")
CODE_SUFFIXES = (".py", ".js", ".mjs", ".scss", ".css", ".png", ".svg", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf", ".webp")


# [@ANCHOR: test_impact:module_dirs]
def module_dirs(roots):
    """{module name: real path} for every directory directly below each root that holds a `__manifest__.py`."""
    found = {}
    for root in roots:
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for name in names:
            path = os.path.join(root, name)
            if os.path.isfile(os.path.join(path, "__manifest__.py")):
                found.setdefault(name, os.path.realpath(path))
    return found


# [@ANCHOR: test_impact:read_depends]
def read_depends(module_path):
    """The `depends` list of a module's manifest. Raises ValueError when the manifest cannot be read or is not a literal dict:
    an unreadable manifest silently dropped every dependent edge through that module, which under-tests."""
    try:
        with open(os.path.join(module_path, "__manifest__.py"), encoding="utf-8") as handle:
            data = ast.literal_eval(handle.read())
    except (OSError, ValueError, SyntaxError) as error:
        raise ValueError("cannot read the manifest of %s: %s" % (module_path, error)) from error
    if not isinstance(data, dict):
        raise ValueError("the manifest of %s is not a dict literal" % module_path)
    return list(data.get("depends", []))


# [@ANCHOR: test_impact:read_test_depends]
def read_test_depends(module_path):
    """The `test_depends` list of a module's manifest ([] when absent): modules the module's tests need installed although its
    runtime `depends` does not list them. Raises ValueError like `read_depends`. Not `depends_cycle`, a separate runtime mechanism."""
    with open(os.path.join(module_path, "__manifest__.py"), encoding="utf-8") as handle:
        data = ast.literal_eval(handle.read())
    return list(data.get("test_depends", []))


# [@ANCHOR: test_impact:reverse_closure]
def reverse_closure(changed, modules):
    """`changed` plus every module that depends (`depends` or `test_depends`) on one of them, directly or through others. Returns a sorted list.
    Depends names that are not in `modules` (Odoo's own, such as `base`) are ignored."""
    reverse = {}
    for name, path in modules.items():
        # test_depends: a change to the companion must also run the tests that need it installed.
        for dep in read_depends(path) + read_test_depends(path):
            reverse.setdefault(dep, set()).add(name)
    result, frontier = set(changed), list(changed)
    while frontier:
        for dependent in reverse.get(frontier.pop(), ()):
            if dependent not in result:
                result.add(dependent)
                frontier.append(dependent)
    return sorted(result)


# [@ANCHOR: test_impact:classify_path]
def classify_path(rel):
    """(category, reason) for a path relative to a module directory.

    Categories: 'full' (cannot be narrowed), 'code' (module plus dependents), 'test' (a `tests/test_*.py` file),
    'doc' (documentation: the module's own tests only)."""
    parts = rel.split("/")
    name = parts[-1]
    if name == "__manifest__.py":
        return "full", "manifest change affects installation and every dependent"
    if parts[0] in FULL_DIRS:
        return "full", "%s/ is loaded data (rules, views, records) that dependents also load" % parts[0]
    if parts[0] == "tests" and len(parts) == 2 and name.startswith("test_") and name.endswith(".py"):
        return "test", "test file"
    if name.endswith(".md") or parts[0] == "docs":
        return "doc", "documentation"
    if name.endswith(".xml") and parts[0] != "static":
        return "full", "XML outside static/ is a view or data file"
    if name.endswith(CODE_SUFFIXES) or (name.endswith(".xml") and parts[0] == "static"):
        return "code", "module code or asset (tests helpers included): module and its dependents"
    return "full", "unknown file type, cannot map it"


# [@ANCHOR: test_impact:classes_in_test_file]
def classes_in_test_file(path):
    """Names of the top-level classes with a base class in a test file (the Odoo test cases); [] when the file is missing,
    unreadable or defines none, which means the caller cannot narrow to classes."""
    try:
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
    except (OSError, SyntaxError, ValueError):
        return []
    return [node.name for node in tree.body if isinstance(node, ast.ClassDef) and node.bases]


def _outside(rel):
    """(category, reason) for a changed path that belongs to no module."""
    top = rel.split("/")[0]
    # Only documentation suffixes count, also under the documentation directories: a .json, .csv or .py there may be read by a test.
    if rel.endswith(DOC_SUFFIXES) and (top in DOC_DIRS or rel.endswith(".md")):
        return "none", "documentation outside the modules (linters cover it, no Odoo test reads it)"
    return "full", "outside every Odoo module (tool, shared library, daemon, config or gitlink): any module may depend on it"


# [@ANCHOR: test_impact:analyze]
def analyze(changes, modules):
    """Decide what to run. `changes` is a list of (repo_root, relative_path) for every changed file (renames and deletes
    listed under both names); `modules` is `module_dirs`' mapping for the whole universe (hams_com and hams_open).
    Returns an `Impact`. Never returns less than the safe answer: one unmappable file makes the result 'full'."""
    reasons, full, code_modules, doc_modules, test_modules, tests = [], False, set(), set(), set(), {}
    by_path = sorted(modules.items(), key=lambda item: -len(item[1]))
    for root, rel in changes:
        absolute = os.path.realpath(os.path.join(root, rel))
        owner = next((name for name, path in by_path if absolute.startswith(path + os.sep)), None)
        if owner is None:
            category, why = _outside(rel)
            subject = rel
        else:
            category, why = classify_path(os.path.relpath(absolute, modules[owner]).replace(os.sep, "/"))
            subject = "%s: %s" % (owner, rel)
        reasons.append("%-60s -> %-4s (%s)" % (subject[:60], category, why))
        if category == "full":
            full = True
        elif category == "code":
            code_modules.add(owner)
        elif category == "doc":
            doc_modules.add(owner)
        elif category == "test":
            test_modules.add(owner)
            tests.setdefault(owner, []).append(absolute)
    if full:
        return Impact(KIND_FULL, sorted(modules), None, reasons + ["=> run the full module list: at least one change cannot be narrowed"])
    if code_modules:
        try:
            closure = reverse_closure(code_modules, modules)
        except ValueError as error:
            return Impact(KIND_FULL, sorted(modules), None, reasons + ["=> run the full module list: dependents cannot be proven (%s)" % error])
        extra = sorted((doc_modules | test_modules) - set(closure))
        everything = sorted(set(closure) | doc_modules | test_modules)
        note = "=> %s changed code; run them and their dependents: %s" % (", ".join(sorted(code_modules)), ", ".join(closure))
        if extra:
            note += " (plus the documentation/test-only modules %s, whole: tags would under-test the code modules)" % ", ".join(extra)
        return Impact(KIND_TARGETED, everything, None, reasons + [note])
    if doc_modules or test_modules:
        everything = sorted(doc_modules | test_modules)
        tags, narrow = [], not doc_modules
        for module in sorted(test_modules):
            classes = sorted({c for path in tests[module] for c in classes_in_test_file(path)})
            if not classes:
                narrow = False
                reasons.append("%s: a changed test file has no readable test class, so no --test-tags" % module)
            tags += ["/%s:%s" % (module, c) for c in classes]
        if narrow and tags:
            return Impact(KIND_TESTS, everything, tags, reasons + ["=> only test files changed: no code to propagate, run the changed classes"])
        return Impact(KIND_TESTS, everything, None, reasons + ["=> test/documentation files changed: run those modules whole"])
    return Impact(KIND_NONE, [], None, reasons + ["=> nothing Odoo can test changed (documentation only)"])


# [@ANCHOR: test_impact:build_command]
def build_command(impact):
    """The `test.py` command line for an `Impact`, or a '#' comment line when there is nothing to run."""
    if impact.kind == KIND_NONE:
        return "# no Odoo test impact: do not run test.py for this branch"
    command = "python3 hams_shared/tools/test.py --wait -u %s" % ",".join(impact.modules)
    if impact.test_tags:
        command += " --test-tags %s" % ",".join(impact.test_tags)
    return command


# [@ANCHOR: test_impact:changed_files]
def changed_files(repo, base="origin/main"):
    """Relative paths that differ from merge-base(base, HEAD) in the work tree (committed, staged, unstaged, untracked),
    renames split into delete + add. Raises RuntimeError when `repo` is not a git checkout or has no `base`."""
    def git(*args):
        done = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
        if done.returncode != 0:
            raise RuntimeError("git %s failed in %s: %s" % (" ".join(args), repo, done.stderr.strip()))
        return [line for line in done.stdout.splitlines() if line]
    merge_base = git("merge-base", base, "HEAD")[0]
    return sorted(set(git("diff", "--name-only", "--no-renames", merge_base) + git("ls-files", "--others", "--exclude-standard")))


def default_roots(repo):
    """The module universe: `repo`, its sibling hams_com / hams_open, else the main checkouts under ~/workspace."""
    parent = os.path.dirname(repo)
    roots = [repo]
    for name in ("hams_com", "hams_open"):
        for candidate in (os.path.join(parent, name), os.path.expanduser("~/workspace/" + name)):
            if os.path.isdir(candidate) and os.path.realpath(candidate) != os.path.realpath(repo):
                roots.append(candidate)
                break
    return roots


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=".", help="the checkout whose branch is analysed (default: the current directory)")
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--root", action="append", help="a directory of modules for the dependency graph (repeatable; default: the repo and its siblings)")
    parser.add_argument("--files", nargs="+", help="analyse these repo-relative paths instead of asking git")
    parser.add_argument("--command-only", action="store_true")
    args = parser.parse_args(argv)
    top = subprocess.run(["git", "-C", args.repo, "rev-parse", "--show-toplevel"], capture_output=True, text=True).stdout.strip()
    repo = os.path.realpath(top or args.repo)
    try:
        files = args.files if args.files else changed_files(repo, args.base)
    except RuntimeError as error:
        print("test_impact: %s" % error, file=sys.stderr)
        return 2
    modules = module_dirs(args.root or default_roots(repo))
    impact = analyze([(repo, rel) for rel in files], modules)
    if args.command_only:
        print(build_command(impact))
        return 0
    print("test_impact: %d changed file(s) in %s against %s" % (len(files), repo, args.base))
    print("\n".join(impact.reasons))
    print("\n" + build_command(impact))
    print("(per-PR shortcut: the nightly whole-system run stays the safety net)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
