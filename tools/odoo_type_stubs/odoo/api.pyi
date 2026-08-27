# This software is distributed under the terms of the GNU General Public License, version 3.
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Minimal, original stub for odoo.api (ODOO_AWARE_TYPE_CHECKING.md Phase 2, step 4).
#
# Environment is declared as a real (non-Any) class purely so `get_method_hook` has a concrete
# `odoo.api.Environment.__getitem__` fullname to match -- the same "a hook can't fire on a call to
# Any" constraint documented in fields.pyi's own comment applies here identically: with `env: Any`
# on BaseModel (this stub's previous state), `self.env['res.users']` type-checks as subscripting
# `Any`, and no `__getitem__` hook registered for `Environment` ever runs. Confirmed directly this
# session, same way as the fields.py finding.
#
# Every other Environment attribute (`.user`, `.cr`, `.company`, `.context`, `.uid`, `.su`,
# `.ref`, `.lang`, `.is_admin()`, ...) is deliberately left unmodeled via the class-level
# `__getattr__` below -- this file's only job is making `env['some.model']` resolve to the real
# merged model, not building a complete Environment interface (real, separate, not-attempted
# work). A `__getattr__` here is safe for the same reason it's safe on the `fields` module: this
# isn't `BaseModel.__getattr__` (banned per models.pyi's own comment, because it would swallow
# real "this model attribute doesn't exist" bugs) -- Environment's own attribute surface was never
# checked before this file existed (it was `Any`), so nothing this project checks today regresses.
from typing import Any


class Environment:
    # Real Odoo code constructs one directly (`api.Environment(cr, uid, context)`), not just
    # accesses `self.env` -- confirmed directly this session: without a permissive __init__ here,
    # mypy infers the default no-arg `object.__init__` and flags every real 3-positional-arg call
    # site as "Too many arguments for Environment".
    def __init__(self, *args: Any, **kwargs: Any) -> None: ...
    def __getitem__(self, model_name: str) -> Any: ...
    # Real code checks whether an optional OCA dependency is installed via
    # `'storage.backend' in self.env` (this codebase even has a dedicated
    # `# burn-ignore-optional-oca-dep` tag in check_burn_list.py for exactly this idiom).
    # `in` is dunder-dispatched (type(obj).__contains__), which bypasses the __getattr__
    # fallback below the same way subscripting does -- confirmed via a real mypy sweep
    # beyond res.users (hams_s3/models/res_config_settings.py) where every use of this
    # idiom false-positived as "Environment has no attribute __iter__ (not iterable)"
    # before this line existed. Without an explicit __contains__, mypy falls back to
    # __iter__ (also unmodeled) rather than __getattr__, so this needs its own entry
    # exactly like __getitem__ above.
    def __contains__(self, model_name: str) -> bool: ...
    def __getattr__(self, name: str) -> Any: ...


# Real code also uses `api.model`, `api.constrains(...)`, `api.depends(...)`,
# `api.model_create_multi`, `api.onchange(...)`, etc. as decorators directly on the `api` module
# object, not as Environment members -- confirmed directly this session: without this module-level
# fallback, turning `api` from a blanket `Any` into a real stub module (needed so `Environment`
# itself has a concrete fullname) made every one of those decorator usages a new "Module has no
# attribute" false positive. Same PEP 484 partial-stub escape hatch as fields.pyi's own
# module-level __getattr__, and not the banned BaseModel.__getattr__ pattern for the same reason
# documented there.
def __getattr__(name: str) -> Any: ...
