# This software is distributed under the terms of the GNU General Public License, version 3.
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Minimal, original stub for odoo.models -- written directly from reading
# the real installed Odoo 19 source (/usr/lib/python3/dist-packages/odoo/
# orm/models.py and orm/fields.py), not derived from or copied out of any
# third-party stub project. Confirmed directly (not assumed) that Odoo
# ships no py.typed marker and no .pyi stubs of its own, so `from odoo
# import models` resolves every name to `Any` under plain mypy -- which
# in turn makes mypy skip attribute-checking entirely on any class
# deriving from it, real bugs and all. This file's only job is to give
# Model/AbstractModel/TransientModel a real (non-Any) TypeInfo so mypy's
# own native attribute checking actually activates on classes deriving
# from them; odoo_mypy_plugin.py's MRO-injection hook is what then makes
# that checking see the *merged* attribute set across `_inherit`
# contributors, not just one file's own class body.
#
# BaseModel below declares the handful of real ORM primitives
# (env/id/ids/write/create/...) that a whole-tree run against this
# codebase's actual res.users contributors showed dominate false
# positives without them (env/id/__iter__/ensure_one/with_user/ids alone
# were ~77% of 393 errors in that run) -- these are declared, not
# precisely typed: the job is making `self.env` resolve to *something*,
# not modeling Odoo's real generic recordset typing (that's
# odoo-stubs-scale work, not this file's job). Do NOT add
# `__getattr__(self, name: str) -> Any` here as a shortcut to silence
# the rest -- that defeats the actual point of this stub (making a
# genuinely nonexistent attribute still get flagged) as surely as not
# stubbing BaseModel at all.
from typing import Any, Iterator

from .api import Environment


class BaseModel:
    # Environment, not Any, as of Phase 2 step 4 (ODOO_AWARE_TYPE_CHECKING.md) -- needed so
    # `self.env['some.model']` has a concrete `Environment.__getitem__` fullname for
    # odoo_mypy_plugin.py's get_method_hook to match; see api.pyi's own comment for why every
    # other Environment attribute stays unmodeled (falls through Environment.__getattr__) rather
    # than this being a step toward a complete Environment stub.
    env: Environment
    id: Any
    ids: list
    pool: Any
    _fields: Any
    _name: str
    # Any, not str | list[str]: real Odoo accepts both the plain-string
    # form (_inherit = "res.users") and the mixin self-reference form
    # (_inherit = ["res.users", "edge.routing.mixin"]) across different
    # _inherit contributors to the *same* model. Once MRO injection makes
    # those contributors look like a subclass chain to mypy, a precise
    # str type here produces spurious "incompatible types in assignment"
    # errors between siblings that were never really overriding each
    # other -- confirmed against three real files that hit exactly this.
    _inherit: Any

    def __iter__(self) -> Iterator[Any]: ...
    def __len__(self) -> int: ...
    # A real Odoo recordset supports index/slice access (records[0], records[1:3]), each
    # returning a new recordset of the same model, not a plain Python element -- Any, matching
    # __iter__'s own looser choice above, not a precise Self/overload pair (that's
    # odoo-stubs-scale modeling, not this file's job; see the module docstring).
    def __getitem__(self, index: Any) -> Any: ...
    def ensure_one(self) -> Any: ...
    def exists(self) -> Any: ...
    def browse(self, ids: Any = ...) -> Any: ...
    def sudo(self, flag: bool = ...) -> Any: ...
    def with_user(self, user: Any) -> Any: ...
    def with_company(self, company: Any) -> Any: ...
    def with_env(self, env: Any) -> Any: ...
    def with_context(self, *args: Any, **kwargs: Any) -> Any: ...
    def search(self, domain: Any, offset: int = ..., limit: Any = ..., order: Any = ...) -> Any: ...
    def _search(self, *args: Any, **kwargs: Any) -> Any: ...
    def create(self, vals_list: Any) -> Any: ...
    def write(self, vals: Any) -> bool: ...
    def unlink(self) -> bool: ...
    def mapped(self, func: Any) -> Any: ...
    def filtered(self, func: Any) -> Any: ...
    def _register_hook(self) -> None: ...


class Model(BaseModel):
    pass


class AbstractModel(BaseModel):
    pass


class TransientModel(BaseModel):
    pass


# Real Odoo 19 API (odoo/orm/models.py) for declarative SQL constraints,
# e.g. `_name_unique = models.Constraint("UNIQUE(name)", "...")`. Left as
# Any rather than modeled precisely, same rationale as __init__.pyi's
# other unstubbed names -- this file's job is Model/AbstractModel/
# TransientModel, not a complete models.py surface.
from typing import Any

Constraint: Any
