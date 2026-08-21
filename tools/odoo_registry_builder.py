#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Odoo registry model builder -- foundation for the mypy plugin proposed in
docs/proposals/ODOO_AWARE_TYPE_CHECKING.md (Phase 2, step 1).

mypy has no concept of Odoo's registry-time class merging: each `_inherit`
class is just an unrelated class to it, so a real model's actual merged
attribute set (everything contributed by every module that extends it) is
invisible. This module builds that merged registry by static AST analysis --
walk every `_name`/`_inherit` declaration across hams_com and hams_open,
compose merged fields and methods per final model name -- so a future mypy
plugin has something real to resolve `env['some.model']`, `Many2one`/
`One2many`/`Many2many` comodel types, and `self.<attr>` access against,
instead of each file's own isolated class body.

The module-resolution and manifest-graph walking here mirrors
check_model_extension_collisions.py (ADR 0086) deliberately, not
independently reinvented -- that script is the proven, already-verified
half of this problem (it doesn't need fields/methods, only class-level
_name/_inherit metadata, so it stays a separate, narrower, already-hard-gated
check rather than being rewritten to depend on this heavier module).

Usage as a script (for direct inspection / verification):
    python3 odoo_registry_builder.py <repo_root> [model.name ...]
Prints the merged field/method set for the named models (or a summary of
the whole registry if none given).

Usage as a library (for the eventual mypy plugin):
    from odoo_registry_builder import build_registry
    registry = build_registry([hams_com_root, hams_open_root])
    model = registry.get("res.users")
    model.fields["partner_id"].comodel  # -> "res.partner"
Known limitation, found and confirmed while using this against a real question (does res.users
really have a website_id field? -- it does, via Odoo core's own `website` module,
`related='partner_id.website_id'`): this registry only scans hams_com and hams_open. Any field or
method contributed by Odoo's own core/standard addons (base, mail, website, portal, ...) is
invisible to it -- a real gap for a future mypy plugin built on this (it would false-positive on
every core-contributed attribute) that isn't solved yet. Confirmed via the real installed source at
/usr/lib/python3/dist-packages/odoo/addons/ rather than guessed; extending build_registry() to also
walk that tree (same _name/_inherit logic, different root) is the natural fix, not attempted here.
"""

import ast
import os
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional


MODEL_BASES = {"Model", "AbstractModel", "TransientModel"}
SKIP_DIRS = {"node_modules", "__pycache__", ".git", "daemons", "tools", "radae"}

# fields.<X>(...) constructors this builder understands. Relational ones
# (Many2one/One2many/Many2many) get their comodel resolved from the first
# positional string-literal argument; the rest are recorded with comodel=None
# but still matter for plain-attribute resolution (self.name, self.active, ...).
RELATIONAL_FIELD_TYPES = {"Many2one", "One2many", "Many2many"}
KNOWN_FIELD_TYPES = RELATIONAL_FIELD_TYPES | {
    "Char", "Text", "Html", "Boolean", "Integer", "Float", "Monetary",
    "Date", "Datetime", "Binary", "Selection", "Reference", "Json",
    "Image", "Properties",
}


@dataclass
class FieldInfo:
    name: str
    field_type: str
    comodel: Optional[str]
    module: str
    file: str
    lineno: int


@dataclass
class MethodInfo:
    name: str
    arg_names: List[str]          # positional/keyword-or-positional arg names, including self/cls
    posonly_count: int
    has_varargs: bool
    has_varkw: bool
    kwonly_names: List[str]
    module: str
    file: str
    lineno: int

    def min_args(self) -> int:
        """Minimum number of positional args a caller must supply (including
        self), i.e. args with no default. Used for the 'wrong arg count' bug
        class from the proposal doc's own motivating examples."""
        return self.posonly_count  # refined by the caller using defaults; see _extract_methods

    def max_args(self) -> Optional[int]:
        return None if self.has_varargs else len(self.arg_names)


@dataclass
class MergedModel:
    name: str
    fields: Dict[str, FieldInfo] = field(default_factory=dict)
    methods: Dict[str, MethodInfo] = field(default_factory=dict)
    # Every (module, file, lineno) that contributed to this merged model,
    # in the order encountered -- not load order (this is a static walk,
    # not a simulation of Odoo's actual module load sequence), but enough
    # to see every contributor for debugging a false positive.
    contributors: List[tuple] = field(default_factory=list)


def _find_sibling_repo(repo_root):
    """Mirrors check_model_extension_collisions.py's sibling-repo resolution."""
    repo_root = os.path.abspath(repo_root)
    for sibling_name in ("hams_open", "hams_com"):
        if os.path.basename(repo_root) == sibling_name:
            continue
        candidate = os.path.abspath(os.path.join(repo_root, "..", sibling_name))
        if not os.path.isdir(candidate):
            continue
        has_a_module = any(
            os.path.isfile(os.path.join(candidate, d, "__manifest__.py"))
            for d in os.listdir(candidate)
            if os.path.isdir(os.path.join(candidate, d))
        )
        if has_a_module:
            return candidate
    return None


def _is_model_class(node):
    for base in node.bases:
        if isinstance(base, ast.Attribute) and base.attr in MODEL_BASES:
            return True
        if isinstance(base, ast.Name) and base.id in MODEL_BASES:
            return True
    return False


def _literal_str_or_list(value_node):
    try:
        value = ast.literal_eval(value_node)
    except (ValueError, SyntaxError):
        return None
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return list(value)
    return None


def _module_of(filepath, cache):
    d = os.path.dirname(os.path.abspath(filepath))
    walked = []
    result = None
    while d and d != os.path.dirname(d):
        if d in cache:
            result = cache[d]
            break
        if os.path.isfile(os.path.join(d, "__manifest__.py")):
            result = os.path.basename(d)
            break
        walked.append(d)
        d = os.path.dirname(d)
    for wd in walked:
        cache[wd] = result
    return result


def _field_call_info(call_node):
    """Given an ast.Call that's the RHS of a field assignment, return
    (field_type, comodel) if it looks like a real fields.<X>(...) call this
    builder understands, else None. Handles both `fields.Many2one(...)` and
    a bare `Many2one(...)` (from `from odoo.fields import Many2one`)."""
    func = call_node.func
    if isinstance(func, ast.Attribute):
        ftype = func.attr
    elif isinstance(func, ast.Name):
        ftype = func.id
    else:
        return None
    if ftype not in KNOWN_FIELD_TYPES:
        return None
    comodel = None
    if ftype in RELATIONAL_FIELD_TYPES and call_node.args:
        first = call_node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            comodel = first.value
    return ftype, comodel


def _extract_fields(class_node, module, fpath):
    out = []
    for stmt in class_node.body:
        if not isinstance(stmt, ast.Assign):
            continue
        if not isinstance(stmt.value, ast.Call):
            continue
        info = _field_call_info(stmt.value)
        if info is None:
            continue
        ftype, comodel = info
        for target in stmt.targets:
            if isinstance(target, ast.Name):
                out.append(FieldInfo(
                    name=target.id, field_type=ftype, comodel=comodel,
                    module=module, file=fpath, lineno=stmt.lineno,
                ))
    return out


def _args_info(args_node: ast.arguments):
    """Flatten an ast.arguments node into (arg_names, posonly_count,
    n_defaults, has_varargs, has_varkw, kwonly_names). arg_names covers
    posonlyargs + args (i.e. everything callable positionally), matching
    what a caller's positional-arg-count actually has to satisfy."""
    posonly = [a.arg for a in args_node.posonlyargs]
    regular = [a.arg for a in args_node.args]
    arg_names = posonly + regular
    n_defaults = len(args_node.defaults)  # defaults apply to the trailing `regular` args
    has_varargs = args_node.vararg is not None
    has_varkw = args_node.kwarg is not None
    kwonly_names = [a.arg for a in args_node.kwonlyargs]
    return arg_names, len(posonly), n_defaults, has_varargs, has_varkw, kwonly_names


def _extract_methods(class_node, module, fpath):
    out = []
    for stmt in class_node.body:
        if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        arg_names, posonly_count, n_defaults, has_varargs, has_varkw, kwonly_names = _args_info(stmt.args)
        out.append(MethodInfo(
            name=stmt.name, arg_names=arg_names, posonly_count=len(arg_names) - n_defaults,
            has_varargs=has_varargs, has_varkw=has_varkw, kwonly_names=kwonly_names,
            module=module, file=fpath, lineno=stmt.lineno,
        ))
    return out


def _extract_class_info(class_node, module, fpath):
    """Returns (name_values, inherit_values, fields, methods) for one model
    class body. Field/method extraction always runs (a class contributes its
    attributes to the registry regardless of whether it's declaring a new
    model or extending one) -- only _name/_inherit determine *which* merged
    model entry/entries they land in."""
    name_values = None
    inherit_values = None
    for stmt in class_node.body:
        if not isinstance(stmt, ast.Assign):
            continue
        targets = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
        if "_name" in targets:
            name_values = _literal_str_or_list(stmt.value)
        elif "_inherit" in targets:
            inherit_values = _literal_str_or_list(stmt.value)
    fields_found = _extract_fields(class_node, module, fpath)
    methods_found = _extract_methods(class_node, module, fpath)
    return name_values, inherit_values, fields_found, methods_found


def _merge_into(registry: Dict[str, MergedModel], model_name, module, fpath, lineno, fields_found, methods_found):
    merged = registry.setdefault(model_name, MergedModel(name=model_name))
    merged.contributors.append((module, fpath, lineno))
    for f in fields_found:
        merged.fields[f.name] = f  # last-contributor-wins, same override semantics as Odoo itself for a re-declared field
    for m in methods_found:
        merged.methods[m.name] = m


def build_registry(roots) -> Dict[str, MergedModel]:
    """Walk every .py file under each root, find Odoo model classes, and
    return {final_model_name: MergedModel} with fields/methods composed
    across every _name/_inherit contributor. Mirrors
    check_model_extension_collisions.py's own walk (SKIP_DIRS, module
    resolution via nearest __manifest__.py) so the two tools agree on what
    counts as "a model class" and "which module owns this file"."""
    registry: Dict[str, MergedModel] = {}
    module_cache = {}

    for repo_root in roots:
        for root, dirs, files in os.walk(repo_root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                fpath = os.path.join(root, fname)
                mod = _module_of(fpath, module_cache)
                if not mod:
                    continue
                try:
                    tree = ast.parse(open(fpath, "r", encoding="utf-8").read(), filename=fpath)
                except (SyntaxError, OSError):
                    continue
                for node in ast.walk(tree):
                    if not isinstance(node, ast.ClassDef) or not _is_model_class(node):
                        continue
                    name_values, inherit_values, fields_found, methods_found = _extract_class_info(node, mod, fpath)
                    if name_values:
                        # _name always establishes/contributes to that model's
                        # identity, whether or not _inherit is also present
                        # (the mixin self-reference idiom: _name = "res.users",
                        # _inherit = ["res.users", "some.mixin"] both extends
                        # res.users AND is the thing check_model_extension_
                        # collisions.py calls "self-referencing" -- either way
                        # this class's fields/methods belong on res.users).
                        for nm in name_values:
                            _merge_into(registry, nm, mod, fpath, node.lineno, fields_found, methods_found)
                    elif inherit_values:
                        # Bare _inherit, no _name: a pure extension. A list here
                        # (rare without _name, but handled defensively, same as
                        # check_model_extension_collisions.py's inherit_only)
                        # extends each named target independently.
                        for tgt in inherit_values:
                            _merge_into(registry, tgt, mod, fpath, node.lineno, fields_found, methods_found)

    return registry


def _print_model(model: MergedModel):
    print(f"=== {model.name} ===")
    print(f"  {len(model.contributors)} contributing class(es):")
    for mod, fpath, lineno in model.contributors:
        print(f"    {mod}: {fpath}:{lineno}")
    print(f"  {len(model.fields)} field(s):")
    for fname in sorted(model.fields):
        f = model.fields[fname]
        comodel_str = f" -> {f.comodel}" if f.comodel else ""
        print(f"    {fname}: {f.field_type}{comodel_str}  ({f.module}:{f.lineno})")
    print(f"  {len(model.methods)} method(s):")
    for mname in sorted(model.methods):
        m = model.methods[mname]
        print(f"    {mname}({', '.join(m.arg_names)}{'  *args' if m.has_varargs else ''}{'  **kwargs' if m.has_varkw else ''})  ({m.module}:{m.lineno})")


def main():
    if len(sys.argv) < 2:
        print("Usage: odoo_registry_builder.py <repo_root> [model.name ...]")
        sys.exit(1)

    repo_root = os.path.abspath(sys.argv[1])
    roots = [repo_root]
    sibling = _find_sibling_repo(repo_root)
    if sibling:
        roots.append(sibling)

    registry = build_registry(roots)
    requested = sys.argv[2:]

    if requested:
        for name in requested:
            model = registry.get(name)
            if model is None:
                print(f"=== {name}: not found in registry ===")
                continue
            _print_model(model)
    else:
        multi_contributor = [m for m in registry.values() if len(m.contributors) > 1]
        print(f"Registry built: {len(registry)} models, {len(multi_contributor)} with 2+ contributing classes.")
        print("Top 10 by contributor count:")
        for m in sorted(multi_contributor, key=lambda m: -len(m.contributors))[:10]:
            print(f"  {m.name}: {len(m.contributors)} contributors, {len(m.fields)} fields, {len(m.methods)} methods")


if __name__ == "__main__":
    main()
