#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
GDPR personal-data coverage report
-------------------------------------------------------------------------------------------
Reads the model definitions of one or more Odoo repos (AST only, nothing is imported or run) and lists every
persistent model that holds personal data -- a many2one to res.users or res.partner, or an email, phone, street
address, IP address or latitude/longitude field -- with whether the model is named in a module that implements the
data-subject export (`_get_gdpr_export_data` / `_get_gdpr_streamed_keys`), in a module that implements the erasure
(`_execute_gdpr_erasure`), or in the reviewed list.

A model that is none of those is FLAGGED: a person's data may sit there that neither export nor erasure reaches.
The reviewed list (gdpr_coverage_reviewed.json next to this file: {"model.name": "why it needs no hook"}) records the
models someone read and judged (public reference data, staff-only logs, kept by a recorded decision, ...).

Matching is by the model name as a string literal anywhere in a file that defines the hook, so it can say "covered"
for a model that is only mentioned; it is a review aid, not proof. Models that are transient (wizards), have no table
(`_auto = False`, SQL views) or are abstract are skipped.

Usage: gdpr_personal_data_coverage.py [--strict] [--table] <repo_root> [<repo_root> ...]
Default prints the flagged models and exits 0; --strict exits 1 when any is flagged; --table prints every model.
"""

import argparse
import ast
import collections
import json
import os
import re
import sys

SKIP_DIRS = {"node_modules", "__pycache__", ".git", "tests", "migrations", "daemons", "tools"}
PERSONAL_NAME = re.compile(
    r"(^|_)(email|phone|mobile|street2?|ip_address|remote_addr|latitude|longitude|first_name|last_name|birth_date)($|_)"
)
USER_COMODELS = {"res.users", "res.partner"}
RELATIONAL = {"Many2one", "Many2many", "One2many"}
EXPORT_HOOKS = {"_get_gdpr_export_data", "_get_gdpr_streamed_keys"}
ERASURE_HOOKS = {"_execute_gdpr_erasure"}
REVIEWED_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gdpr_coverage_reviewed.json")


def _py_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name.endswith(".py") and not name.startswith("test_"):
                yield os.path.join(dirpath, name)


def _const(node):
    return node.value if isinstance(node, ast.Constant) else None


def _class_model(cls):
    """(_name, _inherit-as-string, is_transient, no_table) of a class body."""
    name = inherit = None
    no_table = False
    for stmt in cls.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            target, value = stmt.targets[0].id, _const(stmt.value)
            if target == "_name":
                name = value
            elif target == "_inherit" and isinstance(value, str):
                inherit = value
            elif target == "_auto" and value is False:
                no_table = True
    bases = {b.attr if isinstance(b, ast.Attribute) else getattr(b, "id", "") for b in cls.bases}
    return name, inherit, bool({"TransientModel", "AbstractModel"} & bases), no_table


def _personal_fields(cls):
    found = []
    for stmt in cls.body:
        if not (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name)):
            continue
        if not (isinstance(stmt.value, ast.Call) and isinstance(stmt.value.func, ast.Attribute)):
            continue
        kind, field = stmt.value.func.attr, stmt.targets[0].id
        comodel = _const(stmt.value.args[0]) if stmt.value.args else None
        for kw in stmt.value.keywords:
            if kw.arg == "comodel_name":
                comodel = _const(kw.value)
        if kind in RELATIONAL and comodel in USER_COMODELS:
            ondelete = next((_const(kw.value) for kw in stmt.value.keywords if kw.arg == "ondelete"), None)
            # A many2one's default on delete of the user is "set null"; "restrict" blocks an administrator's hard delete.
            found.append(f"{field}->{comodel}" + (f"({ondelete or 'set null'})" if kind == "Many2one" else ""))
        elif kind in {"Char", "Text", "Float"} and PERSONAL_NAME.search(field):
            found.append(f"{field}:{kind}")
    return found


def scan_models(roots):
    """{model: {"module": str, "fields": [..]}} for persistent models with personal-data fields."""
    models = collections.defaultdict(lambda: {"module": "", "fields": [], "skip": False})
    for root in roots:
        for path in _py_files(root):
            try:
                with open(path, encoding="utf-8") as handle:
                    tree = ast.parse(handle.read())
            except (OSError, SyntaxError, UnicodeDecodeError):
                continue
            for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
                name, inherit, no_model, no_table = _class_model(cls)
                model = name or inherit
                if not model:
                    continue
                entry = models[model]
                entry["module"] = entry["module"] or os.path.relpath(path, root).split(os.sep)[0]
                entry["skip"] = entry["skip"] or no_model or no_table
                entry["fields"].extend(_personal_fields(cls))
    return {m: e for m, e in models.items() if e["fields"] and not e["skip"]}


def hook_sources(roots):
    """Concatenated source of every file defining an export hook, and of every file defining an erasure hook."""
    export_src, erasure_src = [], []
    for root in roots:
        for path in _py_files(root):
            try:
                with open(path, encoding="utf-8") as handle:
                    src = handle.read()
            except (OSError, UnicodeDecodeError):
                continue
            if any(f"def {hook}" in src for hook in EXPORT_HOOKS):
                export_src.append(src)
            if any(f"def {hook}" in src for hook in ERASURE_HOOKS):
                erasure_src.append(src)
    return "\n".join(export_src), "\n".join(erasure_src)


def coverage(roots, reviewed=None):
    """Rows (model, module, fields, exported, erased, reviewed_reason) sorted by model."""
    reviewed = reviewed or {}
    export_src, erasure_src = hook_sources(roots)
    rows = []
    for model, entry in sorted(scan_models(roots).items()):
        literal = re.compile(r"""['"]%s['"]""" % re.escape(model))
        rows.append(
            (model, entry["module"], sorted(set(entry["fields"])), bool(literal.search(export_src)),
             bool(literal.search(erasure_src)), reviewed.get(model))
        )
    return rows


def flagged(rows):
    return [r for r in rows if not (r[3] or r[4] or r[5])]


def load_reviewed(path=REVIEWED_PATH):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("roots", nargs="+")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--table", action="store_true")
    args = parser.parse_args(argv)
    rows = coverage([os.path.abspath(r) for r in args.roots], load_reviewed())
    shown = rows if args.table else flagged(rows)
    for model, module, fields, exported, erased, reason in shown:
        status = "export" if exported else "-"
        status += "/erasure" if erased else "/-"
        print(f"{model} [{module}] {status}{' reviewed: ' + reason if reason else ''} :: {', '.join(fields)}")
    print(f"{len(flagged(rows))} flagged of {len(rows)} personal-data models", file=sys.stderr)
    return 1 if args.strict and flagged(rows) else 0


if __name__ == "__main__":
    sys.exit(main())
