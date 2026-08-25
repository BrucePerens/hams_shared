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
    class_name: str = ""  # the contributing class's own Python identifier --
    # needed by the mypy plugin (ODOO_AWARE_TYPE_CHECKING.md Phase 2 steps
    # 2-4) to know *which* class in `file` to attach synthesized attributes
    # to; defaulted rather than required so any external caller built
    # against the pre-existing 6-field shape doesn't break.


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
    class_name: str = ""  # see FieldInfo.class_name's own comment
    calls_super: bool = False  # see find_suspicious_redeclarations' own comment on why this matters

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
    # `fields`/`methods` hold one "resolved" entry per name -- the last one
    # encountered in this walk's own order (NOT Odoo's real module-load
    # order; see the contributors comment below). That resolution is
    # necessarily a guess about which contributor "wins" and is only ever
    # meant for a consumer that needs exactly one answer (e.g. the mypy
    # plugin resolving `self.foo`'s type). It must never be the only place
    # this information lives: per the user directly, silently discarding
    # every other contributor here -- with no way to tell "Odoo's own
    # legitimate _inherit override" apart from "two unrelated modules
    # accidentally picked the same name" -- is exactly the kind of silent
    # failure this whole registry exists to stop happening elsewhere.
    # `field_contributions`/`method_contributions` below keep the full,
    # non-silent history; see find_suspicious_redeclarations().
    fields: Dict[str, FieldInfo] = field(default_factory=dict)
    methods: Dict[str, MethodInfo] = field(default_factory=dict)
    field_contributions: Dict[str, List[FieldInfo]] = field(default_factory=dict)
    method_contributions: Dict[str, List[MethodInfo]] = field(default_factory=dict)
    # Every (module, file, lineno, class_name) that contributed to this
    # merged model, in the order encountered -- not load order (this is a
    # static walk, not a simulation of Odoo's actual module load sequence),
    # but enough to see every contributor for debugging a false positive.
    contributors: List[tuple] = field(default_factory=list)


def find_odoo_core_addons_path():
    """Locates the real installed Odoo core/standard addons tree (base,
    mail, website, portal, ...) -- e.g. /usr/lib/python3/dist-packages/
    odoo/addons on this box. Closes the real gap this module's own
    docstring used to flag as unsolved: res.users alone is extended by
    ~25 files across hams_com/hams_open, but real fields like website_id
    only exist via Odoo core's own website module -- confirmed directly
    against /usr/lib/python3/dist-packages/odoo/addons/website/models/
    res_users.py. Without this, every core-contributed attribute would
    false-positive in a consumer like the mypy plugin
    ODOO_AWARE_TYPE_CHECKING.md describes. Returns None (not a hard
    failure) if Odoo isn't importable in the current environment --
    callers should treat that as "core coverage unavailable here", not
    crash the whole registry build over it.
    """
    try:
        import odoo
    except ImportError:
        odoo = None

    candidates = []
    if odoo is not None:
        # __file__ can genuinely be None here (confirmed directly on this
        # box: this install resolves as a namespace-style package) --
        # __path__ is the real fallback for that case, not a guess.
        if getattr(odoo, "__file__", None):
            candidates.append(os.path.join(os.path.dirname(odoo.__file__), "addons"))
        for p in getattr(odoo, "__path__", []) or []:
            candidates.append(os.path.join(p, "addons"))
    # Last resort: the real, confirmed install location on this box and
    # other common Debian/Ubuntu packaging locations, same "don't just
    # trust the Python import, also check where it really lives" pattern
    # this codebase already uses for ardopcf/mercury/pat binary discovery.
    candidates.append("/usr/lib/python3/dist-packages/odoo/addons")

    for c in candidates:
        if os.path.isdir(c):
            return c
    return None


def _manifest_depends(manifest_path):
    """Reads just the 'depends' list out of one __manifest__.py via AST
    (never imports/executes the manifest), returns [] if missing/unparseable."""
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=manifest_path)
    except (SyntaxError, OSError):
        return []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "depends":
                    return [e.value for e in value.elts if isinstance(e, ast.Constant)]
    return []


def find_needed_core_modules(hams_roots, core_addons_path):
    """Per the user directly: track only the Odoo core modules hams_com/
    hams_open actually depend on, not the full ~700-module install --
    walking everything would be slower for no benefit, and worse, would
    be actively *wrong*: it would let the registry (and anything built on
    it, like the mypy plugin) believe a field/method exists via a core
    module this codebase never actually installs, which is a false
    negative waiting to happen, not just noise.

    Computes the transitive closure: every hams_com/hams_open module's own
    'depends' list gives the direct frontier of core-module names, then
    BFS through those core modules' *own* manifests (also real 'depends'
    lists, read the same way) to pull in their transitive dependencies
    too -- e.g. depending on 'website' needs 'website's own dependencies
    (portal, http_routing, ...) even though no hams_com/hams_open manifest
    names them directly. Returns a set of core module directory names.
    """
    frontier = set()
    for repo_root in hams_roots:
        for root, dirs, files in os.walk(repo_root):
            if "radae" in dirs:
                dirs.remove("radae")
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            if "__manifest__.py" not in files:
                continue
            frontier.update(_manifest_depends(os.path.join(root, "__manifest__.py")))

    hams_module_names = set()
    for repo_root in hams_roots:
        for root, dirs, files in os.walk(repo_root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            if "__manifest__.py" in files:
                hams_module_names.add(os.path.basename(root))

    needed = set()
    queue = [m for m in frontier if m not in hams_module_names]
    while queue:
        mod = queue.pop()
        if mod in needed:
            continue
        needed.add(mod)
        manifest_path = os.path.join(core_addons_path, mod, "__manifest__.py")
        for dep in _manifest_depends(manifest_path):
            if dep not in needed and dep not in hams_module_names:
                queue.append(dep)
    return needed


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
                    class_name=class_node.name,
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


def _calls_super(func_node):
    """True if this function body contains any `super()` call anywhere
    (not just as the very first statement -- a real method can validate
    args, log, or short-circuit before delegating). A crude but honest
    static signal: it can't know whether the super() call actually reaches
    a *different* contributor's implementation of this same name (that
    depends on Odoo's real MRO at runtime, which this static tool doesn't
    simulate), only that the method is written in the cooperative-
    override style rather than fully replacing whatever it shadows."""
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "super":
            return True
    return False


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
            class_name=class_node.name, calls_super=_calls_super(stmt),
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


def _merge_into(registry: Dict[str, MergedModel], model_name, module, fpath, lineno, class_name, fields_found, methods_found):
    merged = registry.setdefault(model_name, MergedModel(name=model_name))
    merged.contributors.append((module, fpath, lineno, class_name))
    for f in fields_found:
        merged.fields[f.name] = f  # "resolved" value -- see MergedModel's own comment on why this alone isn't the source of truth
        merged.field_contributions.setdefault(f.name, []).append(f)
    for m in methods_found:
        merged.methods[m.name] = m
        merged.method_contributions.setdefault(m.name, []).append(m)


def find_suspicious_redeclarations(registry: Dict[str, MergedModel]):
    """A field/method name declared by more than one *different* (module,
    class_name) pair on the same merged model isn't automatically a bug --
    Odoo's own _inherit semantics legitimately let a later-loaded module
    override an earlier one's field (change its string/default/selection,
    tighten a method's behavior, etc.). But until this function existed,
    that case was completely indistinguishable from an accidental
    cross-module name collision (two unrelated modules picking the same
    method name by coincidence, silently shadowing one real implementation
    with an unrelated one) -- both looked identical: the loser just
    vanished with no record. This surfaces every such name instead of
    silently resolving it, so a human (or a future stricter check) can
    actually judge each case. Two contributions from the very same
    (module, class_name) -- e.g. a field assigned twice in one class body,
    unusual but not a cross-module concern -- are not flagged here.

    Returns {model_name: {name: (contributions, likely_cooperative)}} for
    every field/method name with 2+ distinct (module, class_name)
    contributors. `likely_cooperative` is True for methods where *all but
    at most one* contributor calls super() -- the real, common Odoo idiom
    of each module chaining onto the last (e.g. `SELF_WRITEABLE_FIELDS`
    implemented as a property every contributor extends via
    `return super().SELF_WRITEABLE_FIELDS + [...]`). The "at most one"
    allowance (not "every single one") is deliberate and confirmed
    necessary against real data: whichever contributor originally defines
    the property has no earlier implementation to chain onto and
    legitimately never calls super() itself -- requiring literally all of
    them produced a false "worth a look" on this codebase's own real,
    already-verified-correct SELF_WRITEABLE_FIELDS chain (ham_base/
    ham_callbook/ham_logbook/ham_satellite all extend it via super(); only
    Odoo core's own root definition doesn't). This can't identify *which*
    contributor is the true root without real module-load order, so it
    stays a count-based heuristic, not a claim about which one is root.
    Always False for fields (a `fields.Char(...)` assignment has no
    super() to call) and for any method with 2+ non-super contributors,
    which is the stronger, more actionable signal: either a deliberate
    full replacement (worth knowing, still not necessarily a bug) or a
    genuine accidental collision.
    """
    suspicious: Dict[str, Dict[str, tuple]] = {}
    for model_name, merged in registry.items():
        all_contributions = list(merged.field_contributions.items()) + list(merged.method_contributions.items())
        for name, contributions in all_contributions:
            distinct_sources = {(c.module, c.class_name) for c in contributions}
            if len(distinct_sources) <= 1:
                continue
            non_super_count = sum(1 for c in contributions if not getattr(c, "calls_super", False))
            likely_cooperative = non_super_count <= 1
            suspicious.setdefault(model_name, {})[name] = (contributions, likely_cooperative)
    return suspicious


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
                    with open(fpath, "r", encoding="utf-8") as f:
                        tree = ast.parse(f.read(), filename=fpath)
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
                            _merge_into(registry, nm, mod, fpath, node.lineno, node.name, fields_found, methods_found)
                    elif inherit_values:
                        # Bare _inherit, no _name: a pure extension. A list here
                        # (rare without _name, but handled defensively, same as
                        # check_model_extension_collisions.py's inherit_only)
                        # extends each named target independently.
                        for tgt in inherit_values:
                            _merge_into(registry, tgt, mod, fpath, node.lineno, node.name, fields_found, methods_found)

    return registry


def _redeclaration_marker(contributions):
    distinct_sources = {(c.module, c.class_name) for c in contributions}
    if len(distinct_sources) <= 1:
        return ""
    sources = ", ".join(f"{mod}.{cls}" for mod, cls in sorted(distinct_sources))
    return f"  [!] REDECLARED by {len(distinct_sources)} sources: {sources} -- resolved value shown is last-seen, not necessarily Odoo's real load order"


def _print_model(model: MergedModel):
    print(f"=== {model.name} ===")
    print(f"  {len(model.contributors)} contributing class(es):")
    for mod, fpath, lineno, class_name in model.contributors:
        print(f"    {mod}: {fpath}:{lineno} (class {class_name})")
    print(f"  {len(model.fields)} field(s):")
    for fname in sorted(model.fields):
        f = model.fields[fname]
        comodel_str = f" -> {f.comodel}" if f.comodel else ""
        marker = _redeclaration_marker(model.field_contributions.get(fname, []))
        print(f"    {fname}: {f.field_type}{comodel_str}  ({f.module}:{f.lineno}){marker}")
    print(f"  {len(model.methods)} method(s):")
    for mname in sorted(model.methods):
        m = model.methods[mname]
        marker = _redeclaration_marker(model.method_contributions.get(mname, []))
        print(f"    {mname}({', '.join(m.arg_names)}{'  *args' if m.has_varargs else ''}{'  **kwargs' if m.has_varkw else ''})  ({m.module}:{m.lineno}){marker}")


def main():
    if len(sys.argv) < 2:
        print("Usage: odoo_registry_builder.py <repo_root> [model.name ...]")
        sys.exit(1)

    repo_root = os.path.abspath(sys.argv[1])
    hams_roots = [repo_root]
    sibling = _find_sibling_repo(repo_root)
    if sibling:
        hams_roots.append(sibling)

    roots = list(hams_roots)
    core_addons_path = find_odoo_core_addons_path()
    if core_addons_path:
        needed_core_modules = find_needed_core_modules(hams_roots, core_addons_path)
        roots.extend(os.path.join(core_addons_path, m) for m in needed_core_modules)
        print(f"[*] Odoo core addons available -- including {len(needed_core_modules)} core module(s) "
              f"actually depended on (not the full install).", file=sys.stderr)
    else:
        print("[!] Odoo core addons not found in this environment -- registry will be blind to any "
              "field/method Odoo's own base/mail/website/etc. contribute (see find_odoo_core_addons_path's "
              "own doc comment).", file=sys.stderr)

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

        # Per the user directly: never let a redeclaration -- an accidental
        # cross-module name collision indistinguishable, until now, from a
        # deliberate Odoo _inherit override -- resolve silently. Surfaced
        # by default, not behind a flag nobody remembers to pass.
        suspicious = find_suspicious_redeclarations(registry)
        # Split by likely_cooperative so the actionable half isn't buried
        # under the (much larger, mostly-harmless) super()-chaining half --
        # see find_suspicious_redeclarations' own doc comment for exactly
        # what that split does and doesn't prove.
        worth_a_look: Dict[str, list] = {}
        likely_fine: Dict[str, list] = {}
        for model_name, names in suspicious.items():
            for name, (contributions, likely_cooperative) in names.items():
                bucket = likely_fine if likely_cooperative else worth_a_look
                bucket.setdefault(model_name, []).append(name)

        total_worth_a_look = sum(len(v) for v in worth_a_look.values())
        total_likely_fine = sum(len(v) for v in likely_fine.values())
        print(f"\nRedeclared names (same field/method name, 2+ different contributing classes): "
              f"{total_worth_a_look} worth a look, {total_likely_fine} likely fine "
              f"(every contributor calls super(), the normal Odoo cooperative-override idiom).")
        if worth_a_look:
            print("Top 10 models by worth-a-look redeclaration count:")
            by_count = sorted(worth_a_look.items(), key=lambda kv: -len(kv[1]))[:10]
            for model_name, names in by_count:
                print(f"  {model_name}: {len(names)} name(s) -- {', '.join(sorted(names)[:5])}"
                      f"{', ...' if len(names) > 5 else ''}")


if __name__ == "__main__":
    main()
