#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Secret storage gate: a secret goes in the secret store, never in the database.

The Odoo web process can read every `ir.config_parameter` row and every model field, and so can anyone who gets a
database dump, an SQL-injection read, a settings read over RPC or a stolen backup. Modules ask the secret store
(`zero_sudo/daemon/secret_store.py` in hams_open; story `zero_sudo/docs/stories/secret_store_tier_a.md`) for a secret
or for an operation on one instead. This gate fails the build when new code stores a secret in the database. Rule ids:

`secret-config-parameter`
    `set_param("<key>", ...)` with a string-literal key that names a secret, a field declared with
    `config_parameter="<key>"` for such a key, or an `ir.config_parameter` data record (XML) whose `key` names one.
    Symbol: the key.

`secret-field`
    A `fields.Char`, `fields.Text` or `fields.Binary` on an Odoo model (a class with `_name` or `_inherit`) whose field
    name names a secret. Symbol: `Class.field`.

A name names a secret when one of its words (split on `.` and `_`) is `secret`, `password`, `passwd`, `apikey`,
`token` or `credential`, or it contains `api_key`, `private_key` or `signing_key`, unless a word marks it as not the
secret itself: `public`, `hash`, `hint`, `masked`, `last4`, `fingerprint`, `configured`, `set`, `url`, `expiry`,
`expires`, `expiration`, `date`, `ttl`, `lifetime`, `length`, `count`, `enabled`, `required`, `reset`, `policy`,
`id`, `ids`, `name`, `label`. Test files are not judged (a test sets fake secrets on purpose).

Silencing, as for check_boundary_hazards.py: a same-line `# secret-storage-ignore: <rule-id>: <reason>` comment (XML:
`<!-- secret-storage-ignore: <rule-id>: <reason> -->` on the line) for a site that is not a secret, and
`secret_storage_known.json` next to this script for the secrets that are still stored in the database and not migrated
yet (one entry per finding, with an owner and a reason). A known entry that no longer matches a finding is an error, so
the list only shrinks. Prints every finding as `rule path:line: message`; exits 1 when one is not silenced.
"""

import ast
import json
import os
import re
import sys

import check_target
from tree_files import tree_files

IGNORE_DIR_NAMES = {"__pycache__", "node_modules", ".venv", "venv", "target", ".git", "worktrees", "site-packages"}
KNOWN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "secret_storage_known.json")
RULES = ("secret-config-parameter", "secret-field")

SECRET_WORDS = frozenset({"secret", "password", "passwd", "apikey", "token", "credential"})
SECRET_PHRASES = ("api_key", "private_key", "signing_key")
NOT_SECRET_WORDS = frozenset({
    "public", "hash", "hint", "masked", "last4", "fingerprint", "configured", "set", "url", "expiry", "expires",
    "expiration", "date", "ttl", "lifetime", "length", "count", "enabled", "required", "reset", "policy", "id", "ids",
    "name", "label",
})
SECRET_FIELD_TYPES = frozenset({"Char", "Text", "Binary"})
_IGNORE_RE = re.compile(r"secret-storage-ignore:\s*([a-z-]+)\s*:\s*(\S.*)")
_XML_KEY_RE = re.compile(r'<field\s+name="key"[^>]*>\s*([^<\s]+)\s*</field>')
_XML_PARAM_RECORD_RE = re.compile(r'<record\b[^>]*model="ir\.config_parameter"[^>]*>(.*?)</record>', re.S)


# [@ANCHOR: secret_storage:names_a_secret]
def names_a_secret(name):
    """True when a parameter key or field name names a secret (rules in the module docstring)."""
    lowered = name.lower()
    words = [w for w in re.split(r"[._\-]+", lowered) if w]
    if any(w in NOT_SECRET_WORDS for w in words):
        return False
    return any(w in SECRET_WORDS for w in words) or any(p in lowered for p in SECRET_PHRASES)


# [@ANCHOR: secret_storage:is_test_path]
def is_test_path(path):
    """True for a file under a `tests` directory or named like a test (`test_*.py`, `*_test.py`, `conftest.py`)."""
    parts = path.replace("\\", "/").split("/")
    name = parts[-1]
    return "tests" in parts[:-1] or name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"


# [@ANCHOR: secret_storage:find_files]
def find_files(repo_root):
    """Every `.py` and `.xml` file of the tree (git-tracked and unignored, minus vendored and build directories), sorted."""
    return sorted(set(tree_files(repo_root, ".py", IGNORE_DIR_NAMES, skip_hidden=True))
                  | set(tree_files(repo_root, ".xml", IGNORE_DIR_NAMES, skip_hidden=True)))


# [@ANCHOR: secret_storage:silenced]
def silenced(lines, lineno, rule):
    """True when line `lineno` (1-based) carries an ignore comment for `rule` with a reason."""
    if not 1 <= lineno <= len(lines):
        return False
    match = _IGNORE_RE.search(lines[lineno - 1])
    return bool(match and match.group(1) == rule)


# [@ANCHOR: secret_storage:check_python]
def check_python(source):
    """`[(rule, symbol, line, message)]` for one Python source; a file that does not parse has none."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    lines = source.splitlines()
    findings = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            findings += _call_findings(node)
        elif isinstance(node, ast.ClassDef) and _is_odoo_model(node):
            findings += _field_findings(node)
    return [f for f in findings if not silenced(lines, f[2], f[0])]


# [@ANCHOR: secret_storage:call_findings]
def _call_findings(node):
    """`set_param("<secret key>", ...)` and `config_parameter="<secret key>"` in one call."""
    found = []
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "set_param" and node.args:
        key = node.args[0]
        if isinstance(key, ast.Constant) and isinstance(key.value, str) and names_a_secret(key.value):
            found.append(("secret-config-parameter", key.value, node.lineno,
                          f"set_param({key.value!r}) stores a secret in the database; use the secret store"))
    for keyword in node.keywords:
        value = keyword.value
        if (keyword.arg == "config_parameter" and isinstance(value, ast.Constant) and isinstance(value.value, str)
                and names_a_secret(value.value)):
            found.append(("secret-config-parameter", value.value, node.lineno,
                          f"config_parameter={value.value!r} stores a secret in the database; use the secret store"))
    return found


# [@ANCHOR: secret_storage:is_odoo_model]
def _is_odoo_model(node):
    """True for a class body that assigns `_name` or `_inherit` (an Odoo model or an extension of one)."""
    for stmt in node.body:
        if isinstance(stmt, ast.Assign) and any(isinstance(t, ast.Name) and t.id in ("_name", "_inherit")
                                                for t in stmt.targets):
            return True
    return False


# [@ANCHOR: secret_storage:field_findings]
def _field_findings(node):
    """`<secret name> = fields.Char|Text|Binary(...)` in one model class."""
    found = []
    for stmt in node.body:
        if not (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name)):
            continue
        call = stmt.value
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                and call.func.attr in SECRET_FIELD_TYPES and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "fields"):
            continue
        name = stmt.targets[0].id
        if names_a_secret(name):
            found.append(("secret-field", f"{node.name}.{name}", stmt.lineno,
                          f"field {name!r} stores a secret in the database; use the secret store"))
    return found


# [@ANCHOR: secret_storage:check_xml]
def check_xml(source):
    """`[(rule, symbol, line, message)]` for `ir.config_parameter` data records whose key names a secret."""
    lines = source.splitlines()
    findings = []
    for record in _XML_PARAM_RECORD_RE.finditer(source):
        key_match = _XML_KEY_RE.search(record.group(1))
        if key_match and names_a_secret(key_match.group(1)):
            line = source.count("\n", 0, record.start(1) + key_match.start()) + 1
            if not silenced(lines, line, "secret-config-parameter"):
                findings.append(("secret-config-parameter", key_match.group(1), line,
                                 f"ir.config_parameter record {key_match.group(1)!r} stores a secret in the database"))
    return findings


# [@ANCHOR: secret_storage:check_file]
def check_file(path):
    """Findings for one file; test files have none."""
    if is_test_path(path):
        return []
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        source = handle.read()
    return check_xml(source) if path.endswith(".xml") else check_python(source)


# [@ANCHOR: secret_storage:load_known]
def load_known(path=None):
    """`(entries, problems)`: the known list's entries and a message per malformed one (an entry needs a known rule, a
    path, a symbol, an owner and a reason)."""
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


# [@ANCHOR: secret_storage:scan]
def scan(roots):
    """`{(rule, relpath, symbol): [(line, message), ...]}` over every root."""
    found = {}
    for repo_root in roots:
        for path in find_files(repo_root):
            rel = os.path.relpath(path, repo_root)
            for rule, symbol, line, message in check_file(path):
                found.setdefault((rule, rel, symbol), []).append((line, message))
    return found


# [@ANCHOR: secret_storage:report]
def report(found, entries, problems, roots):
    """The output lines and the exit status for a scan result against the known list."""
    out = [f"❌ {os.path.basename(KNOWN_FILE)}: {p}" for p in problems]
    known_keys = {(e["rule"], e["path"], e["symbol"]) for e in entries}
    for (rule, rel, symbol), hits in sorted(found.items()):
        if (rule, rel, symbol) not in known_keys:
            out += [f"❌ {rule} {rel}:{line}: {message}" for line, message in sorted(hits)]
    for entry in entries:
        key = (entry["rule"], entry["path"], entry["symbol"])
        if key in found:
            continue
        exists = any(os.path.exists(os.path.join(r, entry["path"])) for r in roots)
        if exists or len(roots) >= 2:
            out.append(f"❌ {os.path.basename(KNOWN_FILE)}: {entry['rule']} {entry['path']} {entry['symbol']} no longer "
                       "has the finding: remove the entry")
    if out:
        out.append("   DIAGNOSTIC FOR AI: ask the secret store for the secret, or for an operation on it "
                   "(hams_open zero_sudo/docs/stories/secret_store_tier_a.md); never ir.config_parameter or a field.")
    return out, (1 if out else 0)


# [@ANCHOR: secret_storage:main]
def main(argv=None):
    """`check_secret_storage.py <repo_root> [<repo_root> ...]`; a `hams_shared` root means its parent repository."""
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print("Usage: check_secret_storage.py <repo_root> [<repo_root> ...]")
        return 1
    roots = []
    for given in argv:
        repo_root = check_target.require_directory(given, "check_secret_storage")
        if os.path.basename(repo_root) == "hams_shared":
            repo_root = os.path.dirname(repo_root)
        if repo_root not in roots:
            roots.append(repo_root)
    check_target.require_scanned(
        sum(len(find_files(repo_root)) for repo_root in roots), ", ".join(roots), "check_secret_storage", ".py and .xml files"
    )
    entries, problems = load_known()
    lines, status = report(scan(roots), entries, problems, roots)
    for line in lines:
        print(line)
    if status == 0:
        print("[*] secret storage: no new secret in ir.config_parameter or a model field")
    return status


if __name__ == "__main__":
    sys.exit(main())
