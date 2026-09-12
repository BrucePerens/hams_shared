#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
int(float(...)) without catching OverflowError gate (Python).

Born 2026-09-11 from a real bug found by adversarial review in `sota_sync.process_sota_csv()`'s
own `_int()` helper: `int(float(val))` wrapped in `try: ... except ValueError: return 0` looks
like it safely handles any malformed numeric string, but `int(float("inf"))` raises
`OverflowError`, not `ValueError` -- Python's `float()` parses "inf"/"nan"/"-infinity"
(case-insensitively) into a real, finite-looking-to-the-type-system value with no exception at
all, and it's only the SUBSEQUENT `int()` conversion of that non-finite float that fails, with
the "wrong" exception type. An `except ValueError` guard that looks complete for "any malformed
input string" silently lets an "inf"/"-inf" (but not "nan", which int() rejects with ValueError
same as always) input propagate an uncaught `OverflowError` instead.

This is mechanically certain, not a heuristic guess about trust boundaries: if `int(float(x))`
is reachable at all, and `x` can ever be text spelling "inf"/"-inf" (case-insensitively, with an
optional sign/leading "infinity"), the surrounding exception handling MUST catch OverflowError
(or the code must check `math.isfinite()` before calling `int()`) -- regardless of whether `x`
is externally-controlled input or not, this is simply what `int(float(...))`'s own real contract
requires. Confirmed as a real, live bug in this exact codebase the same night this gate was
written (`daemons/sota_sync/main.py`'s `_int()`) -- a whole CSV file's remaining rows were
silently dropped by an "inf" AltM/Points value the existing `except ValueError` did not catch.

This is a heuristic textual/AST scan, not a real dataflow analysis -- same tradeoff
check_burn_list.py's own GENERAL_ERROR_RULES already make throughout this codebase. It finds
`int(float(...))` call expressions, walks up to the nearest enclosing `try` statement, and checks
whether any of that try's own `except` handlers would catch `OverflowError` (an explicit
`OverflowError`/`ArithmeticError`/`Exception`/`BaseException`, a bare `except:`, or a tuple
containing one of those). A call with no enclosing `try` at all is flagged outright -- nothing
catches its OverflowError. False positives are possible (an `int(float(...))` call whose `x` is
provably always a real Python float already, e.g. `int(float(some_int_variable))`, can never
actually produce "inf"); suppress a specific line with a same-line
`# int-float-overflow-ignore: <reason>` comment rather than disabling the whole file.
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

_IGNORE_COMMENT = "int-float-overflow-ignore:"

_OVERFLOW_CATCHING_NAMES = {"OverflowError", "ArithmeticError", "Exception", "BaseException"}


def find_python_files(repo_root):
    found = []
    for root, dirs, filenames in os.walk(repo_root):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIR_NAMES and not d.startswith(".")]
        for fname in filenames:
            if fname.endswith(".py"):
                found.append(os.path.join(root, fname))
    return sorted(found)


def _is_int_of_float_call(node):
    """True if `node` is a Call expression shaped like `int(<expr containing a float(...) call>)`
    -- not just the exact `int(float(x))` shape. Real gap found and fixed the same night this
    gate was written: `int(float(freq) * 1000000)` (hams_local_relay's own legacy Flask relay,
    `main.py`'s `qsy()` endpoint) has the identical real bug -- `float("inf") * 1000000` is
    still `inf`, and `int()` of that still raises `OverflowError` -- but a narrower check
    requiring `float(...)` to be int()'s own DIRECT, sole argument would miss it entirely. Walks
    the whole argument subtree for any call to a name literally spelled `float` (not e.g. a
    variable named `float`, which AST alone can't rule out, but a genuine shadowing of the
    `float` builtin is exotic enough not to worry about here)."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "int"):
        return False
    if len(node.args) != 1:
        return False
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "float"
        for n in ast.walk(node.args[0])
    )


def _handler_catches_overflow(handler):
    """True if this `except ...:` clause's own type expression would catch OverflowError --
    a bare `except:`, `except OverflowError:`/`ArithmeticError:`/`Exception:`/`BaseException:`,
    or a tuple of exception types containing one of those."""
    if handler.type is None:
        return True  # bare except
    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    for t in types:
        if isinstance(t, ast.Name) and t.id in _OVERFLOW_CATCHING_NAMES:
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
    # Map every node to its direct parent, so a flagged call can walk up to its nearest
    # enclosing ast.Try (if any).
    parent_of = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parent_of[child] = parent

    findings = []
    for node in ast.walk(tree):
        if not _is_int_of_float_call(node):
            continue

        line_idx = node.lineno - 1
        if 0 <= line_idx < len(lines) and _IGNORE_COMMENT in lines[line_idx]:
            continue

        enclosing_try = None
        cur = parent_of.get(node)
        while cur is not None:
            if isinstance(cur, ast.Try):
                # Only a `try` whose OWN body (not another handler/finally/orelse) contains
                # this call counts -- walking further up from inside an except/finally block
                # would find the same Try node again spuriously.
                if any(node is d or node in ast.walk(d) for d in cur.body):
                    enclosing_try = cur
                    break
            cur = parent_of.get(cur)

        if enclosing_try is None:
            findings.append((
                node.lineno,
                "`int(float(...))` with no enclosing try/except at all -- int(float(\"inf\")) "
                "raises OverflowError (not ValueError), so this call will crash uncaught on any "
                "\"inf\"/\"-inf\"-spelled input. Wrap it and catch OverflowError alongside "
                "ValueError, or check math.isfinite() first.",
            ))
            continue

        if not any(_handler_catches_overflow(h) for h in enclosing_try.handlers):
            findings.append((
                node.lineno,
                "`int(float(...))` is guarded by a try/except that does not catch "
                "OverflowError -- int(float(\"inf\")) raises OverflowError, not ValueError, so "
                "an \"inf\"/\"-inf\"-spelled input propagates uncaught past this handler. Add "
                "OverflowError to the except clause, or check math.isfinite() before calling "
                "int().",
            ))

    return findings


def main():
    if len(sys.argv) < 2:
        print("Usage: check_int_float_overflow.py <repo_root>")
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
