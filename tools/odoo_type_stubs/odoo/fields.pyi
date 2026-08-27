# This software is distributed under the terms of the GNU General Public License, version 3.
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Minimal, original stub for odoo.fields (ODOO_AWARE_TYPE_CHECKING.md Phase 2, step 2).
#
# Only Many2one/One2many/Many2many are declared as real (non-Any) functions here -- that's
# deliberate, not an oversight: a mypy `get_function_hook` can only ever fire for a call whose
# callee mypy has already resolved to a concrete, named function/class symbol. Before this file
# existed, `odoo/__init__.pyi` declared the whole `fields` submodule as `fields: Any`, so
# `fields.Many2one(...)` type-checked as calling an attribute of `Any` -- mypy never even knows
# it's `odoo.fields.Many2one` being called, and no `get_function_hook` registered for that
# fullname can ever run. Confirmed directly this session (not assumed): registering the hook
# alone, without first turning `Many2one`/`One2many`/`Many2many` into real stub symbols, left the
# hook dead code -- it never fired.
#
# Every other field constructor Odoo ships (Char, Text, Selection, Datetime, Reference, ...) is
# deliberately left unstubbed and falls through the module-level `__getattr__` below, resolving
# to `Any` the same way the whole module used to. This isn't a partial-effort shortcut: this
# plugin's actual job for relational fields is resolving the comodel argument against the merged
# registry (`ODOO_AWARE_TYPE_CHECKING.md` step 2) -- plain scalar fields have no comodel to
# resolve, so giving them a real, precisely-typed stub buys nothing for that job and risks new
# false positives on real call patterns this session didn't audit (`fields.Datetime.now()`,
# `fields.Date.today()`, `fields.Date.context_today()`, `fields.first(...)`, and any other
# class-attribute/module-function idiom on a type this file doesn't declare). The module-level
# `__getattr__` is the standard PEP 484 "partial stub" escape hatch for exactly this -- it is NOT
# the pattern `odoo_type_stubs/odoo/models.pyi` explicitly bans on `BaseModel` (a `__getattr__`
# there would swallow every real "this attribute doesn't exist" bug this whole project exists to
# catch); a module-level `__getattr__` on `fields` only affects which *field constructor names*
# resolve to `Any`, not whether a real model's own attributes get checked.
from typing import Any


def Many2one(comodel_name: str = ..., string: str = ..., *args: Any, **kwargs: Any) -> Any: ...
def One2many(
    comodel_name: str = ..., inverse_name: str = ..., string: str = ..., *args: Any, **kwargs: Any
) -> Any: ...
def Many2many(
    comodel_name: str = ...,
    relation: str = ...,
    column1: str = ...,
    column2: str = ...,
    string: str = ...,
    *args: Any,
    **kwargs: Any,
) -> Any: ...


def __getattr__(name: str) -> Any: ...
