#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Wall-clock-in-tests gate (Python, JS/Hoot, Rust).

Bruce's policy (2026-11-16): no test may depend on wall-clock timing or on system load. A test that
asserts on elapsed real time (`time.monotonic() - t0 < 2`) passes on an idle box and fails under load, which is
how the whole-system run kept flaking. Measure complexity with `time.process_time()` (CPU time), counters or
virtual time instead (`tokio::time::pause`, jest/Hoot fake timers, an injected clock).

This gate FAILS when a test file asserts on an elapsed wall-clock value:
  Python: a difference of time.time/monotonic/perf_counter(_ns)/datetime.now/utcnow readings (or a variable holding
          one) inside an `assert` or a `self.assert*` call, directly or through a variable derived from it.
  JS:     a Date.now()/performance.now() delta inside expect(...)/assert...(...), directly or through a variable.
  Rust:   `.elapsed()` / `.duration_since(...)` / an Instant difference inside assert!/assert_eq!/assert_ne!, directly
          or through a variable. A file that uses tokio's paused clock (`tokio::time::pause`, `start_paused`) is skipped:
          its elapsed() is virtual time and deterministic.
`time.process_time()` is CPU time and is allowed. An assertion inside a polling loop (`while !done() { assert!(start.elapsed() < LIMIT) }`) is a timeout guard,
not a measurement: allowed. A lower-bound-only comparison (`elapsed >= 0.5` after a real sleep) is
allowed too: a monotonic clock never runs short under load.

Allowlist (tools/wallclock_in_tests_allowlist.txt): one `<repo>/<path>  # reason` per line, for a file where a
wall-clock assertion is legitimate. Ratchet baseline (tools/wallclock_in_tests_baseline.json): findings that exist
today as `{"<repo>/<path>": count}`. A file above its baseline count fails; a file at or below it only prints a
warning. The baseline only ever shrinks: delete an entry (or lower it) when a file is converted.

Usage: check_wallclock_in_tests.py <repo_root> [<repo_root> ...]  (--list-baseline prints a fresh baseline as JSON)
"""

import ast
import json
import os
import re
import sys

import check_target
from tree_files import tree_files

IGNORE_DIR_NAMES = {"__pycache__", "node_modules", ".venv", "venv", "target", ".git", "worktrees", "site-packages"}

_TOOLS = os.path.dirname(os.path.abspath(__file__))
ALLOWLIST_PATH = os.path.join(_TOOLS, "wallclock_in_tests_allowlist.txt")
BASELINE_PATH = os.path.join(_TOOLS, "wallclock_in_tests_baseline.json")

_PY_CLOCK_ATTRS = {"time", "monotonic", "perf_counter", "time_ns", "monotonic_ns", "perf_counter_ns", "now", "utcnow"}
_PY_CLOCK_NAMES = {"monotonic", "perf_counter", "time_ns", "monotonic_ns", "perf_counter_ns"}


def _is_clock_call(node):
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in _PY_CLOCK_ATTRS:
        if func.attr == "time":
            return isinstance(func.value, ast.Name) and func.value.id == "time"
        if func.attr in ("now", "utcnow"):  # not `clock.now()` of a virtual clock
            return ast.unparse(func.value).split(".")[-1] in ("datetime", "dt", "Datetime")
        return True
    return isinstance(func, ast.Name) and func.id in _PY_CLOCK_NAMES


def _key(node):
    if isinstance(node, (ast.Name, ast.Attribute)):
        return ast.unparse(node)
    return None


def _scope_nodes(scope):
    """Nodes of one function (or module) body, not descending into nested functions."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(node))


def _has_clock_sub(expr, clock_vars):
    """True when expr holds `reading - reading`: both sides a clock call or a variable holding one. `now - timedelta(1)`
    is a shifted timestamp (test data), not an elapsed time."""
    def clocky(node):
        if _is_clock_call(node):
            return True
        if isinstance(node, ast.Name):
            return node.id in clock_vars
        if isinstance(node, ast.Attribute):  # `record.field` of a variable is not a reading of that variable
            return ast.unparse(node) in clock_vars or (not isinstance(node.value, ast.Name) and clocky(node.value))
        return any(clocky(child) for child in ast.iter_child_nodes(node))

    return any(isinstance(n, ast.BinOp) and isinstance(n.op, ast.Sub) and clocky(n.left) and clocky(n.right)
               for n in ast.walk(expr))


def _lower_bound_only(node, elapsed_vars, clock_vars):
    """True when every comparison of an elapsed value in the assertion is a lower bound (`elapsed >= 0.5` after a real
    sleep): that holds on a loaded box too (a monotonic clock never runs short), so it is not load-dependent."""
    def has(expr):
        return _has_clock_sub(expr, clock_vars) or bool({_key(n) for n in ast.walk(expr)} & elapsed_vars)
    seen = False
    if isinstance(node, ast.Call):
        name = node.func.attr
        if name in ("assertGreater", "assertGreaterEqual") and len(node.args) >= 2 and not has(node.args[1]):
            return True
        if name in ("assertLess", "assertLessEqual") and len(node.args) >= 2 and not has(node.args[0]):
            return True
        return False
    for cmp in (n for n in ast.walk(node) if isinstance(n, ast.Compare)):
        operands = [cmp.left, *cmp.comparators]
        for i, op in enumerate(cmp.ops):
            left, right = operands[i], operands[i + 1]
            if has(left) or has(right):
                seen = True
                ok = (has(left) and isinstance(op, (ast.Gt, ast.GtE)) and not has(right)) or \
                     (has(right) and isinstance(op, (ast.Lt, ast.LtE)) and not has(left))
                if not ok:
                    return False
    return seen


def _py_scope_findings(scope):
    nodes = list(_scope_nodes(scope))
    in_loop = {id(n) for loop in nodes if isinstance(loop, (ast.While, ast.For)) for n in ast.walk(loop)}
    clock_vars, elapsed_vars = set(), set()
    assigns = []
    for node in nodes:
        if isinstance(node, ast.Assign):
            assigns.append(([_key(t) for t in node.targets], node.value))
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and node.value is not None:
            assigns.append(([_key(node.target)], node.value))
        elif isinstance(node, ast.NamedExpr):
            assigns.append(([_key(node.target)], node.value))
    for _ in range(3):  # a few passes settle chains (t0 -> dt -> ms)
        for targets, value in assigns:
            names = {_key(n) for n in ast.walk(value)} - {None}
            if any(_is_clock_call(n) for n in ast.walk(value)):
                clock_vars.update(t for t in targets if t)
            if _has_clock_sub(value, clock_vars) or names & elapsed_vars:
                elapsed_vars.update(t for t in targets if t)
    clock_vars -= elapsed_vars  # a reading that is also a difference counts as the difference
    found = []
    for node in nodes:
        if id(node) in in_loop:
            continue  # a deadline inside a polling loop is a timeout guard, not an assertion on elapsed time
        if isinstance(node, ast.Assert):
            expr = node
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr.startswith("assert")):
            expr = node
        else:
            continue
        keys = {_key(n) for n in ast.walk(expr)} - {None}
        if (keys & elapsed_vars or _has_clock_sub(expr, clock_vars)) \
                and not _lower_bound_only(node, elapsed_vars, clock_vars):
            found.append(node.lineno)
    return found


def check_python(source):
    if not re.search(r"time|now\(", source):
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    if not any(_is_clock_call(n) for n in ast.walk(tree)):
        return []
    scopes = [tree] + [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    lines = set()
    for scope in scopes:
        lines.update(_py_scope_findings(scope))
    return sorted(lines)


_JS_CLOCK = r"(?:Date\.now\(\)|performance\.now\(\)|new Date\(\)\.getTime\(\)|process\.hrtime[^)]*\))"
_JS_ASSIGN = re.compile(r"\s*(?:(?:const|let|var)\s+)?([A-Za-z_$][\w$.]*)\s*=\s*([^;=][^;]*)")
_JS_ASSERT = re.compile(r"\b(?:expect|assert(?:\.\w+)?|QUnit\.assert\.\w+)\s*\(")


def _statement_at(lines, start):
    """Text of the statement starting at lines[start], joined until its parentheses balance (max 8 lines)."""
    text = ""
    for line in lines[start:start + 8]:
        text += line + "\n"
        if text.count("(") <= text.count(")"):
            break
    return text


def _generic_taint(lines, clock_re, sub_re_for, assign_re):
    """Shared by JS and Rust: returns the set of variable names holding a clock difference."""
    clock_vars, elapsed_vars = set(), set()
    for _ in range(3):
        for line in lines:
            m = assign_re.match(line) if len(line) < 400 else None
            if not m:
                continue
            name, rhs = m.group(1), m.group(2)
            if re.search(r"\b(?:%s)\b" % "|".join(map(re.escape, elapsed_vars)) if elapsed_vars else r"(?!x)x", rhs):
                elapsed_vars.add(name)
            elif sub_re_for(clock_vars).search(rhs):
                elapsed_vars.add(name)
            elif clock_re.search(rhs):
                clock_vars.add(name)
    return clock_vars, elapsed_vars


def check_js(source):
    lines = source.splitlines()
    clock_re = re.compile(_JS_CLOCK)

    def sub_re_for(clock_vars):
        names = "|".join(map(re.escape, clock_vars)) if clock_vars else r"(?!x)x"
        reading = "(?:%s|%s)" % (_JS_CLOCK, names)
        return re.compile(r"%s\s*-\s*%s" % (reading, reading))

    clock_vars, elapsed_vars = _generic_taint(lines, clock_re, sub_re_for, _JS_ASSIGN)
    found = []
    for i, line in enumerate(lines):
        if not _JS_ASSERT.search(line):
            continue
        stmt = _statement_at(lines, i)
        direct = sub_re_for(clock_vars).search(stmt)
        via = elapsed_vars and re.search(r"\b(?:%s)\b" % "|".join(map(re.escape, elapsed_vars)), stmt)
        lower = re.search(r"toBeGreaterThan|>=?", stmt) and not re.search(r"toBeLessThan|[^=-]<=?\s|\bapprox", stmt)
        if (direct or via) and not lower:
            found.append(i + 1)
    return found


_RS_ASSIGN = re.compile(r"let\s+(?:mut\s+)?([A-Za-z_]\w*)(?:\s*:[^=]+)?\s*=\s*([^;]+)")
_RS_CLOCK = r"(?:Instant::now\(\)|SystemTime::now\(\))"
_RS_ELAPSED = r"\.elapsed\(\)|\.duration_since\(|\.signed_duration_since\("
_RS_ASSERT = re.compile(r"\b(?:debug_)?assert(?:_eq|_ne)?!\s*\(")


def _macro_at(lines, start):
    text = ""
    for line in lines[start:start + 12]:
        text += line + "\n"
        if text.count("(") <= text.count(")"):
            break
    return text


def check_rust(source):
    if "tokio::time::pause" in source or "start_paused" in source:
        return []  # paused tokio clock: elapsed() is virtual time
    lines = source.splitlines()
    starts = [i for i, line in enumerate(lines) if re.match(r"\s*(?:pub(?:\([a-z]+\))?\s+)?(?:async\s+)?fn\s", line)]
    found = []
    for begin, end in zip([0, *starts], [*starts, len(lines)]):  # variables are scoped per function
        found += [begin + n for n in _check_rust_block(lines[begin:end])]
    return found


def _rust_in_loop(lines, i):
    """True when line i sits inside a `while`/`loop`/`for` block of this function (found by indentation)."""
    indent = len(lines[i]) - len(lines[i].lstrip())
    for j in range(i - 1, -1, -1):
        text = lines[j].strip()
        if not text:
            continue
        here = len(lines[j]) - len(lines[j].lstrip())
        if here < indent:
            if re.match(r"(?:'\w+:\s*)?(?:while|loop|for)\b", text):
                return True
            indent = here
    return False


def _check_rust_block(lines):
    clock_vars, elapsed_vars = set(), set()
    for _ in range(3):
        for line in lines:
            m = _RS_ASSIGN.search(line) if len(line) < 400 else None
            if not m:
                continue
            name, rhs = m.group(1), m.group(2)
            uses_elapsed_var = elapsed_vars and re.search(r"\b(?:%s)\b" % "|".join(map(re.escape, elapsed_vars)), rhs)
            diff_of_clock = clock_vars and re.search(r"\b(?:%s)\b\s*-|-\s*\b(?:%s)\b" % (
                "|".join(map(re.escape, clock_vars)), "|".join(map(re.escape, clock_vars))), rhs)
            if re.search(_RS_ELAPSED, rhs) or uses_elapsed_var or diff_of_clock or re.search(_RS_CLOCK + r"\s*-", rhs):
                elapsed_vars.add(name)
            elif re.search(_RS_CLOCK, rhs):
                clock_vars.add(name)
    found = []
    for i, line in enumerate(lines):
        if not _RS_ASSERT.search(line) or _rust_in_loop(lines, i):
            continue  # a deadline inside a polling loop is a timeout guard, not an assertion on elapsed time
        stmt = _macro_at(lines, i)
        via = elapsed_vars and re.search(r"\b(?:%s)\b" % "|".join(map(re.escape, elapsed_vars)), stmt)
        lower = re.search(r"\s>=?\s", stmt) and not re.search(r"\s<=?\s|assert_eq|assert_ne", stmt)
        if (re.search(_RS_ELAPSED, stmt) or via or re.search(_RS_CLOCK + r"\s*-", stmt)) and not lower:
            found.append(i + 1)
    return found


def is_python_test(rel):
    parts = rel.split("/")
    name = parts[-1]
    return name.startswith("test_") or name.endswith("_test.py") or name == "tests.py" or "tests" in parts[:-1] \
        or "test" in parts[:-1]


def is_js_test(rel):
    parts = rel.split("/")
    return ".test." in parts[-1] or ".spec." in parts[-1] or "tests" in parts[:-1]


def is_rust_test(rel, source):
    return "/tests/" in "/" + rel or "#[test]" in source or "#[tokio::test" in source


def read_allowlist(path=ALLOWLIST_PATH):
    allowed = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                entry = line.split("#", 1)[0].strip()
                if entry:
                    allowed.add(entry)
    return allowed


def read_baseline(path=BASELINE_PATH):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def scan(roots):
    """{display_path: [line, ...]} for every test file with at least one finding, before allowlist/baseline."""
    results = {}
    scanned = 0
    for root in roots:
        prefix = os.path.basename(root)
        files = []
        for suffix, kind in ((".py", "py"), (".js", "js"), (".mjs", "js"), (".rs", "rs")):
            files += [(p, kind) for p in tree_files(root, suffix, IGNORE_DIR_NAMES, skip_hidden=True)]
        for path, kind in files:
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            if kind == "py" and not is_python_test(rel):
                continue
            if kind == "js" and not is_js_test(rel):
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    source = f.read()
            except (OSError, UnicodeDecodeError):
                continue
            scanned += 1
            if kind == "py":
                lines = check_python(source)
            elif kind == "js":
                lines = check_js(source)
            else:
                lines = check_rust(source) if is_rust_test(rel, source) else []
            if lines:
                results[f"{prefix}/{rel}"] = lines
    return results, scanned


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print("Usage: check_wallclock_in_tests.py <repo_root> [<repo_root> ...] [--list-baseline]")
        sys.exit(1)
    roots = []
    for given in args:
        check_target.require_directory(given, "check_wallclock_in_tests")
        repo_root = os.path.abspath(given)
        if os.path.basename(repo_root) == "hams_shared":
            repo_root = os.path.dirname(repo_root)
        if repo_root not in roots:
            roots.append(repo_root)
    results, scanned = scan(roots)
    check_target.require_scanned(scanned, ", ".join(roots), "check_wallclock_in_tests", "test files")
    allowed = read_allowlist()
    results = {p: v for p, v in results.items() if p not in allowed}
    if "--list-baseline" in sys.argv:
        print(json.dumps({p: len(v) for p, v in sorted(results.items())}, indent=1))
        return
    baseline = read_baseline()
    failed = False
    for path, lines in sorted(results.items()):
        if len(lines) > baseline.get(path, 0):
            failed = True
            for n in lines:
                print(f"❌ {path}:{n}: test asserts on elapsed wall-clock time (load-dependent). Use process_time(), "
                      "a counter, or virtual time (fake timers / tokio::time::pause / an injected clock); "
                      "see tools/check_wallclock_in_tests.py")
        else:
            print(f"⚠️ {path}: {len(lines)} wall-clock assertion(s) at lines {lines} (baseline {baseline[path]}; convert them)")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
