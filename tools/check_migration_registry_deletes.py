#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Migration deletes decided by registry / model-existence gate (Python).

Born from a real incident (2026-11-21): hams_com ham_base 0.13's post-migration deleted 13,241 live chatter rows
because it treated "model not in env.registry" as "model removed". During `-u`, a module's pre/post migration runs while
the modules loaded after it (its dependants) are not yet in the registry, so "absent from the registry" says nothing
about whether the model exists. The same trap applies to ir_model rows, to_regclass(...) and information_schema lookups
when they decide to delete or unlink data.

A file under a `migrations/<version>/` directory is flagged when a DELETE FROM / DROP TABLE / DROP COLUMN / TRUNCATE
statement, or an `.unlink()` / `.delete()` call, sits inside an `if` / `while` / conditional expression / comprehension
filter whose condition (or a variable assigned from such a lookup, or a cr.fetchone()/fetchall() right after a
lookup execute) uses: `registry`, membership in `env` (`x in env`, `env.get(...)`, `env[...]`), `ir_model` / `ir.model`
text, `to_regclass`, `information_schema`, or a helper named `*_exists`.

Heuristic AST scan, like the other check_* gates. Escape hatch: `# migration-registry-ok: <reason>` on the line of the
delete or on the line of the condition, for a reviewed case (for example "the table is this module's own, created by
its own earlier migration").
"""

import ast
import os
import re
import sys

import check_target
import lint_cache
from source_text import read_source  # noqa: E402
from tree_files import tree_files

IGNORE_DIR_NAMES = {"__pycache__", "node_modules", ".venv", "venv", "target", ".git", "worktrees", "site-packages"}
_IGNORE_COMMENT = "migration-registry-ok:"
_DELETE_SQL = re.compile(r"\b(DELETE\s+FROM|DROP\s+TABLE|DROP\s+COLUMN|TRUNCATE)\b", re.IGNORECASE)
_TOKEN_TEXT = re.compile(r"ir_model\b|ir\.model\b|to_regclass|information_schema", re.IGNORECASE)
_EXISTS_NAME = re.compile(r"_exists?$|^exists?_|^has_(table|column|model)")


def find_python_files(repo_root):
    return tree_files(
        repo_root, ".py", IGNORE_DIR_NAMES, skip_hidden=True, accept=lambda rel: "/migrations/" in "/" + rel.replace(os.sep, "/")
    )


def _strings(node):
    return [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _is_env(node):
    return (isinstance(node, ast.Name) and node.id == "env") or (isinstance(node, ast.Attribute) and node.attr == "env")


def _uses_token(node):
    """True if the expression looks at the registry / model existence."""
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and (n.id == "registry" or _EXISTS_NAME.search(n.id)):
            return True
        if isinstance(n, ast.Attribute) and (n.attr == "registry" or _EXISTS_NAME.search(n.attr)):
            return True
        if isinstance(n, ast.Compare) and any(_is_env(c) for c in n.comparators) and any(
            isinstance(o, (ast.In, ast.NotIn)) for o in n.ops
        ):
            return True
        if isinstance(n, ast.Subscript) and _is_env(n.value):
            return True
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "get" and _is_env(n.func.value):
            return True
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and _TOKEN_TEXT.search(n.value):
            return True
    return False


def _is_fetch(node):
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ("fetchone", "fetchall", "fetchmany")
        for n in ast.walk(node)
    )


def _is_delete(node):
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Attribute) and node.func.attr in ("unlink", "delete"):
        return True
    return any(_DELETE_SQL.search(s) for a in [*node.args, *[k.value for k in node.keywords]] for s in _strings(a))


def _names(node):
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _check_function(func, lines):
    """Findings for one function: deletes under a registry/existence condition, tracking taint in source order."""
    tainted = set()
    findings = []
    state = {"lookup": False}  # the last cr.execute looked at registry/ir_model/table existence

    def ignored(*linenos):
        return any(0 < n <= len(lines) and _IGNORE_COMMENT in lines[n - 1] for n in linenos)

    def cond_bad(test):
        return _uses_token(test) or bool(_names(test) & tainted) or (_is_fetch(test) and state["lookup"])

    def visit_stmt(stmt, guards):
        if isinstance(stmt, (ast.If, ast.While)):
            bad = cond_bad(stmt.test)
            inner = guards + ([stmt.test.lineno] if bad else [])
            for child in stmt.body + stmt.orelse:
                visit_stmt(child, inner)
            return
        if isinstance(stmt, (ast.For, ast.AsyncFor)):
            for child in stmt.body + stmt.orelse:
                visit_stmt(child, guards)
            return
        if isinstance(stmt, (ast.With, ast.AsyncWith)):
            for child in stmt.body:
                visit_stmt(child, guards)
            return
        if isinstance(stmt, ast.Try) or type(stmt).__name__ == "TryStar":
            for child in stmt.body + stmt.orelse + stmt.finalbody + [c for h in stmt.handlers for c in h.body]:
                visit_stmt(child, guards)
            return
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return
        # Simple statement: update lookup/taint state, then look for deletes.
        for node in ast.walk(stmt):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "execute"
                and node.args
            ):
                state["lookup"] = _uses_token(node.args[0]) and not _is_delete(node)
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and stmt.value is not None:
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            if _uses_token(stmt.value) or bool(_names(stmt.value) & tainted) or (_is_fetch(stmt.value) and state["lookup"]):
                for t in targets:
                    tainted.update(_names(t))
        for node in ast.walk(stmt):
            conds = list(guards)
            if isinstance(node, ast.IfExp) and cond_bad(node.test):
                conds.append(node.test.lineno)
            if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
                for gen in node.generators:
                    conds += [i.lineno for i in gen.ifs if cond_bad(i)]
            if _is_delete(node) and conds and not ignored(node.lineno, *conds):
                findings.append(
                    (
                        node.lineno,
                        f"delete/unlink decided by a registry or model-existence condition (line {conds[0]}). During `-u` a "
                        "module's migration runs before its dependants are in env.registry, so \"absent\" does not mean "
                        "\"removed\" (ham_base 0.13 deleted 13,241 live chatter rows this way, 2026-11-21). Delete by an "
                        "explicit list of records or xml ids instead; if reviewed and safe add "
                        "`# migration-registry-ok: <reason>` on this line.",
                    )
                )

    for stmt in func.body:
        visit_stmt(stmt, [])
    return findings


def check_file(path):
    """Returns a list of (line_num, message) findings for one migration .py file."""
    source, problem = read_source(path)
    if problem:
        return [(0, problem)]
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError:
        return []
    lines = source.splitlines()
    findings = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            findings += _check_function(node, lines)
    return sorted(set(findings))


def main():
    if len(sys.argv) < 2:
        print("Usage: check_migration_registry_deletes.py <repo_root> [<repo_root> ...]")
        sys.exit(1)
    roots = []
    for given in sys.argv[1:]:
        check_target.require_directory(given, "check_migration_registry_deletes")
        repo_root = os.path.abspath(given)
        if os.path.basename(repo_root) == "hams_shared":
            repo_root = os.path.dirname(repo_root)
        if repo_root not in roots:
            roots.append(repo_root)

    cache = lint_cache.ResultCache(
        "check_migration_registry_deletes", lint_cache.checker_version(os.path.abspath(__file__))
    )
    any_found = False
    scanned = 0
    for repo_root in roots:
        for path in find_python_files(repo_root):
            scanned += 1
            for line_num, msg in lint_cache.cached_file_result(cache, path, check_file):
                any_found = True
                rel = os.path.relpath(path, repo_root)
                if len(roots) > 1:
                    rel = os.path.join(os.path.basename(repo_root), rel)
                print(f"❌ {rel}:{line_num}: {msg}")
    cache.save()
    check_target.require_scanned(scanned, ", ".join(roots), "check_migration_registry_deletes", "migration files")
    sys.exit(1 if any_found else 0)


if __name__ == "__main__":
    main()
