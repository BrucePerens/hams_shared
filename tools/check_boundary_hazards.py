#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Boundary hazards gate (Python and shell): five shapes of bug that bug hunts kept finding by hand.

One pass over every `.py` (and, for the `gh` rule, every `.sh`) file of both repositories; each file is parsed once and
every rule looks at the same tree. The rules are heuristics over syntax, not dataflow, tuned to stay silent unless the
dangerous shape is really there. Rule ids, in the order they were proposed (2026-11-09 bug hunts):

`credential-redirect`
    A credential header (`X-API-Key`, `Authorization`, `X-Auth-Token`, ...) is built in a file that sends requests with
    `urllib.request`, and the file has no redirect-refusing handler (no `HTTPRedirectHandler` subclass, no
    `ssrf_safe_fetch`). urllib follows a 3xx answer to any host and copies every header of the request to it, so one
    redirect hands the key to whoever the answer names. Fix: `urllib.request.build_opener(_NoRedirect)` with a
    `redirect_request` that returns None, and send the request through that opener.

`argv-log`
    A logging call whose argument is a raw process command line: a `cmdline` value (psutil, `/proc/<pid>/cmdline`),
    `sys.argv` taken whole or sliced, or the joined text of a `cmd`/`argv` list. A command line is not a secret-free
    string (`--token X`, `-p password`, a URL with a key); the log file outlives the process and is read by more people.
    Log the program name and the arguments you know are safe, or a count.

`rpc-service-identity`
    A public `@api.model` method (no leading underscore, no `@api.private`) of an Odoo model that acts with another
    identity (`_get_service_env`, `_get_service_uid`, `with_user`, `sudo`, a `*_service()` helper) and has no check of
    who the caller is in its body (`self.env.su`, `env.user`, `has_group`, `check_access*`, `AccessError`, or a call to
    a `_require*`/`_check*`/`_assert*` helper). `/web/dataset/call_kw` lets any signed-in user call every public method,
    so such a method is a way to borrow the service account. Fix: decorate with `@api.private` (cron and Python callers
    still work), prefix the name with `_`, or check the caller first.

`gh-truncated-listing`
    `gh ... --json files` (a pull request's file list stops at 100 entries) without `changedFiles` next to it to compare
    against, or `gh api .../pulls/N/files` without `--paginate`. A truncated listing treated as complete judges a
    large pull request on a fraction of its files.

`timestamp-range`
    `time.gmtime`/`localtime`, `datetime.fromtimestamp`/`utcfromtimestamp` of a value that is not obviously this
    machine's own clock or a file time, with no `math.isfinite` call in the function and no enclosing `try` that
    covers OverflowError, ValueError and OSError. JSON accepts `1e999` and `NaN`; both raise out of these calls with an
    exception type the surrounding `except ValueError` does not catch, and an out-of-range year raises a third.

Silencing. A finding may carry a same-line `# boundary-hazard-ignore: <rule-id>: <reason>` comment (on the `def` or a
decorator line for `rpc-service-identity`), for a site that is safe and says why. A finding that is real but not
fixed yet goes in `boundary_hazards_known.json` next to this script, one entry per finding with an owner and a reason
(see README.md "Boundary hazards"); an entry that no longer matches a finding is itself an error, so the list can only
shrink. The scan prints every finding as `rule path:line: message` and exits 1 when any is not silenced.
"""

import ast
import json
import os
import re
import sys

import lint_cache
from tree_files import tree_files

IGNORE_DIR_NAMES = {
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    "target",
    ".git",
    "worktrees",
    # Any virtualenv, whatever its outer directory is called (see check_float_isfinite.py).
    "site-packages",
}

KNOWN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "boundary_hazards_known.json")

RULES = (
    "credential-redirect",
    "argv-log",
    "rpc-service-identity",
    "gh-truncated-listing",
    "timestamp-range",
)

_IGNORE_RE = re.compile(r"boundary-hazard-ignore:\s*([a-z-]+)\s*:\s*(\S.*)")


# [@ANCHOR: boundary_hazards:find_files]
def find_files(repo_root):
    """Every `.py` and `.sh` file of the tree (git-tracked and unignored, minus vendored and build directories), sorted."""
    return sorted(set(tree_files(repo_root, ".py", IGNORE_DIR_NAMES, skip_hidden=True))
                  | set(tree_files(repo_root, ".sh", IGNORE_DIR_NAMES, skip_hidden=True)))


def _is_test_path(path):
    parts = path.replace("\\", "/").split("/")
    name = parts[-1]
    return "tests" in parts[:-1] or name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"


def _call_name(node):
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return getattr(func, "id", "")


def _const_str(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


class _Tree:
    """One parsed file with the lookups every rule needs."""

    def __init__(self, source, path):
        self.source = source
        self.lines = source.splitlines()
        self.path = path
        self.tree = ast.parse(source, filename=path)
        self.parent = {}
        for node in ast.walk(self.tree):
            for child in ast.iter_child_nodes(node):
                self.parent[child] = node

    def enclosing_function(self, node):
        cur = self.parent.get(node)
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return cur
            cur = self.parent.get(cur)
        return None

    def symbol(self, node):
        """Dotted `Class.method` (or `function`, or `<module>`) the node sits in: the stable key of a finding."""
        names = []
        cur = self.parent.get(node) if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else node
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.append(cur.name)
            cur = self.parent.get(cur)
        return ".".join(reversed(names)) or "<module>"

    def ignored(self, rule, *linenos):
        for lineno in linenos:
            if 1 <= lineno <= len(self.lines):
                match = _IGNORE_RE.search(self.lines[lineno - 1])
                if match and match.group(1) == rule:
                    return True
        return False

    def try_bodies_around(self, node):
        """Every `ast.Try` whose own body contains `node`, innermost first."""
        out = []
        child, cur = node, self.parent.get(node)
        while cur is not None:
            if isinstance(cur, ast.Try) and any(child is stmt for stmt in cur.body):
                out.append(cur)
            child, cur = cur, self.parent.get(cur)
        return out


# --------------------------------------------------------------------------------------------------
# credential-redirect

_CREDENTIAL_HEADERS = {
    "x-api-key", "authorization", "proxy-authorization", "x-auth-token", "x-auth-key", "x-api-token",
    "private-token", "api-key", "x-token", "cf-access-client-secret", "x-cf-secret", "apikey",
}
_REDIRECT_SAFE_MARKERS = ("HTTPRedirectHandler", "ssrf_safe_fetch", "safe_fetch(")


def _credential_sites(t):
    """(lineno, node) of every place a credential header is put together."""
    sites = []
    for node in ast.walk(t.tree):
        if isinstance(node, ast.Dict):
            if any((_const_str(k) or "").lower() in _CREDENTIAL_HEADERS for k in node.keys if k is not None):
                sites.append((node.lineno, node))
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
            if (_const_str(node.slice) or "").lower() in _CREDENTIAL_HEADERS:
                sites.append((node.lineno, node))
        elif isinstance(node, ast.Call) and _call_name(node) in ("add_header", "add_unredirected_header"):
            # add_unredirected_header is the safe spelling: urllib does not copy it to a redirect target.
            if _call_name(node) == "add_header" and node.args and (_const_str(node.args[0]) or "").lower() in _CREDENTIAL_HEADERS:
                sites.append((node.lineno, node))
    return sites


def _rule_credential_redirect(t):
    src = t.source
    if "urllib" not in src or not re.search(r"urllib\.request|from urllib import request|from urllib\.request import", src):
        return []
    if not re.search(r"\burlopen\s*\(|\bbuild_opener\s*\(|\.open\s*\(", src):
        return []
    if any(marker in src for marker in _REDIRECT_SAFE_MARKERS):
        return []
    findings, seen = [], set()
    for lineno, node in _credential_sites(t):
        symbol = t.symbol(node)
        if symbol in seen or t.ignored("credential-redirect", lineno):
            continue
        seen.add(symbol)
        findings.append(("credential-redirect", symbol, lineno,
                         "a credential header is built here and this file sends with urllib but refuses no redirect: "
                         "urllib follows a 3xx answer to any host and copies every header to it. Send through "
                         "`urllib.request.build_opener(<HTTPRedirectHandler subclass whose redirect_request returns None>)`."))
    return findings


# --------------------------------------------------------------------------------------------------
# argv-log

_LOG_RECEIVER = re.compile(r"(?i)^_?(logger|log|logging|_log|self\.logger|self\._logger|self\.log|_logger)$")
_LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log", "fatal"}
_ARGV_NAMES = {"cmd", "argv", "cmdline", "full_cmd", "cmd_list", "argv_list", "command_line", "cmd_line"}
_JOIN_NAMES = _ARGV_NAMES | {"command", "args"}
_CMDLINE_WORD = re.compile(r"(?i)cmd_?line|command_?line")


def _receiver_text(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return f"{node.value.id}.{node.attr}"
    return ""


def _argv_hazard(expr):
    """A short description of the raw command line `expr` carries into a log line, or None."""
    # One element (`sys.argv[0]`, `cmd[0]`) or a length (`len(cmd)`) is not a whole command line.
    single = set()
    for n in ast.walk(expr):
        if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant) and isinstance(n.slice.value, int):
            single.add(id(n.value))
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "len":
            single.update(id(a) for a in n.args)
    for n in ast.walk(expr):
        if id(n) in single:
            continue
        if isinstance(n, ast.Name) and (n.id in _ARGV_NAMES or _CMDLINE_WORD.search(n.id)):
            return f"`{n.id}`"
        if isinstance(n, ast.Attribute):
            if _CMDLINE_WORD.search(n.attr):
                return f"`.{n.attr}`"
            if n.attr == "argv" and isinstance(n.value, ast.Name) and n.value.id == "sys":
                return "`sys.argv`"
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value == "cmdline":
            return "the `cmdline` field"
        if isinstance(n, ast.Call) and _call_name(n) == "join":
            for sub in ast.walk(n):
                if isinstance(sub, ast.Name) and sub.id in _JOIN_NAMES:
                    return f"the joined text of `{sub.id}`"
    return None


def _rule_argv_log(t):
    findings = []
    for node in ast.walk(t.tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _LOG_METHODS):
            continue
        if not _LOG_RECEIVER.match(_receiver_text(node.func.value)):
            continue
        parts = list(node.args) + [kw.value for kw in node.keywords]
        for part in parts:
            hazard = _argv_hazard(part)
            if hazard is None or t.ignored("argv-log", node.lineno, getattr(part, "lineno", node.lineno)):
                continue
            findings.append(("argv-log", t.symbol(node), node.lineno,
                             f"a logging call carries {hazard}, a raw command line; an argument can be a secret and a log "
                             "file outlives the process. Log the program name and the arguments known to be safe."))
            break
    return findings


# --------------------------------------------------------------------------------------------------
# rpc-service-identity

_SERVICE_ATTRS = {"_get_service_env", "_get_service_uid", "_get_service_user", "sudo", "with_user"}
_SERVICE_HELPER = re.compile(r"^_\w*service\w*$")
_CALLER_ATTRS = {
    "su", "user", "uid", "has_group", "check_access", "check_access_rights", "check_access_rule", "is_superuser",
    "is_admin", "is_system", "_is_admin", "_is_system", "_is_internal",
}
_CALLER_HELPER = re.compile(r"^_(require|check|assert|ensure|authorize|guard|verify)\w*$")


def _decorators(func):
    names = set()
    for d in func.decorator_list:
        node = d.func if isinstance(d, ast.Call) else d
        names.add(ast.unparse(node))
    return names


def _class_is_model(cls):
    for stmt in cls.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id in ("_name", "_inherit"):
                    return True
    return False


def _rule_rpc_service_identity(t):
    findings = []
    for cls in ast.walk(t.tree):
        if not (isinstance(cls, ast.ClassDef) and _class_is_model(cls)):
            continue
        for func in cls.body:
            if not isinstance(func, ast.FunctionDef) or func.name.startswith("_"):
                continue
            decorators = _decorators(func)
            if "api.model" not in decorators or decorators & {"api.private", "private"}:
                continue
            acts_as, guarded = None, False
            for n in ast.walk(func):
                if isinstance(n, ast.Attribute):
                    if n.attr in _SERVICE_ATTRS or (isinstance(t.parent.get(n), ast.Call) and t.parent[n].func is n
                                                    and _SERVICE_HELPER.match(n.attr)):
                        acts_as = acts_as or n.attr
                    if n.attr in _CALLER_ATTRS and not (n.attr in _SERVICE_ATTRS):
                        guarded = True
                    if isinstance(t.parent.get(n), ast.Call) and t.parent[n].func is n and _CALLER_HELPER.match(n.attr):
                        guarded = True
                elif isinstance(n, ast.Name) and n.id == "AccessError":
                    guarded = True
            lines = [func.lineno] + [d.lineno for d in func.decorator_list]
            if acts_as and not guarded and not t.ignored("rpc-service-identity", *lines):
                findings.append(("rpc-service-identity", f"{cls.name}.{func.name}", func.lineno,
                                 f"public `@api.model` method `{func.name}` acts through `{acts_as}` and never checks who called it: "
                                 "any signed-in user can call it through /web/dataset/call_kw. Decorate it `@api.private` "
                                 "(cron and Python callers still work), prefix it with `_`, or check the caller first."))
    return findings


# --------------------------------------------------------------------------------------------------
# gh-truncated-listing

_GH_JSON_FIELDS = re.compile(r"--json[\"']?[\s,=]+[\"']?([A-Za-z,]+)")
_GH_API_FILES = re.compile(r"pulls/[^\s\"']*/files\b")


def _gh_findings_in_text(text, lineno):
    """Findings for one command's text (a shell line or the joined strings of one argv list)."""
    out = []
    for match in _GH_JSON_FIELDS.finditer(text):
        fields = set(match.group(1).split(","))
        if "files" in fields and "changedFiles" not in fields:
            out.append((lineno, "`--json` asks for `files`, which `gh` stops at 100 entries, with no `changedFiles` in the same "
                                "request to compare the count against; add `changedFiles` and refuse or page when they differ."))
    if _GH_API_FILES.search(text) and "--paginate" not in text:
        out.append((lineno, "`gh api .../pulls/N/files` returns one page (30 entries by default, 100 at most); add `--paginate`."))
    return out


def _rule_gh_truncated_listing_py(t):
    findings = []
    for node in ast.walk(t.tree):
        if not isinstance(node, (ast.List, ast.Tuple)):
            continue
        strings = []
        for elt in node.elts:
            text = _const_str(elt)
            if text is None and isinstance(elt, ast.JoinedStr):
                text = "".join(v.value for v in elt.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
            if text is not None:
                strings.append(text)
        if not strings or "gh" not in strings[:1]:
            continue
        for lineno, message in _gh_findings_in_text(" ".join(strings), node.lineno):
            if not t.ignored("gh-truncated-listing", node.lineno, lineno):
                findings.append(("gh-truncated-listing", t.symbol(node), lineno, message))
    return findings


def _rule_gh_truncated_listing_sh(path, source):
    findings = []
    lines = source.splitlines()
    index = 0
    while index < len(lines):
        start = index
        text = lines[index]
        while text.rstrip().endswith("\\") and index + 1 < len(lines):
            index += 1
            text = text.rstrip()[:-1] + " " + lines[index]
        index += 1
        if not re.search(r"\bgh\s+(pr|api|run|release|issue)\b", text) or text.lstrip().startswith("#"):
            continue
        for lineno, message in _gh_findings_in_text(text, start + 1):
            if "boundary-hazard-ignore: gh-truncated-listing" not in text:
                findings.append(("gh-truncated-listing", "<script>", lineno, message))
    return findings


# --------------------------------------------------------------------------------------------------
# timestamp-range

_TIMESTAMP_CALLS = {"gmtime", "localtime", "fromtimestamp", "utcfromtimestamp"}
_TRUSTED_CALLS = {"time", "monotonic", "now", "utcnow", "timestamp", "getmtime", "getctime", "getatime", "mktime",
                  "time_ns", "today", "perf_counter"}
_PASSTHROUGH_CALLS = {"float", "int", "abs", "max", "min", "round"}


def _caught_names(try_node):
    names = set()
    for handler in try_node.handlers:
        if handler.type is None:
            names.add("Exception")
            continue
        for item in (handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]):
            names.add(getattr(item, "id", getattr(item, "attr", "?")))
    return names


def _covers_range_errors(names):
    if names & {"Exception", "BaseException"}:
        return True
    return (bool(names & {"OverflowError", "ArithmeticError"}) and "ValueError" in names
            and bool(names & {"OSError", "EnvironmentError", "IOError"}))


def _trusted_time(t, expr, scope, depth=0):
    """True when `expr` is this machine's clock, a file time, a constant, or arithmetic of those (also through a local that
    is only ever assigned such values)."""
    if depth > 4:
        return False
    if isinstance(expr, ast.Constant):
        return isinstance(expr.value, (int, float))
    if isinstance(expr, ast.Call):
        if _call_name(expr) in _TRUSTED_CALLS:
            return True
        if _call_name(expr) in _PASSTHROUGH_CALLS and expr.args:
            return all(_trusted_time(t, a, scope, depth + 1) for a in expr.args)
        return False
    if isinstance(expr, ast.Attribute):
        return expr.attr in ("st_mtime", "st_ctime", "st_atime", "st_mtime_ns")
    if isinstance(expr, ast.BinOp):
        return _trusted_time(t, expr.left, scope, depth + 1) and _trusted_time(t, expr.right, scope, depth + 1)
    if isinstance(expr, ast.UnaryOp):
        return _trusted_time(t, expr.operand, scope, depth + 1)
    if isinstance(expr, ast.IfExp):
        return _trusted_time(t, expr.body, scope, depth + 1) and _trusted_time(t, expr.orelse, scope, depth + 1)
    if isinstance(expr, ast.Name):
        values = []
        for n in ast.walk(scope):
            if isinstance(n, ast.Assign) and any(isinstance(tg, ast.Name) and tg.id == expr.id for tg in n.targets):
                values.append(n.value)
            elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == expr.id and n.value:
                values.append(n.value)
        return bool(values) and all(_trusted_time(t, v, scope, depth + 1) for v in values)
    return False


def _rule_timestamp_range(t):
    findings = []
    for node in ast.walk(t.tree):
        if not (isinstance(node, ast.Call) and _call_name(node) in _TIMESTAMP_CALLS and node.args):
            continue
        func = t.enclosing_function(node)
        scope = func if func is not None else t.tree
        if _trusted_time(t, node.args[0], scope):
            continue
        if any(_covers_range_errors(_caught_names(tr)) for tr in t.try_bodies_around(node)):
            continue
        if any(isinstance(n, ast.Call) and _call_name(n) == "isfinite" for n in ast.walk(scope)):
            continue
        if t.ignored("timestamp-range", node.lineno):
            continue
        findings.append(("timestamp-range", t.symbol(node), node.lineno,
                         f"`{ast.unparse(node)[:80]}`: a value from outside (JSON accepts 1e999 and NaN) raises OverflowError, "
                         "ValueError or OSError here and nothing in this function checks `math.isfinite` or the range. "
                         "Check the value (finite, inside the years you accept) before converting, or catch all three."))
    return findings


# --------------------------------------------------------------------------------------------------

# [@ANCHOR: boundary_hazards:check_file]
def check_file(path):
    """`[rule, symbol, line, message]` findings for one file, before the known list is applied (JSON-serialisable, so the
    per-file result cache can hold it). A `.sh` file is only judged by `gh-truncated-listing`; a file that does not parse
    has none."""
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        source = handle.read()
    if path.endswith(".sh"):
        return [list(f) for f in _rule_gh_truncated_listing_sh(path, source)]
    try:
        tree = _Tree(source, path)
    except (SyntaxError, ValueError):
        return []
    findings = []
    findings += _rule_gh_truncated_listing_py(tree)
    if not _is_test_path(path):
        findings += _rule_credential_redirect(tree)
        findings += _rule_argv_log(tree)
        findings += _rule_rpc_service_identity(tree)
        findings += _rule_timestamp_range(tree)
    return [list(f) for f in findings]


# [@ANCHOR: boundary_hazards:load_known]
def load_known(path=None):
    """`(entries, problems)`: the entries of the known list and a message for each malformed one. An entry needs a rule that
    exists, a path, a symbol, a non-empty owner and a non-empty reason."""
    path = path or KNOWN_FILE
    if not os.path.exists(path):
        return [], []
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    entries, problems = [], []
    for index, entry in enumerate(data.get("entries", [])):
        missing = [k for k in ("rule", "path", "symbol", "owner", "reason") if not str(entry.get(k, "")).strip()]
        if missing:
            problems.append(f"known entry {index}: missing or empty {', '.join(missing)}")
        elif entry["rule"] not in RULES:
            problems.append(f"known entry {index}: unknown rule {entry['rule']!r}")
        else:
            entries.append(entry)
    return entries, problems


def _scan(roots, cache):
    """`{(rule, relpath, symbol): [(line, message), ...]}` over every root, plus the set of relative paths scanned."""
    found, scanned = {}, set()
    for repo_root in roots:
        for path in find_files(repo_root):
            rel = os.path.relpath(path, repo_root)
            scanned.add(rel)
            for rule, symbol, line, message in lint_cache.cached_file_result(cache, path, check_file):
                found.setdefault((rule, rel, symbol), []).append((line, message))
    return found, scanned


def main():
    if len(sys.argv) < 2:
        print("Usage: check_boundary_hazards.py <repo_root> [<repo_root> ...]")
        sys.exit(1)
    roots = []
    for given in sys.argv[1:]:
        repo_root = os.path.abspath(given)
        if os.path.basename(repo_root) == "hams_shared":
            repo_root = os.path.dirname(repo_root)
        if repo_root not in roots:
            roots.append(repo_root)

    cache = lint_cache.ResultCache("check_boundary_hazards", lint_cache.checker_version(os.path.abspath(__file__)))
    found, scanned = _scan(roots, cache)
    cache.save()

    entries, problems = load_known()
    known_keys = {(e["rule"], e["path"], e["symbol"]) for e in entries}
    failed = False
    for problem in problems:
        print(f"❌ {os.path.basename(KNOWN_FILE)}: {problem}")
        failed = True
    for (rule, rel, symbol), hits in sorted(found.items()):
        if (rule, rel, symbol) in known_keys:
            continue
        failed = True
        for line, message in sorted(hits):
            print(f"❌ {rule} {rel}:{line}: {message}")
    # A known entry whose file was scanned and no longer has the finding is stale. A path no scanned root has is judged
    # only when both repositories were scanned (a single-root run cannot tell a deleted file from the other repository's).
    for entry in entries:
        key = (entry["rule"], entry["path"], entry["symbol"])
        if key in found:
            continue
        exists = any(os.path.exists(os.path.join(r, entry["path"])) for r in roots)
        if exists or (not exists and len(roots) >= 2):
            failed = True
            print(f"❌ {os.path.basename(KNOWN_FILE)}: {entry['rule']} {entry['path']} {entry['symbol']} no longer has the "
                  "finding: remove the entry")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
