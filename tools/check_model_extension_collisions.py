#!/usr/bin/env python3
# This software is distributed under the terms of the Affero General Public License (AGPL-3).
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Odoo Own-Model Extension Collision Detector (ADR 0086)
--------------------------------------------------------
Catches the exact bug class a full-session audit found repeatedly in this
codebase: two modules independently declaring the same Odoo model, or one
module cross-extending another's _auto=False SQL-view model, both of which
Odoo's registry merges *silently* -- no error at install time, just
load-order-dependent behavior that can leave real fields with no backing
database column, methods that never actually run, or ir.model.access.csv
rows that never resolve. See:
  hams_shared/docs/adrs/0086_own_model_extension_consolidation.md

Two checks, both hard failures:

1. AMBIGUOUS MULTI-OWNER MODEL: two or more different modules each
   contain a class declaring `_name = "some.model"` for the *same* model
   name. Odoo merges same-_name classes with no _inherit relationship
   between them by load order alone -- exactly what happened to
   ham.elmer.topic and ham.dx.spot (and, combined with a missing
   self-reference in `_inherit`, ham.equipment's registry-crash). Fix:
   pick one module as the real owner (per ADR 0086, usually whichever
   loads first / whichever the referencing code most depends on) and
   change every other declaration to a bare `_inherit = "some.model"`
   with no `_name` -- or, if the two classes were never meant to be the
   same model, rename one.

2. CROSS-MODULE EXTENSION OF AN _auto=False MODEL: the module that
   declares `_name = "some.model"` also sets `_auto = False` (a
   hand-built or _table_query-driven SQL view), and a *different* module
   extends it via `_inherit = "some.model"`. ADR 0086 bans this outright,
   no exemption: these models build their table via a Python `init()`
   override, and Odoo has no super()-chaining contract for `init()`
   across `_inherit` -- a second init() override silently wins or loses
   by load order, and the losing side's fields have no backing column.
   This is exactly what happened to ham.repeater.public.view and
   ham.operator.index. Fix: move the extending module's fields/methods
   directly into the base model's own file.

Usage: check_model_extension_collisions.py <repo_root>
Always scans the full manifest graph (both hams_com and hams_open, like
check_dependency_cycles.py) regardless of any scoped target list, since a
collision is a property of the whole registry, not any single module.
"""

import ast
import os
import sys


MODEL_BASES = {"Model", "AbstractModel", "TransientModel"}
SKIP_DIRS = {"node_modules", "__pycache__", ".git", "daemons", "tools", "radae"}


def _resolve_repo_root(given_path):
    """run_linters.py's own `dir_path` (computed from its __file__, which lives inside
    hams_shared/tools/) resolves to the hams_shared directory itself, not a real repo root --
    confirmed directly: this checker was scanning 0 models (silently, since it's silent on
    success) via run_linters.py's actual invocation, versus 182 models when pointed at either
    real repo root directly. hams_shared has no Odoo modules of its own and isn't its own
    sibling, so _find_sibling_repo/_scan found nothing and this whole hard gate (ADR 0086) was a
    no-op in every CI run. Same fix as check_access_csv_group_order.py/
    check_module_subpackage_imports.py: detect the hams_shared case by name and redirect to its
    real parent repo."""
    given_path = os.path.abspath(given_path)
    if os.path.basename(given_path) == "hams_shared":
        return os.path.dirname(given_path)
    return given_path


def _find_sibling_repo(repo_root):
    """Mirrors run_linters.py's / check_dependency_cycles.py's sibling-repo
    resolution so the graph this script builds is complete regardless of
    which repo it's invoked from."""
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
    """True if this ClassDef looks like it derives from models.Model /
    AbstractModel / TransientModel (handles both `models.Model` and a
    bare `Model` import form)."""
    for base in node.bases:
        if isinstance(base, ast.Attribute) and base.attr in MODEL_BASES:
            return True
        if isinstance(base, ast.Name) and base.id in MODEL_BASES:
            return True
    return False


def _literal_str_or_list(value_node):
    """Best-effort literal_eval for `_name`/`_inherit` assignment values.
    Returns a list of strings (a single-string value becomes a 1-item
    list), or None if it isn't a literal this checker can reason about."""
    try:
        value = ast.literal_eval(value_node)
    except (ValueError, SyntaxError):
        return None
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return list(value)
    return None


def _extract_class_info(class_node):
    """Returns (name_values, inherit_values, auto_false, has_init) for one
    model class body."""
    name_values = None
    inherit_values = None
    auto_false = False
    has_init = False
    for stmt in class_node.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name == "init":
            has_init = True
        if not isinstance(stmt, ast.Assign):
            continue
        targets = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
        if "_name" in targets:
            name_values = _literal_str_or_list(stmt.value)
        elif "_inherit" in targets:
            inherit_values = _literal_str_or_list(stmt.value)
        elif "_auto" in targets:
            try:
                auto_false = ast.literal_eval(stmt.value) is False
            except (ValueError, SyntaxError):
                pass
    return name_values, inherit_values, auto_false, has_init


def _module_of(filepath, cache):
    """Walk up from filepath to the nearest ancestor directory containing
    __manifest__.py; returns that directory's basename, or None."""
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


def _scan(roots):
    # model_name -> list of (module, file, lineno)  ALL `_name = ...` decls,
    # including ones that also self-reference in _inherit.
    name_owners = {}
    # model_name -> list of (module, file, lineno)  only the *non*
    # self-referencing subset: `_name = X` where `_inherit` is absent, or
    # present but doesn't include X itself. A class with `_name = X` and
    # `_inherit` containing X is explicitly marking itself as an EXTENSION
    # of an existing model (Odoo's documented "extend + add a mixin"
    # idiom, e.g. edge_routing's `_name = "res.users"` +
    # `_inherit = ["res.users", "edge.routing.mixin"]") -- not a second
    # claim of ownership, so it's excluded from the ambiguity check below.
    claiming_owners = {}
    # model_name -> True if some `_name` declarer of it also set _auto = False
    auto_false_models = {}
    # list of (target_name, module, file, lineno)  (from an `_inherit`-only decl)
    inherit_only = []
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
                    with open(fpath, "r", encoding="utf-8") as f:
                        tree = ast.parse(f.read(), filename=fpath)
                except (SyntaxError, OSError):
                    continue
                for node in ast.walk(tree):
                    if not isinstance(node, ast.ClassDef) or not _is_model_class(node):
                        continue
                    name_values, inherit_values, auto_false, has_init = _extract_class_info(node)
                    del has_init  # not needed once check 2 keys off _auto alone
                    if name_values:
                        for nm in name_values:
                            name_owners.setdefault(nm, []).append((mod, fpath, node.lineno))
                            self_referencing = bool(inherit_values) and nm in inherit_values
                            if not self_referencing:
                                claiming_owners.setdefault(nm, []).append((mod, fpath, node.lineno))
                            if auto_false:
                                auto_false_models[nm] = True
                    elif inherit_values:
                        for tgt in inherit_values:
                            inherit_only.append((tgt, mod, fpath, node.lineno))

    return name_owners, claiming_owners, auto_false_models, inherit_only


def main():
    if len(sys.argv) < 2:
        print("Usage: check_model_extension_collisions.py <repo_root>")
        sys.exit(1)

    repo_root = _resolve_repo_root(sys.argv[1])
    roots = [repo_root]
    sibling = _find_sibling_repo(repo_root)
    if sibling:
        roots.append(sibling)

    name_owners, claiming_owners, auto_false_models, inherit_only = _scan(roots)
    errors_found = False

    # Check 1: ambiguous multi-owner model
    for model_name, owners in claiming_owners.items():
        modules = sorted({m for m, _f, _l in owners})
        if len(modules) < 2:
            continue
        print(f"🚨 AMBIGUOUS MODEL MERGE: '{model_name}' declared with _name by {len(modules)} different modules")
        for mod, fpath, lineno in owners:
            print(f"  {mod}: {fpath}:{lineno}")
        print(
            "  Error: Odoo merges same-_name classes with no formal _inherit "
            "relationship between them by load order alone -- which module's "
            "ir.model.access.csv rows resolve, and which class's method "
            "overrides actually run, is undefined. See ADR 0086."
        )
        print(
            "  Fix: pick one module as the real owner and change every other "
            "declaration to a bare `_inherit = \"" + model_name + "\"` with no _name."
        )
        errors_found = True

    # Check 2: cross-module extension of an _auto=False model
    owner_module_of = {}
    for model_name, owners in claiming_owners.items():
        # Only meaningful when there's a single real owner; an ambiguous
        # multi-owner case is already reported by check 1.
        modules = {m for m, _f, _l in owners}
        if len(modules) == 1:
            owner_module_of[model_name] = next(iter(modules))

    for target_name, mod, fpath, lineno in inherit_only:
        if target_name not in auto_false_models:
            continue
        owner_mod = owner_module_of.get(target_name)
        if owner_mod is None or owner_mod == mod:
            continue
        print(f"🚨 CROSS-MODULE EXTENSION OF AN _auto=False MODEL: '{target_name}'")
        print(f"  Owned (with _auto = False) by: {owner_mod}")
        print(f"  Extended by: {mod}: {fpath}:{lineno}")
        print(
            "  Error: this model builds its table via a hand-written or "
            "_table_query-driven init(). Odoo has no super()-chaining "
            "contract for init() across _inherit, so a second module's "
            "fields silently have no backing database column. Banned "
            "outright per ADR 0086, rule 2 -- no exemption."
        )
        print(
            f"  Fix: move {mod}'s fields/methods for this model directly "
            f"into {owner_mod}'s own base model file."
        )
        errors_found = True

    if errors_found:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
