#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Service Account ACL Checker
-------------------------------------------------------------------------------------------
A zero_sudo service account (odoo_facility_service_internal, sk_processor_service, ...) is an
ordinary res.users whose only rights are the ir.model.access rows of its groups. Code that runs
`self.env["some.model"].with_user(svc_uid).create(...)` against a model none of those groups can
create/write/unlink/read raises AccessError, and these call sites usually sit inside a
`try/except` that logs and carries on -- so the feature just silently does not work. Two such
bugs were found by hand on 2026-11-14 (hams_base's email-change alert could not create
mail.mail as the facility account; ham_sk_workflow's lockout notice could not read
mail.notification as the sk account), so the lookup is now done statically.

What is checked (AST, no Odoo needed): inside one function, a service identity is
  * a name assigned from `<x>._get_service_uid("module.xml_id")` (the id may be a module or
    class string constant, or the result of a same-file helper that returns such a call), or
  * a name assigned from `<x>._get_service_env("module.xml_id")`, or
  * `self.env.ref("module.xml_id")` (optionally `.id`) used directly.
A record set is `<env>["model"].with_user(<identity>)` or `<service env>["model"]`, followed through
`search/browse/filtered/sorted/with_context/exists` and through names it is assigned to. Each
terminal call on such a record set is one required permission: create -> create, write -> write,
unlink -> unlink, search/read/search_read/search_count/read_group/name_search -> read,
message_post -> create on mail.message plus read on the model.

The account's groups come from every `res.users` record in the two repos' XML (group_ids /
groups_id refs), closed over `implied_ids` of `res.groups` records and over groups that list the
user in `users`/`user_ids`. The rows come from every `ir.model.access.csv` of the repos and of the
Odoo core modules they depend on (a row with an empty group applies to everybody). A required
permission with no row granting it to any of the account's groups is reported.

Escape hatch: `# audit-ignore-service-acl: <why>` on any line of the call. Use it only for a
call that is really fine (a model whose ACL is granted by Python code at install time, an
account whose groups are set by a data function, ...), with the reason in the comment.

Usage: check_service_account_acl.py <repo_root>
"""

import ast
import csv
import glob
import os
import re
import sys
import xml.etree.ElementTree as ET

import check_target
import lint_exec
from tree_files import tree_walk
from source_text import read_source

IGNORE_MARKER = "audit-ignore-service-acl"
SKIP_DIRS = {"node_modules", "__pycache__", ".git", "daemons", "tools", "radae", "tests", "static"}
CORE_ADDONS = "/usr/lib/python3/dist-packages/odoo/addons"
REF_RE = re.compile(r"""ref\(\s*['"]([\w.]+)['"]\s*\)""")

LINK_RE = re.compile(r"""\(\s*4\s*,\s*ref\(\s*['"]([\w.]+)['"]\s*\)\s*\)""")
PERM_OF_OP = {
    "create": "create",
    "write": "write",
    "unlink": "unlink",
    "search": "read",
    "search_read": "read",
    "search_count": "read",
    "read": "read",
    "read_group": "read",
    "name_search": "read",
}
# get_param/set_param are not checked: ham_base's ir.config_parameter override hands every call from a service
# account to the config service account (ham* keys and its allow-lists), so the caller's own ACL is not used.
EXTRA_PERMS = {}
KEEP_MODEL_OPS = {"search", "browse", "filtered", "sorted", "with_context", "exists", "with_prefetch", "ensure_one", "with_company", "create", "limit"}
SEED_FIELDS = {"partner_id": "res.partner", "user_id": "res.users", "company_id": "res.company"}


def _resolve_repo_roots(given_path):
    given_path = os.path.abspath(given_path)
    if os.path.basename(given_path) == "hams_shared":
        given_path = os.path.dirname(given_path)
    roots = [given_path]
    sibling = lint_exec.sibling_repo_root(given_path, lint_exec.has_odoo_module)
    if sibling:
        roots.append(sibling)
    return roots


def _module_of(path):
    """Directory name of the Odoo module (the nearest parent holding __manifest__.py)."""
    cur = os.path.dirname(path)
    while cur and cur != os.path.dirname(cur):
        if os.path.exists(os.path.join(cur, "__manifest__.py")):
            return os.path.basename(cur)
        cur = os.path.dirname(cur)
    return os.path.basename(os.path.dirname(path))


def _qualify(xml_id, module):
    return xml_id if "." in xml_id else f"{module}.{xml_id}"


def _manifest_depends(root):
    deps = set()
    for manifest in glob.glob(os.path.join(root, "*", "__manifest__.py")):
        text, _problem = read_source(manifest)
        for match in re.finditer(r"""['"]depends['"]\s*:\s*\[(.*?)\]""", text or "", re.S):
            deps.update(re.findall(r"""['"]([\w]+)['"]""", match.group(1)))
    return deps


# [@ANCHOR: check_service_account_acl:acl_class]
class Acl:
    """Groups of each service user and the permissions each group has on each model."""

    def __init__(self):
        self.user_groups = {}  # user xml id -> set of group xml ids
        self.implied = {}  # group xml id -> set of group xml ids
        self.rows = {}  # model key -> list of (group xml id or "", {perm,...})

    def add_xml(self, path, module):
        try:
            tree = ET.parse(path)
        except (ET.ParseError, OSError):
            return
        for func in tree.iter("function"):
            # <function model="res.groups" name="write"> <value eval="[ref(group)]"/> <value eval="{'implied_ids': [(4, ref(x))]}"/>
            values = func.findall("value")
            if func.get("model") == "res.groups" and func.get("name") == "write" and len(values) == 2:
                for group in REF_RE.findall(values[0].get("eval") or ""):
                    for implied in LINK_RE.findall(values[1].get("eval") or ""):
                        self.implied.setdefault(_qualify(group, module), set()).add(_qualify(implied, module))
        for rec in tree.iter("record"):
            model, rid = rec.get("model"), rec.get("id")
            if not rid or model not in ("res.users", "res.groups"):
                continue
            rid = _qualify(rid, module)
            for field in rec.findall("field"):
                name = field.get("name")
                refs = [_qualify(r, module) for r in REF_RE.findall(field.get("eval") or "")]
                if field.get("ref"):
                    refs.append(_qualify(field.get("ref"), module))
                if model == "res.users" and name in ("group_ids", "groups_id"):
                    self.user_groups.setdefault(rid, set()).update(refs)
                elif model == "res.groups" and name == "implied_ids":
                    self.implied.setdefault(rid, set()).update(refs)
                elif model == "res.groups" and name in ("users", "user_ids"):
                    for user in refs:
                        self.user_groups.setdefault(user, set()).add(rid)

    def add_csv(self, path, module):
        try:
            with open(path, newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    model_ref = (row.get("model_id:id") or "").rsplit(".", 1)[-1]
                    group = row.get("group_id:id") or ""
                    perms = {p for p in ("read", "write", "create", "unlink") if row.get(f"perm_{p}") == "1"}
                    if model_ref and perms:
                        self.rows.setdefault(model_ref, []).append((_qualify(group, module) if group else "", perms))
        except (OSError, csv.Error, UnicodeDecodeError):
            return

    def groups_of(self, user):
        seen, todo = set(), list(self.user_groups.get(user, ()))
        while todo:
            group = todo.pop()
            if group not in seen:
                seen.add(group)
                todo.extend(self.implied.get(group, ()))
        return seen

    def missing(self, user, model, perm):
        """True when no row gives `perm` on `model` to one of the user's groups (or to everybody)."""
        key = "model_" + model.replace(".", "_")
        groups = self.groups_of(user)
        for group, perms in self.rows.get(key, ()):
            if perm in perms and (not group or group in groups):
                return False
        return True


def load_acl(roots, core_addons=CORE_ADDONS):
    acl = Acl()
    sources = []
    wanted = {"base", "mail", "portal", "web"}
    for root in roots:
        wanted |= _manifest_depends(root)
    if core_addons and os.path.isdir(core_addons):
        sources += [(os.path.join(core_addons, m), m) for m in sorted(wanted) if os.path.isdir(os.path.join(core_addons, m))]
    for root in roots:
        sources += [(d, os.path.basename(d)) for d in sorted(glob.glob(os.path.join(root, "*"))) if os.path.exists(os.path.join(d, "__manifest__.py"))]
    for directory, module in sources:
        for path in glob.glob(os.path.join(directory, "**", "*.xml"), recursive=True):
            if "/static/" not in path and "/tests/" not in path:
                acl.add_xml(path, module)
        for path in glob.glob(os.path.join(directory, "security", "*.csv")):
            acl.add_csv(path, module)
    return acl


def _str(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _class_model(cls):
    """The model a class stands for: its _name, else a string _inherit. None for an abstract mixin (no table, no ACL)."""
    if any(isinstance(b, ast.Attribute) and b.attr == "AbstractModel" for b in cls.bases):
        return None
    wanted = {}
    for node in cls.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and _str(node.value):
            wanted[node.targets[0].id] = node.value.value
    return wanted.get("_name") or wanted.get("_inherit")


def _module_constants(tree):
    consts = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _str(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    consts[target.id] = node.value.value
                elif isinstance(target, ast.Attribute):
                    consts[target.attr] = node.value.value
    return consts


def field_models(roots):
    """field name -> comodel, only where every Many2one/One2many/Many2many of that name agrees."""
    found = {k: {v} for k, v in SEED_FIELDS.items()}
    for root in roots:
        for path in glob.glob(os.path.join(root, "*", "**", "*.py"), recursive=True):
            if "/tests/" in path or "/tools/" in path:
                continue
            text, _problem = read_source(path)
            for match in re.finditer(r"""^\s+(\w+)\s*=\s*fields\.(?:Many2one|One2many|Many2many)\(\s*['"]([\w.]+)['"]""", text or "", re.M):
                found.setdefault(match.group(1), set()).add(match.group(2))
    return {name: next(iter(models)) for name, models in found.items() if len(models) == 1}


# [@ANCHOR: check_service_account_acl:filescan_class]
class FileScan:
    def __init__(self, tree, source_lines, fields=None):
        self.fields = fields or {}
        self.cls_model = None
        self.consts = _module_constants(tree)
        self.lines = source_lines
        self.helpers = {}  # function name -> xml id of the service uid it returns
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for ret in ast.walk(node):
                    if isinstance(ret, ast.Return) and ret.value is not None:
                        xml_id = self._service_call_id(ret.value)
                        if xml_id:
                            self.helpers[node.name] = xml_id

    def _xml_id(self, node):
        text = _str(node)
        if text is None and isinstance(node, ast.Name):
            text = self.consts.get(node.id)
        if text is None and isinstance(node, ast.Attribute):
            text = self.consts.get(node.attr)
        return text if text and "." in text else None

    def _service_call_id(self, node):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("_get_service_uid", "_get_service_env") and node.args:
            return self._xml_id(node.args[0])
        return None

    def uid_of(self, node, names):
        """Service xml id an expression evaluates to as a uid, or None."""
        if isinstance(node, ast.Name):
            return names.get(node.id)
        if isinstance(node, ast.Call):
            if self._service_call_id(node):
                return self._service_call_id(node)
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in self.helpers and isinstance(func.value, ast.Name):
                return self.helpers[func.attr]
            if isinstance(func, ast.Attribute) and func.attr == "ref" and node.args:
                return self._xml_id(node.args[0])
        if isinstance(node, ast.Attribute) and node.attr == "id":
            return self.uid_of(node.value, names)
        return None

    def recset_of(self, node, uids, envs, recs):
        """(service xml id, model) for a record-set expression, or None."""
        if isinstance(node, ast.Name):
            rec = recs.get(node.id)
            return rec if rec and rec[0] else None
        if isinstance(node, ast.Subscript):
            model = _str(node.slice)
            if model and isinstance(node.value, ast.Name) and node.value.id in envs:
                return (envs[node.value.id], model)
            return None
        if isinstance(node, ast.Attribute) and node.attr == "env":
            return None
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attr, base = node.func.attr, node.func.value
            if attr == "with_user" and node.args:
                uid = self.uid_of(node.args[0], uids)
                model = self._model_of(base, recs)
                return (uid, model) if uid and model else None
            if attr in KEEP_MODEL_OPS:
                inner = self.recset_of(base, uids, envs, recs)
                return inner if inner and inner[0] else None
        return None

    def _model_of(self, node, recs):
        if isinstance(node, ast.Subscript):
            return _str(node.slice)
        if isinstance(node, ast.Name) and node.id in recs:
            return recs[node.id][1]
        if isinstance(node, ast.Name) and node.id == "self":
            return self.cls_model
        if isinstance(node, ast.Attribute) and node.attr in self.fields:
            return self.fields[node.attr]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in KEEP_MODEL_OPS:
            return self._model_of(node.func.value, recs)
        return None

    def _bind(self, name, value, uids, envs, recs):
        rec = self.recset_of(value, uids, envs, recs)
        if rec:
            recs[name] = rec
        else:
            model = self._model_of(value, recs)
            if model:
                recs[name] = (None, model)

    def calls(self, func):
        """Yield (lineno, end_lineno, xml id, model, perm) for each terminal call in `func`."""
        uids, envs, recs = {}, {}, {}
        events = []
        for node in ast.walk(func):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                events.append((node.lineno, "assign", node))
            elif isinstance(node, ast.For):
                events.append((node.lineno, "for", node))
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                events.append((node.lineno, "call", node))
        events.sort(key=lambda e: (e[0], 0 if e[1] in ("assign", "for") else 1))
        for _line, kind, node in events:
            if kind == "assign":
                name, value = node.targets[0].id, node.value
                xml_id = self.uid_of(value, uids)
                if xml_id and not (isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute) and value.func.attr == "_get_service_env"):
                    uids[name] = xml_id
                elif isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute) and value.func.attr == "_get_service_env" and xml_id:
                    envs[name] = xml_id
                else:
                    self._bind(name, value, uids, envs, recs)
                continue
            if kind == "for":
                if isinstance(node.target, ast.Name):
                    self._bind(node.target.id, node.iter, uids, envs, recs)
                continue
            op = node.func.attr
            if op not in PERM_OF_OP and op not in EXTRA_PERMS and op != "message_post":
                continue
            rec = self.recset_of(node.func.value, uids, envs, recs)
            if not rec or not rec[0]:
                continue
            end = getattr(node, "end_lineno", None) or node.lineno
            if op == "message_post":
                yield (node.lineno, end, rec[0], "mail.message", "create")
                yield (node.lineno, end, rec[0], rec[1], "read")
            else:
                for perm in EXTRA_PERMS.get(op) or (PERM_OF_OP[op],):
                    yield (node.lineno, end, rec[0], rec[1], perm)


def check_file(path, repo_root, acl, fields=None):
    source, problem = read_source(path)
    if problem:
        return [problem]
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError:
        return []
    lines = source.splitlines()
    scan = FileScan(tree, lines, fields)
    found = []
    funcs = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            funcs += [(f, _class_model(node)) for f in node.body if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))]
    in_class = {id(f) for f, _m in funcs}
    funcs += [(f, None) for f in ast.walk(tree) if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and id(f) not in in_class]
    for func, scan.cls_model in funcs:
        for lineno, end, user, model, perm in scan.calls(func):
            if any(IGNORE_MARKER in line for line in lines[lineno - 1:end]):
                continue
            if acl.missing(user, model, perm):
                found.append(
                    f"{os.path.relpath(path, repo_root)}:{lineno} {user} has no {perm} ACL on {model} "
                    f"(no ir.model.access row for its groups): the call raises AccessError. Grant the row, "
                    f"use the right service account, or add '# {IGNORE_MARKER}: <why>'."
                )
    return sorted(set(found))


def check_service_account_acl(repo_roots, stats=None, core_addons=CORE_ADDONS):
    if isinstance(repo_roots, str):
        repo_roots = [repo_roots]
    acl = load_acl(repo_roots, core_addons)
    fields = field_models(repo_roots)
    violations = []
    for repo_root in repo_roots:
        for root, _dirs, files in tree_walk(repo_root, ".py", SKIP_DIRS, skip_hidden=True, untracked=True):
            for filename in files:
                if filename.endswith(".py") and not filename.startswith("test_"):
                    if stats is not None:
                        stats["files"] = stats.get("files", 0) + 1
                    violations.extend(check_file(os.path.join(root, filename), repo_root, acl, fields))
    return violations


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: check_service_account_acl.py <repo_root>")
        sys.exit(1)
    check_target.require_directory(sys.argv[1], "check_service_account_acl")
    stats = {}
    roots = _resolve_repo_roots(sys.argv[1])
    violations = check_service_account_acl(roots, stats)
    check_target.require_scanned(stats.get("files", 0), ", ".join(roots), "check_service_account_acl", "Python files")
    if violations:
        print("❌ Service account ACL violations:")
        for v in violations:
            print(f"  - {v}")
        sys.exit(1)
