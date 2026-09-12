#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
float()-parses-nan/inf-silently gate (Python).

Born 2026-09-11/12 from a single night's bug-hunt sweep that found and fixed roughly fifteen
separate real instances of the same bug class across hams_com: Python's `float(x)` happily
parses "nan"/"inf"/"-infinity" (case-insensitively) into a real, non-finite float with NO
exception at all -- so a `try: float(x) except ValueError: ...` block, which reads as "safely
parse this untrusted text," silently lets a non-finite value straight through. Depending on the
call site this then either (a) gets stored as-is, silently poisoning any later comparison, sort,
or aggregation over that value (NaN != NaN; a NaN/Inf coordinate breaks distance math), (b)
crashes downstream with a real, uncaught, unrelated-looking exception (e.g. `int(float("inf"))`
raising `OverflowError`, or a Postgres `numeric(N, M)` column raising "numeric field overflow"
for Infinity, confirmed live via psql this same night), or (c) silently returns wrong results
from a library call that itself tolerates a garbage angle/value without raising (e.g. `ephem`
accepting a "nan"-valued Observer.lat and returning a nonsense angle instead of erroring).

This is a heuristic textual/AST scan, not real dataflow analysis -- same tradeoff every other
`check_*.py` gate in this directory already makes. It flags a `float(...)` call that sits inside
a `try` block whose own handlers would catch `ValueError` (the standard "this might not parse"
guard), when the ENCLOSING FUNCTION's body contains no `math.isfinite(...)` call anywhere. That
last condition is deliberately coarse (function-wide, not tied to the specific value) to keep
false positives low: if the function already checks finiteness for ANY value, on the reasonable
assumption it's doing so for the value(s) that need it, this gate stays silent. A `float(...)`
call with no enclosing `try` at all is a DIFFERENT, usually more severe shape (no defensive
parsing intended at all, e.g. `ham_shack.res_users.action_broadcast_cq()`'s
`float(frequency)` before this same night's fix) and is deliberately NOT flagged by this gate --
grep for bare `float(` calls by hand for that; folding it in here would flood this narrowly-scoped
check with unrelated internal-computation call sites that were never meant to reject malformed
input in the first place.

Escape hatch: a same-line `# float-isfinite-ignore: <reason>` comment, e.g. for a `float(...)`
call whose value is provably always finite already (a re-parse of an int, or a value already
validated by a caller), or where the non-finite case is genuinely, deliberately meant to
propagate (rare, but real dataflow analysis can't tell from AST alone).
"""

import ast
import os
import sys

IGNORE_DIR_NAMES = {
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    "target",
    ".git",
    "worktrees",
}

_IGNORE_COMMENT = "float-isfinite-ignore:"

_VALUEERROR_CATCHING_NAMES = {"ValueError", "Exception", "BaseException"}


def find_python_files(repo_root):
    found = []
    for root, dirs, filenames in os.walk(repo_root):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIR_NAMES and not d.startswith(".")]
        for fname in filenames:
            if fname.endswith(".py"):
                found.append(os.path.join(root, fname))
    return sorted(found)


def _is_float_call(node):
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "float"


def _is_isfinite_call(node):
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "isfinite":
        return True
    if isinstance(func, ast.Name) and func.id == "isfinite":
        return True
    return False


def _handler_catches_valueerror(handler):
    """True if this `except ...:` clause's own type expression would catch ValueError -- a bare
    `except:`, `except ValueError:`/`Exception:`/`BaseException:`, or a tuple of exception types
    containing one of those."""
    if handler.type is None:
        return True  # bare except
    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    for t in types:
        if isinstance(t, ast.Name) and t.id in _VALUEERROR_CATCHING_NAMES:
            return True
    return False


def check_file(path):
    """Returns a list of (line_num, message) findings for one .py file."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        source = f.read()
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError:
        return []

    lines = source.splitlines()
    parent_of = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parent_of[child] = parent

    def enclosing_function(node):
        cur = parent_of.get(node)
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return cur
            cur = parent_of.get(cur)
        return None

    # Cache: does this function (by id) already call math.isfinite anywhere in its body?
    isfinite_functions = set()
    for node in ast.walk(tree):
        if _is_isfinite_call(node):
            func = enclosing_function(node)
            if func is not None:
                isfinite_functions.add(id(func))

    findings = []
    for node in ast.walk(tree):
        if not _is_float_call(node):
            continue

        line_idx = node.lineno - 1
        if 0 <= line_idx < len(lines) and _IGNORE_COMMENT in lines[line_idx]:
            continue

        enclosing_try = None
        cur = parent_of.get(node)
        while cur is not None:
            if isinstance(cur, ast.Try):
                if any(node is d or node in ast.walk(d) for d in cur.body):
                    enclosing_try = cur
                    break
            cur = parent_of.get(cur)

        if enclosing_try is None:
            continue  # a bare float() with no try/except is a different, out-of-scope shape

        if not any(_handler_catches_valueerror(h) for h in enclosing_try.handlers):
            continue  # this try isn't guarding against a parse failure at all

        func = enclosing_function(node)
        if func is not None and id(func) in isfinite_functions:
            continue  # the enclosing function already checks finiteness somewhere

        findings.append((
            node.lineno,
            "`float(...)` inside a try/except ValueError with no math.isfinite() check anywhere "
            "in the enclosing function -- float(\"nan\")/float(\"inf\")/float(\"-infinity\") "
            "parse successfully with NO exception, so this \"safely parse\" guard silently lets "
            "a non-finite value through. Add `if not math.isfinite(result): ...` (reject or "
            "coerce, matching how this function already handles other invalid input).",
        ))

    return findings


def main():
    if len(sys.argv) < 2:
        print("Usage: check_float_isfinite.py <repo_root>")
        sys.exit(1)

    repo_root = os.path.abspath(sys.argv[1])
    if os.path.basename(repo_root) == "hams_shared":
        repo_root = os.path.dirname(repo_root)

    any_found = False
    for path in find_python_files(repo_root):
        for line_num, msg in check_file(path):
            any_found = True
            rel = os.path.relpath(path, repo_root)
            print(f"❌ {rel}:{line_num}: {msg}")

    if any_found:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
