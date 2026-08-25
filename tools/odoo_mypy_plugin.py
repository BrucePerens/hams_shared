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
to check).
"""

import os
import sys
from typing import Callable, Dict, List, Optional

from mypy.nodes import MypyFile
from mypy.plugin import Plugin, ClassDefContext

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
    fpath_abs = os.path.abspath(fpath)
    if core_addons_path:
        core_abs = os.path.abspath(core_addons_path)
        if fpath_abs.startswith(core_abs + os.sep):
            rel = os.path.relpath(fpath_abs, core_abs)
            if rel.endswith(".py"):
                rel = rel[: -len(".py")]
            dotted = rel.replace(os.sep, ".")
            return f"odoo.addons.{dotted}.{class_name}"

    for root in hams_roots:
        root_abs = os.path.abspath(root)
        if fpath_abs.startswith(root_abs + os.sep):
            rel = os.path.relpath(fpath_abs, root_abs)
            if rel.endswith(".py"):
                rel = rel[: -len(".py")]
            dotted = rel.replace(os.sep, ".")
            return f"{dotted}.{class_name}"
    return None


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
        self._compute_sibling_map()

    def _compute_sibling_map(self):
        for model in self._registry.values():
            fullnames = []
            for mod, fpath, lineno, class_name in model.contributors:
                fn = _fullname_for_contributor(
                    mod, fpath, class_name, self._hams_roots, self._core_addons_path
                )
                if fn:
                    fullnames.append(fn)
            for fn in fullnames:
                self._class_siblings[fn] = [other for other in fullnames if other != fn]

            modnames = sorted({fn.rsplit(".", 1)[0] for fn in fullnames})
            for modname in modnames:
                others = [m for m in modnames if m != modname]
                self._module_siblings.setdefault(modname, [])
                for m in others:
                    if m not in self._module_siblings[modname]:
                        self._module_siblings[modname].append(m)

    def get_additional_deps(self, file: MypyFile) -> List[tuple]:
        siblings = self._module_siblings.get(file.fullname)
        if not siblings:
            return []
        return [(10, modname, -1) for modname in siblings]

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
