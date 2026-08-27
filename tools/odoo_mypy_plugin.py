#!/usr/bin/env python3
# This software is distributed under the terms of the GNU General Public License, version 3.
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Odoo-aware mypy plugin (ODOO_AWARE_TYPE_CHECKING.md Phase 2, steps 2-4)
-------------------------------------------------------------------------------------------
mypy has no concept of Odoo's registry-time class merging: each `_inherit` class is just an
unrelated class to it, so `self.<attr>` only ever sees what's declared in that one file's own
class body, not the real, merged attribute set every other `_inherit` contributor adds. That's
the exact bug class this whole project exists to catch (a method call with the wrong arg count,
a typo'd attribute name) -- but naive mypy can't do it: run against real `_inherit` classes, it
produces nothing but false positives ("BaseModel has no attribute X" for X that's really there,
just declared in a different file), which is worse than not checking at all.

The mechanism, proven against real synthetic and real production code in this session before
writing this file: `get_customize_class_mro_hook` synthesizes a shared MRO across the
textually-unrelated classes that share a `_name`/`_inherit` value, using
`odoo_registry_builder.py`'s already-built, already-verified registry to know which classes those
are. Once mypy's own MRO includes every sibling, its own *native*, already-correct attribute/type
resolution does the actual merging -- no hand-written attribute-resolution hook needed for that
part. Confirmed empirically (not assumed) that this survives real production code where the
`Model`/`AbstractModel`/`TransientModel` base itself resolves to `Any` under
`ignore_missing_imports` (Odoo ships no py.typed marker, no .pyi stubs -- confirmed directly
against the real installed odoo package).

The MRO hook alone is NOT sufficient, and this plugin does not rely on it alone:
`_inherit` siblings never import each other, so mypy's own file-processing order is not reliable
for them -- on a real multi-file run, `ctx.api.lookup_fully_qualified_or_none` sometimes returns
`None` for a sibling that genuinely is in the checked file set, simply because it hasn't been
analyzed yet at the point the hook runs (confirmed by test: reliable for a 2-file case, silently
drops siblings across a larger real run). `get_additional_deps` below is load-bearing, not
optional -- it forces mypy's own build graph to treat every `_inherit` contributor's module as
depending on its siblings' modules, the same technique `django-stubs` uses for the equivalent
Django problem. `test_odoo_mypy_plugin.py`'s
`test_get_additional_deps_pulls_in_the_sibling_even_when_not_explicitly_passed` is written to fail
if this hook is ever removed while the MRO hook alone remains -- see that test before assuming the
MRO hook by itself is enough.

Real-code false-positive rate, measured not assumed (`ODOO_AWARE_TYPE_CHECKING.md` has the full
trace): a whole-tree run against all 26 real hams_com/hams_open contributors to `res.users`, with
`check_untyped_defs = True`, went from 393 errors before the base-ORM stub existed, to 72 after it,
to 43 after `get_additional_deps`, to 19 after also dropping `_name`/`_inherit` from injected
siblings' own symbol tables (see `get_customize_class_mro_hook` below). Not yet
false-positive-clean -- most of the remaining 19 trace to core Odoo's own contributors, which are
out of this plugin's current reach (`_fullname_for_contributor` only matches `hams_com`/
`hams_open` paths).

Comodel resolution (`fields.Many2one("res.users", ...)` -> the real merged `res.users` type) and
`env['some.model']` resolution are NOT yet implemented in this first version -- see the "Not yet
done" section at the bottom. This file's first job is the MRO-merging mechanism alone, verified
working, before adding more surface.

Usage: add `plugins = hams_shared/tools/odoo_mypy_plugin.py` to a mypy.ini section. Requires
`ignore_missing_imports = True` (or an equivalent per-module override for `odoo.*`) in the same
config -- this plugin does not itself solve "does mypy know what odoo.models.Model looks like",
only "does it know what this codebase's own classes derived from it look like once merged".
`check_untyped_defs = True` and `follow_imports = silent` are both load-bearing for real Odoo code
(untyped method bodies are the norm, and `follow_imports = silent` keeps transitively-imported
files analyzed -- so siblings stay findable -- without reporting noise from files you didn't ask
to check). `follow_untyped_imports = True` is ALSO load-bearing as of the later session documented
just below, specifically for resolving any core-Odoo (`odoo.addons.*`) sibling/comodel/env target:
real Odoo ships no `py.typed` marker anywhere in its own source, and mypy's module finder treats an
untyped third-party import as `FOUND_WITHOUT_TYPE_HINTS` -- silently never parsed into a real
module, regardless of whether `_fullname_for_contributor` derives the textually-correct dotted
name for it -- unless this option tells mypy to analyze it anyway. Confirmed directly this session
(not assumed): the same 26-file run's error count did not move on `has_group`/`email`/`partner_id`
until this option was added, and DID move once it was.

**Update, a later session: steps 2 and 4 (comodel and env[...] resolution) are now implemented --
see `get_function_hook`/`get_method_hook`/`_resolve_model_instance` below.** Both needed the same
two-part fix, confirmed empirically before being built, not assumed: (1) `odoo_type_stubs/odoo/
fields.pyi` and `odoo_type_stubs/odoo/api.pyi` had to turn `Many2one`/`One2many`/`Many2many` and
`Environment` from blanket `Any` into real (if deliberately narrow) stub symbols, because a mypy
`get_function_hook`/`get_method_hook` can never fire on a call whose callee mypy has already
resolved to `Any` -- confirmed directly by registering the hooks against the old all-`Any` stubs
first and observing they never ran. (2) `get_additional_deps` needed a second extension beyond the
`_inherit`-sibling one step 3 already had: a comodel string (`fields.Many2one('res.partner', ...)`)
or an `env['res.partner']` literal essentially never corresponds to an actual Python import of that
model's module (Odoo code references models by string, not by importing their defining file), so
without forcing at least one contributor module into the build graph, `named_generic_type` fails
with `KeyError` for the common case, not an edge case. See `_file_comodel_targets`/
`_file_env_targets` and `_compute_comodel_targets`/`_compute_env_targets` below.

Also root-caused and fixed in the same session, not just step 2/4 additions: `_compute_sibling_map`
had a real bug where `_class_siblings[fn] = [...]` (assignment, not accumulation) silently
overwrote a contributor class's sibling list whenever that same class fullname contributed to more
than one model -- exactly the mixin self-reference idiom (`_inherit = ["res.users", "some.mixin"]`,
no `_name`) already documented above as *correctly* merged at the registry level. The class-level
MRO injection was not correctly merged: whichever model was processed last in registry-iteration
order silently won, and the other's entire sibling list -- not just one or two attributes --
vanished from that class's own MRO. This was the real, root cause behind the `has_group`/
`partner_id`/`email` core-Odoo resolution gap this doc's previous update had left as "leading
suspect, not yet confirmed" (`odoo.addons` module resolution needing `follow_untyped_imports = True`
was a second, separate, also-real fix required to make core Odoo's own contributor modules
resolvable at all -- see `ODOO_AWARE_TYPE_CHECKING.md`'s dated section for this session for the
full trace of both fixes together). See `_compute_sibling_map`'s own updated comment for the
accumulate-vs-overwrite fix in detail.
"""

import os
import sys
from typing import Callable, Dict, List, Optional, Set

from mypy.nodes import MypyFile, StrExpr, TypeInfo
from mypy.plugin import ClassDefContext, FunctionContext, MethodContext, Plugin
from mypy.types import AnyType, Instance, Type, TypeOfAny

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import odoo_registry_builder as orb  # noqa: E402


def _repo_roots():
    """Same resolution odoo_registry_builder.py's own main() uses: this
    file's own location is hams_shared/tools/, so hams_com and hams_open
    are both two directories up from there (hams_shared lives inside one
    of the two sibling repos, same as the rest of this tool family)."""
    tools_dir = os.path.dirname(os.path.abspath(__file__))
    hams_shared_dir = os.path.dirname(tools_dir)
    repo_root = os.path.dirname(hams_shared_dir)
    roots = [repo_root]
    sibling = orb._find_sibling_repo(repo_root)
    if sibling:
        roots.append(sibling)
    return roots


def _build_real_registry():
    hams_roots = _repo_roots()
    roots = list(hams_roots)
    core_addons_path = orb.find_odoo_core_addons_path()
    if core_addons_path:
        needed = orb.find_needed_core_modules(hams_roots, core_addons_path)
        roots.extend(os.path.join(core_addons_path, m) for m in needed)
    return orb.build_registry(roots)


def _module_fullname_for_file(
    fpath: str,
    hams_roots: List[str],
    core_addons_path: Optional[str] = None,
) -> Optional[str]:
    """The dotted module path mypy identifies a *file* by (no class name
    suffix) -- factored out of _fullname_for_contributor below so callers
    that only have a file path (get_additional_deps' comodel/env[...]
    target resolution, added for Phase 2 steps 2 and 4) don't need a class
    name they don't have. See _fullname_for_contributor's own docstring
    for the core-Odoo-vs-hams_roots distinction this shares with it."""
    fpath_abs = os.path.abspath(fpath)
    if core_addons_path:
        core_abs = os.path.abspath(core_addons_path)
        if fpath_abs.startswith(core_abs + os.sep):
            rel = os.path.relpath(fpath_abs, core_abs)
            if rel.endswith(".py"):
                rel = rel[: -len(".py")]
            dotted = rel.replace(os.sep, ".")
            return f"odoo.addons.{dotted}"

    for root in hams_roots:
        root_abs = os.path.abspath(root)
        if fpath_abs.startswith(root_abs + os.sep):
            rel = os.path.relpath(fpath_abs, root_abs)
            if rel.endswith(".py"):
                rel = rel[: -len(".py")]
            return rel.replace(os.sep, ".")
    return None


def _fullname_for_contributor(
    module: str,
    fpath: str,
    class_name: str,
    hams_roots: List[str],
    core_addons_path: Optional[str] = None,
) -> Optional[str]:
    """Odoo_registry_builder.py records each contributor's file path and
    class name, but not the Python dotted module path mypy identifies
    classes by (fullname = "<dotted.module.path>.<ClassName>"). Derives
    it the same way Python's own import system would: the file's path
    relative to whichever repo root contains it, with path separators
    turned into dots and the .py suffix dropped.

    Core Odoo addons are a real, separate case, not just another root:
    confirmed directly (`from odoo.addons import base; base.__name__`)
    that Odoo's own addons import as `odoo.addons.<module>...`, not
    `<module>...` the way this codebase's own hams_com/hams_open modules
    do -- treating core_addons_path as an ordinary hams_root would derive
    a fullname mypy never actually uses internally, so
    lookup_fully_qualified_or_none would silently never find it (the
    exact "sibling looks present but isn't" failure class this file's own
    get_additional_deps section already had to fix once for a different
    reason)."""
    dotted = _module_fullname_for_file(fpath, hams_roots, core_addons_path)
    if dotted is None:
        return None
    return f"{dotted}.{class_name}"


# odoo.fields.Many2one/One2many/Many2many -- Phase 2 step 2 (comodel resolution). Matched against
# `fullname` in get_function_hook; only fires now that fields.pyi declares these as real (non-Any)
# stub functions -- see that file's own comment for why a hook can never fire on a call to Any.
_RELATIONAL_FIELD_FULLNAMES = {
    "odoo.fields.Many2one",
    "odoo.fields.One2many",
    "odoo.fields.Many2many",
}

# odoo.api.Environment.__getitem__ -- Phase 2 step 4 (env['some.model'] resolution). Same
# "needs a real stub symbol, not Any, for the hook to ever fire" constraint; see api.pyi.
_ENV_GETITEM_FULLNAME = "odoo.api.Environment.__getitem__"


class OdooPlugin(Plugin):
    def __init__(self, options):
        super().__init__(options)
        self._hams_roots = _repo_roots()
        self._core_addons_path = orb.find_odoo_core_addons_path()
        self._registry = _build_real_registry()
        # fullname -> [sibling fullnames] -- computed once at plugin
        # startup, not per-hook-call, since the registry itself is static
        # for the duration of one mypy run.
        self._class_siblings: Dict[str, List[str]] = {}
        # module dotted name -> sibling modules' dotted names. mypy analyzes
        # unconnected files/SCCs in an order this plugin doesn't control;
        # get_customize_class_mro_hook's lookup_fully_qualified_or_none only
        # succeeds if the sibling module has already been analyzed by the
        # time this class's hook runs, which real multi-file runs showed is
        # NOT reliable on its own (confirmed empirically: works for a
        # 2-file case, silently fails for some siblings in a 26-file real
        # run). get_additional_deps below forces mypy's own build graph to
        # treat these modules as depending on each other, the same
        # technique django-stubs uses for the equivalent Django problem.
        self._module_siblings: Dict[str, List[str]] = {}
        # model name -> ordered, deduped [contributor class fullnames] / [contributor module
        # dotted names] -- Phase 2 steps 2 and 4 both need "given a resolved model name, get a
        # real TypeInfo for it" (_resolve_model_instance below), and "given a file that
        # references that model name (as a Many2one/One2many/Many2many comodel, or an
        # env['x'] literal), force at least one real contributor module into the build"
        # (get_additional_deps below). Any single contributor works equally well as the
        # resolved type once its own get_customize_class_mro_hook has run (every contributor of
        # the same final model ends up with every sibling in its own MRO -- see that hook's own
        # comment), so there's no need to single out the "primary" _name-declaring one here.
        self._model_class_fullnames: Dict[str, List[str]] = {}
        self._model_modules: Dict[str, List[str]] = {}
        # module dotted name -> [model names its own field declarations reference as a
        # Many2one/One2many/Many2many comodel] -- Phase 2 step 2's own get_additional_deps
        # extension. Without this, a file's own relational field targets are almost never
        # already part of the build graph (Odoo code references a comodel by string, never by
        # importing that model's actual Python module), so get_function_hook's
        # named_generic_type lookup would silently fail for the common case, not just an edge
        # case -- confirmed empirically this session (see this file's own module docstring for
        # the exact failure mode: KeyError from mypy's own lookup_qualified).
        self._file_comodel_targets: Dict[str, List[str]] = {}
        # module dotted name -> [model names referenced via an env['x'] / self.env['x'] string
        # literal somewhere in that file] -- Phase 2 step 4's own get_additional_deps
        # extension, same rationale as _file_comodel_targets above (env['some.model'] never
        # implies an actual Python import of that model's module either).
        self._file_env_targets: Dict[str, List[str]] = {}
        self._compute_sibling_map()
        self._compute_comodel_targets()
        self._compute_env_targets()

    def _compute_sibling_map(self):
        for model in self._registry.values():
            fullnames = []
            # Per-contributor fullname -> the OTHER models (mixins) that
            # specific contributor's own _inherit list also names, beyond
            # this model itself (the mixin self-reference idiom: _name =
            # "res.users", _inherit = ["res.users", "some.mixin"]). Recorded
            # here, injected into _class_siblings below, once per
            # contributor -- not merged into the model's own sibling set,
            # since it describes this one class's extra MRO need, not
            # res.users' merged identity. ODOO_AWARE_TYPE_CHECKING.md's own
            # "smaller gap found but NOT fixed" note (the user_websites_seo/
            # ResUsersSEO case, _get_seo_fields unresolved) is exactly this.
            contributor_mixin_targets = {}
            for mod, fpath, lineno, class_name, mixin_targets in model.contributors:
                fn = _fullname_for_contributor(
                    mod, fpath, class_name, self._hams_roots, self._core_addons_path
                )
                if fn:
                    fullnames.append(fn)
                    if mixin_targets:
                        contributor_mixin_targets[fn] = mixin_targets
            # Accumulate, never overwrite: a class using the mixin
            # self-reference idiom (`_inherit = ["res.users",
            # "edge.routing.mixin"]`, no `_name`) is a real contributor to
            # BOTH models, so this same fullname `fn` is visited once per
            # model it contributes to across this outer loop's iterations.
            # An earlier version assigned `_class_siblings[fn] = [...]`
            # here, which silently OVERWROTE the sibling list from
            # whichever model was processed first with whichever was
            # processed last -- confirmed as a real bug, not a hypothetical
            # one: user_websites/models/res_users.py's ResUsers class
            # (`_inherit = ["res.users", "edge.routing.mixin"]`, no
            # `_name`) lost its entire res.users sibling list this way
            # whenever "edge.routing.mixin" was the later-inserted registry
            # key, breaking resolution of EVERY res.users-only attribute on
            # that class (`partner_id`, `has_group`, `email`, ...), not
            # just the ones this doc had flagged. Reproduced directly with
            # a minimal probe using the same list-form _inherit before this
            # fix, and confirmed fixed after it (see
            # ODOO_AWARE_TYPE_CHECKING.md's dated update for this session).
            for fn in fullnames:
                self._class_siblings.setdefault(fn, [])
                for other in fullnames:
                    if other != fn and other not in self._class_siblings[fn]:
                        self._class_siblings[fn].append(other)

            # Mixin injection: for each contributor that also named a real,
            # different model in its own _inherit list, add every one of
            # THAT model's own contributors' fullnames as siblings too --
            # one-directional (the mixin's own class doesn't need this
            # contributor's members in ITS mro; only this contributor needs
            # the mixin's). Looked up directly against self._registry
            # (not self._model_class_fullnames, which is still being built
            # across this same outer loop) so this doesn't depend on
            # dict-iteration order ever reaching the mixin model first.
            # Also forces the mixin's own module into the build graph via
            # _module_siblings, the identical ordering fix
            # get_additional_deps already relies on for same-model
            # siblings -- otherwise get_customize_class_mro_hook's
            # ctx.api.lookup_fully_qualified_or_none finds nothing for a
            # mixin class mypy hasn't analyzed yet.
            for fn, mixin_targets in contributor_mixin_targets.items():
                fn_modname = fn.rsplit(".", 1)[0]
                for mixin_target in mixin_targets:
                    mixin_model = self._registry.get(mixin_target)
                    if mixin_model is None:
                        continue
                    for m_mod, m_fpath, m_lineno, m_class_name, _m_mixin_targets in mixin_model.contributors:
                        mixin_fn = _fullname_for_contributor(
                            m_mod, m_fpath, m_class_name, self._hams_roots, self._core_addons_path
                        )
                        if not mixin_fn or mixin_fn == fn:
                            continue
                        self._class_siblings.setdefault(fn, [])
                        if mixin_fn not in self._class_siblings[fn]:
                            self._class_siblings[fn].append(mixin_fn)
                        mixin_modname = mixin_fn.rsplit(".", 1)[0]
                        if mixin_modname != fn_modname:
                            self._module_siblings.setdefault(fn_modname, [])
                            if mixin_modname not in self._module_siblings[fn_modname]:
                                self._module_siblings[fn_modname].append(mixin_modname)

            modnames = sorted({fn.rsplit(".", 1)[0] for fn in fullnames})
            for modname in modnames:
                others = [m for m in modnames if m != modname]
                self._module_siblings.setdefault(modname, [])
                for m in others:
                    if m not in self._module_siblings[modname]:
                        self._module_siblings[modname].append(m)

            self._model_class_fullnames[model.name] = fullnames
            self._model_modules[model.name] = modnames

    def _compute_comodel_targets(self):
        """Populates _file_comodel_targets: for every field any contributor class declared
        anywhere in the registry (not just the "resolved" last-one-wins entry -- a shadowed
        contributor's own comodel reference is just as real a dependency need as the winning
        one's), if it's relational (comodel is not None), record that this field's own file
        references that comodel model."""
        for model in self._registry.values():
            for contributions in model.field_contributions.values():
                for f in contributions:
                    if not f.comodel:
                        continue
                    modname = _module_fullname_for_file(f.file, self._hams_roots, self._core_addons_path)
                    if not modname:
                        continue
                    self._file_comodel_targets.setdefault(modname, [])
                    if f.comodel not in self._file_comodel_targets[modname]:
                        self._file_comodel_targets[modname].append(f.comodel)

    def _compute_env_targets(self):
        """Populates _file_env_targets from odoo_registry_builder.find_env_getitem_targets --
        scanned once at startup like the rest of this plugin's registry-derived state, not
        per-hook-call. Deliberately scans only self._hams_roots (hams_com/hams_open), not core
        Odoo's own addons tree: this plugin's actual job is catching bugs in code this project
        owns, and core Odoo's own internal env['x'] usage isn't code any future run of this
        plugin needs to type-check."""
        env_targets_by_file = orb.find_env_getitem_targets(self._hams_roots)
        for fpath, model_names in env_targets_by_file.items():
            modname = _module_fullname_for_file(fpath, self._hams_roots, self._core_addons_path)
            if not modname:
                continue
            self._file_env_targets.setdefault(modname, [])
            for m in model_names:
                if m not in self._file_env_targets[modname]:
                    self._file_env_targets[modname].append(m)

    def _resolve_model_instance(self, api, model_name: str) -> Optional[Instance]:
        """Given a resolved Odoo model name (e.g. from a Many2one comodel string literal or an
        env['x'] subscript), return a real Instance type for it, or None if the model isn't in
        the registry or none of its contributors' modules are actually part of this mypy run.

        Tries every contributor in registry order, not just the first -- get_additional_deps
        (via _file_comodel_targets/_file_env_targets below) is what's SUPPOSED to guarantee at
        least one contributor module is in the build, but falling through the list if an
        earlier one still isn't resolvable (a file this plugin's _fullname_for_contributor
        couldn't derive a fullname for, or one get_additional_deps didn't reach for some other
        reason) is strictly more robust than only trying the first and giving up.

        Deliberately does NOT use CheckerPluginInterface.named_generic_type (the "obvious"
        officially-declared API for this). Confirmed empirically this session, not assumed:
        named_generic_type -> TypeChecker.lookup_typeinfo -> lookup_qualified walks each
        INTERMEDIATE package component by looking it up as an already-registered attribute of
        its parent package's OWN symbol table (`n.names.get(parts[i])`) -- which is exactly how
        Python's real import machinery populates namespaces when an actual `import a.b.c`
        statement is processed, but Odoo's own comodel/env['x'] string literals never generate a
        real import of the target's module. get_additional_deps forces the target module to be
        *parsed and analyzed* (so it exists in mypy's `self.modules`), but that alone does NOT
        register it as an attribute on its parent packages' symbol tables -- confirmed directly:
        every real-code lookup here failed with "AssertionError: Internal error: attempted
        lookup of unknown name" (an assert inside lookup_qualified's walk) even for contributors
        get_additional_deps had genuinely pulled into the build.

        The fix: replicate the SAME direct-module-dict lookup
        SemanticAnalyzerPluginInterface.lookup_fully_qualified_or_none already uses (and that
        get_customize_class_mro_hook above already relies on successfully) -- split the fullname
        into (module, name), check `module in api.modules` directly (no parent-chain walk), then
        look up `name` in that module's own top-level symbol table. That officially-declared
        method lives on SemanticAnalyzerPluginInterface, not CheckerPluginInterface (the trait
        FunctionContext/MethodContext.api actually exposes at this, later, type-checking phase);
        `api.modules` itself isn't declared on either narrow trait, but the concrete TypeChecker
        instance backing `ctx.api` genuinely carries it (checker.py's own `self.modules = modules`
        in TypeChecker.__init__) -- accessed defensively via getattr, not assumed, since a future
        mypy version narrowing what's exposed here should degrade to "resolution unavailable",
        not crash the whole run."""
        fullnames = self._model_class_fullnames.get(model_name)
        if not fullnames:
            return None
        modules = getattr(api, "modules", None)
        if modules is None:
            return None
        for fn in fullnames:
            module, _, class_name = fn.rpartition(".")
            filenode = modules.get(module)
            if filenode is None:
                continue
            sym = filenode.names.get(class_name)
            if sym is None or not isinstance(sym.node, TypeInfo):
                continue
            info = sym.node
            any_type = AnyType(TypeOfAny.from_omitted_generics)
            return Instance(info, [any_type] * len(info.defn.type_vars))
        return None

    def get_additional_deps(self, file: MypyFile) -> List[tuple]:
        deps: Set[str] = set(self._module_siblings.get(file.fullname, []))
        for comodel in self._file_comodel_targets.get(file.fullname, []):
            deps.update(self._model_modules.get(comodel, []))
        for comodel in self._file_env_targets.get(file.fullname, []):
            deps.update(self._model_modules.get(comodel, []))
        deps.discard(file.fullname)
        if not deps:
            return []
        return [(10, modname, -1) for modname in sorted(deps)]

    def get_function_hook(self, fullname: str) -> Optional[Callable[[FunctionContext], Type]]:
        if fullname not in _RELATIONAL_FIELD_FULLNAMES:
            return None

        def callback(ctx: FunctionContext) -> Type:
            if not (ctx.args and ctx.args[0]):
                return ctx.default_return_type
            first_arg = ctx.args[0][0]
            if not isinstance(first_arg, StrExpr):
                return ctx.default_return_type
            resolved = self._resolve_model_instance(ctx.api, first_arg.value)
            if resolved is None:
                return ctx.default_return_type
            return resolved

        return callback

    def get_method_hook(self, fullname: str) -> Optional[Callable[[MethodContext], Type]]:
        if fullname != _ENV_GETITEM_FULLNAME:
            return None

        def callback(ctx: MethodContext) -> Type:
            if not (ctx.args and ctx.args[0]):
                return ctx.default_return_type
            arg = ctx.args[0][0]
            if not isinstance(arg, StrExpr):
                return ctx.default_return_type
            resolved = self._resolve_model_instance(ctx.api, arg.value)
            if resolved is None:
                return ctx.default_return_type
            return resolved

        return callback

    def get_customize_class_mro_hook(self, fullname: str) -> Optional[Callable[[ClassDefContext], None]]:
        if fullname not in self._class_siblings:
            return None

        def callback(ctx: ClassDefContext) -> None:
            for sibling_fullname in self._class_siblings.get(fullname, []):
                sibling_info = ctx.api.lookup_fully_qualified_or_none(sibling_fullname)
                if sibling_info is None or sibling_info.node is None:
                    continue
                sibling_typeinfo = sibling_info.node
                # _name/_inherit are Odoo registry-build metadata, never
                # read at runtime the way a real attribute is -- and
                # different _inherit contributors legitimately declare
                # them with different literal shapes (plain string vs.
                # the mixin self-reference list form). Once MRO injection
                # makes one sibling a nearer ancestor of another, mypy
                # applies override-compatibility checking to these class
                # body assignments as if they were a real subclass
                # relationship, which they aren't. Stubbing them as Any on
                # BaseModel doesn't help (the nearer injected ancestor
                # shadows that declaration) -- dropping the declaration
                # from each injected sibling's own symbol table is what
                # actually removes them from the comparison. Confirmed
                # empirically: BaseModel-level Any alone left this
                # artifact in place across 8 real files.
                for meta_attr in ("_name", "_inherit"):
                    sibling_typeinfo.names.pop(meta_attr, None)
                if sibling_typeinfo not in ctx.cls.info.mro:
                    ctx.cls.info.mro.append(sibling_typeinfo)

        return callback


def plugin(version: str):
    return OdooPlugin
